"""Per-user key storage: AES-GCM ciphertext in SQLite, one Google key (required) and one Tavily key (optional) per user.

Each user's AES key is `HKDF(master_secret, info=uid)` and the `uid` is the GCM associated data, so a ciphertext
copied into another user's row fails to decrypt. Every ciphertext carries the `key_id` of the master secret that made
it, so the master secret can be rotated: new ciphertexts use the current id, old ones still decrypt while their secret
is listed in `KEY_ENCRYPTION_PREVIOUS`. Plaintext keys are never returned by the API after saving; only `last4` is.
"""

from __future__ import annotations

import base64
import contextlib
import os
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Depends
from pydantic import BaseModel

from bessible.auth import LOCAL_USER, User, current_user
from bessible.config import settings
from bessible.models import EncryptedCredentials

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

NONCE_BYTES = 12
KEY_BYTES = 32

type Provider = Literal["google", "tavily"]
TABLES: dict[Provider, str] = {"google": "user_keys", "tavily": "user_tavily_keys"}


class KeyStoreError(Exception):
    """The master secret is missing, or a ciphertext cannot be decrypted for this user."""


class KeyMeta(BaseModel):
    """What the API may say about a stored key. Never the key itself."""

    last4: str
    updated_at: datetime


class Keyring:
    """Encrypts and decrypts one user's key under the master secrets, by `key_id`."""

    def __init__(self, current_key_id: str, secrets: Mapping[str, bytes]) -> None:
        """`secrets` maps each `key_id` to its master secret; `current_key_id` must be one of them."""
        if current_key_id not in secrets:
            msg = "KEY_ENCRYPTION_SECRET is not set"
            raise KeyStoreError(msg)
        self._current = current_key_id
        self._secrets = dict(secrets)

    @classmethod
    def from_settings(cls) -> Keyring:
        """Build the keyring from `KEY_ENCRYPTION_SECRET` and `KEY_ENCRYPTION_PREVIOUS`."""
        secrets = {kid: s.get_secret_value().encode() for kid, s in settings.key_encryption_previous.items()}
        if settings.key_encryption_secret is not None:
            secrets[settings.key_encryption_key_id] = settings.key_encryption_secret.get_secret_value().encode()
        return cls(settings.key_encryption_key_id, secrets)

    def _aead(self, key_id: str, uid: str) -> AESGCM:
        try:
            master = self._secrets[key_id]
        except KeyError as exc:
            msg = f"unknown key_id {key_id!r}"
            raise KeyStoreError(msg) from exc
        derived = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=uid.encode()).derive(master)
        return AESGCM(derived)

    def seal(self, uid: str, plaintext: str) -> tuple[str, str]:
        """Encrypt `plaintext` for `uid` under the current master secret. Returns `(key_id, ciphertext)`."""
        nonce = os.urandom(NONCE_BYTES)
        ct = self._aead(self._current, uid).encrypt(nonce, plaintext.encode(), uid.encode())
        return self._current, base64.urlsafe_b64encode(nonce + ct).decode()

    def open(self, uid: str, key_id: str, ciphertext: str) -> str:
        """Return the plaintext. Raises `KeyStoreError` for another user's ciphertext or an unknown `key_id`."""
        try:
            blob = base64.urlsafe_b64decode(ciphertext.encode())
            plain = self._aead(key_id, uid).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], uid.encode())
        except (InvalidTag, ValueError) as exc:
            msg = "stored key cannot be decrypted"
            raise KeyStoreError(msg) from exc
        return plain.decode()

    def encrypt(self, uid: str, plaintext: str) -> EncryptedCredentials:
        """Encrypt a Google key for `uid` as run credentials."""
        key_id, ct = self.seal(uid, plaintext)
        return EncryptedCredentials(uid=uid, key_id=key_id, google_ct=ct)

    def decrypt(self, creds: EncryptedCredentials) -> str:
        """Return the plaintext Google key of run credentials."""
        return self.open(creds.uid, creds.key_id, creds.google_ct)


