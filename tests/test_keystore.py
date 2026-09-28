"""Key store: AES-GCM round trip, per-user binding, rotation, file mode."""

from __future__ import annotations

import sqlite3
import stat
from typing import TYPE_CHECKING

import pytest

from bessible.keystore import Keyring, KeyStore, KeyStoreError
from bessible.models import EncryptedCredentials

if TYPE_CHECKING:
    from pathlib import Path

KEY = "AIza" + "x" * 31 + "WXYZ"


def _store(path: Path, key_id: str = "k1", **secrets: bytes) -> KeyStore:
    return KeyStore(path / "keys.db", Keyring(key_id, secrets or {"k1": b"master-one"}))


def test_round_trip_and_meta(tmp_path: Path) -> None:
    store = _store(tmp_path)
    meta = store.put("alice", KEY)
    assert meta.last4 == "WXYZ"
    assert store.reveal("alice") == KEY
    assert store.meta("alice") == meta
    assert store.reveal("nobody") is None
    assert store.meta("nobody") is None


def test_put_replaces_and_delete_removes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("alice", KEY)
    store.put("alice", KEY[:-4] + "0000")
    assert store.meta("alice").last4 == "0000"
    store.delete("alice")
    assert store.credentials("alice") is None


def test_plaintext_not_in_database_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("alice", KEY)
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("keys.db*"))
    assert KEY.encode() not in raw


def test_ciphertext_copied_to_another_user_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("alice", KEY)
    store.put("bob", "AIza" + "y" * 35)
    alice = store.credentials("alice")
    with sqlite3.connect(tmp_path / "keys.db") as conn:
        conn.execute("UPDATE user_keys SET ciphertext = ? WHERE uid = 'bob'", (alice.google_ct,))
    with pytest.raises(KeyStoreError):
        store.reveal("bob")
    with pytest.raises(KeyStoreError):
        store.checked_credentials("bob")


def test_wrong_uid_in_credentials_fails() -> None:
    ring = Keyring("k1", {"k1": b"master-one"})
    creds = ring.encrypt("alice", KEY)
    with pytest.raises(KeyStoreError):
        ring.decrypt(EncryptedCredentials(uid="mallory", key_id=creds.key_id, google_ct=creds.google_ct))


def test_tampered_ciphertext_fails() -> None:
    ring = Keyring("k1", {"k1": b"master-one"})
    creds = ring.encrypt("alice", KEY)
    with pytest.raises(KeyStoreError):
        ring.decrypt(creds.model_copy(update={"google_ct": "AAAA" + creds.google_ct[4:]}))


def test_rotation_old_ciphertext_still_decrypts(tmp_path: Path) -> None:
    old = _store(tmp_path)
    old.put("alice", KEY)
    # Rotate: k2 is now current, k1 stays listed until every key is re-saved.
    rotated = _store(tmp_path, "k2", k1=b"master-one", k2=b"master-two")
    assert rotated.credentials("alice").key_id == "k1"
    assert rotated.reveal("alice") == KEY
    rotated.put("alice", KEY)
    assert rotated.credentials("alice").key_id == "k2"
    # Once k1 is dropped, a k1 ciphertext no longer decrypts.
    dropped = _store(tmp_path, "k2", k2=b"master-two")
    assert dropped.reveal("alice") == KEY


def test_dropped_master_secret_fails(tmp_path: Path) -> None:
    _store(tmp_path).put("alice", KEY)
    dropped = _store(tmp_path, "k2", k2=b"master-two")
    with pytest.raises(KeyStoreError):
        dropped.reveal("alice")


def test_missing_master_secret_raises() -> None:
    with pytest.raises(KeyStoreError):
        Keyring("k1", {})


def test_db_file_mode_is_0600(tmp_path: Path) -> None:
    _store(tmp_path).put("alice", KEY)
    assert stat.S_IMODE((tmp_path / "keys.db").stat().st_mode) == 0o600


TAVILY = "tvly-" + "t" * 20 + "TAVY"


def test_tavily_key_is_stored_apart_and_rides_in_run_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("alice", TAVILY, "tavily")
    assert store.credentials("alice") is None  # a run still needs the Google key
    assert store.meta("alice") is None
    assert store.meta("alice", "tavily").last4 == "TAVY"
    store.put("alice", KEY)
    creds = store.checked_credentials("alice")
    assert creds.tavily_ct
    assert creds.tavily_key_id == "k1"
    assert TAVILY not in creds.model_dump_json()
    assert store.reveal("alice", "tavily") == TAVILY
    store.delete("alice", "tavily")
    assert store.credentials("alice").tavily_ct is None
    assert store.reveal("alice") == KEY  # deleting Tavily leaves Google


def test_tavily_ciphertext_copied_to_another_user_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("alice", TAVILY, "tavily")
    store.put("bob", KEY)
    store.put("bob", TAVILY[:-4] + "BBBB", "tavily")
    with sqlite3.connect(tmp_path / "keys.db") as conn:
        ct = conn.execute("SELECT ciphertext FROM user_tavily_keys WHERE uid = 'alice'").fetchone()[0]
        conn.execute("UPDATE user_tavily_keys SET ciphertext = ? WHERE uid = 'bob'", (ct,))
    with pytest.raises(KeyStoreError):
        store.checked_credentials("bob")
