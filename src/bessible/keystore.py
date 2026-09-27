"""Per-user Google key storage: AES-GCM ciphertext in SQLite, one key per user.

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
from typing import TYPE_CHECKING, Annotated

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

    def encrypt(self, uid: str, plaintext: str) -> EncryptedCredentials:
        """Encrypt `plaintext` for `uid` under the current master secret."""
        nonce = os.urandom(NONCE_BYTES)
        ct = self._aead(self._current, uid).encrypt(nonce, plaintext.encode(), uid.encode())
        return EncryptedCredentials(
            uid=uid, key_id=self._current, google_ct=base64.urlsafe_b64encode(nonce + ct).decode()
        )

    def decrypt(self, creds: EncryptedCredentials) -> str:
        """Return the plaintext key. Raises `KeyStoreError` for another user's ciphertext or an unknown `key_id`."""
        try:
            blob = base64.urlsafe_b64decode(creds.google_ct.encode())
            plain = self._aead(creds.key_id, creds.uid).decrypt(
                blob[:NONCE_BYTES], blob[NONCE_BYTES:], creds.uid.encode()
            )
        except (InvalidTag, ValueError) as exc:
            msg = "stored key cannot be decrypted"
            raise KeyStoreError(msg) from exc
        return plain.decode()


class KeyStore:
    """SQLite table `user_keys(uid, ciphertext, key_id, last4, updated_at)`, file mode 0600."""

    def __init__(self, db_path: Path, keyring: Keyring) -> None:
        """Open (creating, mode 0600) the SQLite file and its table."""
        self._path = db_path
        self._keyring = keyring
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file 0600 before SQLite opens it, so the keys are never world-readable, even briefly.
        os.close(os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600))
        with self._connect() as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_keys ("
                "uid TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, key_id TEXT NOT NULL,"
                " last4 TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _connect(self) -> contextlib.closing[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return contextlib.closing(conn)

    def put(self, uid: str, google_key: str) -> KeyMeta:
        """Encrypt and store (replace) the user's Google key."""
        creds = self._keyring.encrypt(uid, google_key)
        meta = KeyMeta(last4=google_key[-4:], updated_at=datetime.now(UTC))
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO user_keys(uid, ciphertext, key_id, last4, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(uid) DO UPDATE SET ciphertext=excluded.ciphertext, key_id=excluded.key_id,"
                " last4=excluded.last4, updated_at=excluded.updated_at",
                (uid, creds.google_ct, creds.key_id, meta.last4, meta.updated_at.isoformat()),
            )
        return meta

    def meta(self, uid: str) -> KeyMeta | None:
        """Return `last4` and the update time, or None if the user has no key."""
        with self._connect() as conn:
            row = conn.execute("SELECT last4, updated_at FROM user_keys WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else KeyMeta(last4=row[0], updated_at=datetime.fromisoformat(row[1]))

    def credentials(self, uid: str) -> EncryptedCredentials | None:
        """Return the stored ciphertext for a run's workflow input, or None if the user has no key."""
        with self._connect() as conn:
            row = conn.execute("SELECT ciphertext, key_id FROM user_keys WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else EncryptedCredentials(uid=uid, key_id=row[1], google_ct=row[0])

    def checked_credentials(self, uid: str) -> EncryptedCredentials | None:
        """Like `credentials`, but proves the ciphertext decrypts for this user first (else `KeyStoreError`)."""
        creds = self.credentials(uid)
        if creds is not None:
            self._keyring.decrypt(creds)
        return creds

    def reveal(self, uid: str) -> str | None:
        """Decrypt the user's key for server-side use only (the Gemini ping). Never send this to the browser."""
        creds = self.credentials(uid)
        return None if creds is None else self._keyring.decrypt(creds)

    def delete(self, uid: str) -> None:
        """Remove the user's key row."""
        with self._connect() as conn, conn:
            conn.execute("DELETE FROM user_keys WHERE uid = ?", (uid,))


def get_key_store(_user: Annotated[User, Depends(current_user)]) -> KeyStore:
    """FastAPI dependency: the key store on the VM, built from settings (tests override this).

    Depends on the signed-in user so an unauthenticated request gets 401 before the key database is ever opened.
    With sign-in off (local dev), the local user starts with your `GOOGLE_API_KEY` from `.env`, as the CLI does.
    """
    store = KeyStore(settings.key_db_path, Keyring.from_settings())
    if not settings.auth_enabled and settings.google_api_key is not None and store.meta(LOCAL_USER.uid) is None:
        store.put(LOCAL_USER.uid, settings.google_api_key.get_secret_value())
    return store