class KeyStore:
    """SQLite tables `user_keys` (Google) and `user_tavily_keys`, each `(uid, ciphertext, key_id, last4, updated_at)`.

    The file has mode 0600. Every method takes the `provider` whose table it uses; Google is the default.
    """

    def __init__(self, db_path: Path, keyring: Keyring) -> None:
        """Open (creating, mode 0600) the SQLite file and its table."""
        self._path = db_path
        self._keyring = keyring
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file 0600 before SQLite opens it, so the keys are never world-readable, even briefly.
        os.close(os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600))
        with self._connect() as conn, conn:
            for table in TABLES.values():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "uid TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, key_id TEXT NOT NULL,"
                    " last4 TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )

    def _connect(self) -> contextlib.closing[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return contextlib.closing(conn)

    def put(self, uid: str, key: str, provider: Provider = "google") -> KeyMeta:
        """Encrypt and store (replace) the user's key for `provider`."""
        key_id, ct = self._keyring.seal(uid, key)
        meta = KeyMeta(last4=key[-4:], updated_at=datetime.now(UTC))
        with self._connect() as conn, conn:
            conn.execute(
                f"INSERT INTO {TABLES[provider]}(uid, ciphertext, key_id, last4, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(uid) DO UPDATE SET ciphertext=excluded.ciphertext, key_id=excluded.key_id,"
                " last4=excluded.last4, updated_at=excluded.updated_at",
                (uid, ct, key_id, meta.last4, meta.updated_at.isoformat()),
            )
        return meta

    def meta(self, uid: str, provider: Provider = "google") -> KeyMeta | None:
        """Return `last4` and the update time, or None if the user has no key for `provider`."""
        with self._connect() as conn:
            row = conn.execute(f"SELECT last4, updated_at FROM {TABLES[provider]} WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else KeyMeta(last4=row[0], updated_at=datetime.fromisoformat(row[1]))

    def _sealed(self, uid: str, provider: Provider) -> tuple[str, str] | None:
        """`(ciphertext, key_id)` of the user's key for `provider`, or None."""
        with self._connect() as conn:
            row = conn.execute(f"SELECT ciphertext, key_id FROM {TABLES[provider]} WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else (row[0], row[1])

    def credentials(self, uid: str) -> EncryptedCredentials | None:
        """Return the stored ciphertexts for a run's workflow input, or None if the user has no Google key."""
        google = self._sealed(uid, "google")
        if google is None:
            return None
        tavily = self._sealed(uid, "tavily")
        return EncryptedCredentials(
            uid=uid,
            key_id=google[1],
            google_ct=google[0],
            tavily_ct=tavily[0] if tavily else None,
            tavily_key_id=tavily[1] if tavily else None,
        )

    def checked_credentials(self, uid: str) -> EncryptedCredentials | None:
        """Like `credentials`, but proves every ciphertext decrypts for this user first (else `KeyStoreError`)."""
        creds = self.credentials(uid)
        if creds is not None:
            self._keyring.decrypt(creds)
            if creds.tavily_ct is not None and creds.tavily_key_id is not None:
                self._keyring.open(uid, creds.tavily_key_id, creds.tavily_ct)
        return creds

    def reveal(self, uid: str, provider: Provider = "google") -> str | None:
        """Decrypt the user's key for server-side use only (a key ping). Never send this to the browser."""
        sealed = self._sealed(uid, provider)
        return None if sealed is None else self._keyring.open(uid, sealed[1], sealed[0])

    def delete(self, uid: str, provider: Provider = "google") -> None:
        """Remove the user's key row for `provider`."""
        with self._connect() as conn, conn:
            conn.execute(f"DELETE FROM {TABLES[provider]} WHERE uid = ?", (uid,))


def get_key_store(_user: Annotated[User, Depends(current_user)]) -> KeyStore:
    """FastAPI dependency: the key store on the VM, built from settings (tests override this).

    Depends on the signed-in user so an unauthenticated request gets 401 before the key database is ever opened.
    With sign-in off (local dev), the local user starts with your `GOOGLE_API_KEY` from `.env`, as the CLI does.
    `TAVILY_API_KEY` is not copied: it stays the server's fallback for users without their own Tavily key.
    """
    store = KeyStore(settings.key_db_path, Keyring.from_settings())
    if not settings.auth_enabled and settings.google_api_key is not None and store.meta(LOCAL_USER.uid) is None:
        store.put(LOCAL_USER.uid, settings.google_api_key.get_secret_value())
    return store
