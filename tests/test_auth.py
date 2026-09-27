"""Firebase sign-in: token verification, 401 without a token, the email allow-list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from bessible import auth
from bessible.api.app import app
from bessible.auth import get_token_verifier
from bessible.config import settings
from bessible.keystore import Keyring, KeyStore, get_key_store

if TYPE_CHECKING:
    from pathlib import Path

PROJECT = "bessible-test"
ISS = f"https://securetoken.google.com/{PROJECT}"

PROTECTED = [
    ("POST", "/runs"),
    ("GET", "/runs/bessible-x/status"),
    ("GET", "/runs/bessible-x/result"),
    ("POST", "/runs/bessible-x/decision"),
    ("GET", "/runs/bessible-x/events"),
    ("GET", "/me/key"),
    ("PUT", "/me/key"),
    ("DELETE", "/me/key"),
    ("POST", "/me/key/test"),
    ("POST", "/capacity/check"),
    ("GET", "/site-data?lat=51&lon=0"),
]


def _client(verifier: Any, tmp_path: Path | None = None) -> TestClient:
    """A client whose tokens are checked by `verifier`; with `tmp_path`, `/me/key` reads an empty temp key store."""
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    if tmp_path is not None:
        store = KeyStore(tmp_path / "keys.db", Keyring("k1", {"k1": b"secret"}))
        app.dependency_overrides[get_key_store] = lambda: store
    return TestClient(app)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - applies to every test here
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "auth_enabled", True)  # a developer's .env may turn sign-in off
    yield
    app.dependency_overrides.clear()


def _good(_token: str) -> dict[str, Any]:
    return {"sub": "uid-1", "email": "a@example.com", "email_verified": True}


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_no_token_is_401(method: str, path: str) -> None:
    res = _client(_good).request(method, path)
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Token abc", "abc"])
def test_malformed_header_is_401(header: str) -> None:
    res = _client(_good).get("/me/key", headers={"Authorization": header})
    assert res.status_code == 401


def test_invalid_token_is_401() -> None:
    def bad(_token: str) -> dict[str, Any]:
        msg = "expired"
        raise ValueError(msg)

    res = _client(bad).get("/me/key", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401
    assert "nope" not in res.text


def test_public_routes_need_no_token() -> None:
    client = _client(_good)
    assert client.get("/health").status_code == 200


def test_valid_token_yields_uid(tmp_path: Path) -> None:
    seen: list[str] = []

    def verifier(token: str) -> dict[str, Any]:
        seen.append(token)
        return _good(token)

    res = _client(verifier, tmp_path).get("/me/key", headers={"Authorization": "bearer tok123"})
    assert seen == ["tok123"]
    assert res.status_code == 404  # signed in, but no key stored yet


@pytest.mark.parametrize(
    ("claims", "status"),
    [
        ({"sub": "u", "email": "a@example.com", "email_verified": True}, 200),
        ({"sub": "u", "email": "A@Example.com", "email_verified": True}, 200),
        ({"sub": "u", "email": "a@example.com", "email_verified": False}, 403),
        ({"sub": "u", "email": "eve@example.com", "email_verified": True}, 403),
        ({"sub": "u"}, 403),
    ],
)
def test_allowed_emails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claims: dict[str, Any], status: int) -> None:
    monkeypatch.setattr(settings, "allowed_emails", "a@example.com, b@example.com")
    res = _client(lambda _t: claims, tmp_path).get("/me/key", headers={"Authorization": "Bearer t"})
    assert res.status_code == (404 if status == 200 else status)  # 404 = let in, no key stored


def test_auth_disabled_lets_everyone_in_as_local_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False)

    def never(_token: str) -> dict[str, Any]:
        raise AssertionError("no token is verified with sign-in off")

    store = KeyStore(tmp_path / "keys.db", Keyring("k1", {"k1": b"secret"}))
    store.put("local", "AIza-local-key-1234")
    client = _client(never)
    app.dependency_overrides[get_key_store] = lambda: store
    res = client.get("/me/key")
    assert res.status_code == 200
    assert res.json()["last4"] == "1234"


def test_auth_disabled_seeds_the_local_key_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "google_api_key", SecretStr("AIza-developer-key-9876"))
    monkeypatch.setattr(settings, "key_db_path", tmp_path / "keys.db")
    monkeypatch.setattr(settings, "key_encryption_secret", SecretStr("secret"))
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get("/me/key").json()["last4"] == "9876"

    client.put("/me/key", json={"google_api_key": "AIza-saved-in-settings-5555"})
    assert client.get("/me/key").json()["last4"] == "5555"  # a key saved in Settings wins over .env


def test_auth_enabled_never_seeds_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "google_api_key", SecretStr("AIza-developer-key-9876"))
    monkeypatch.setattr(settings, "key_db_path", tmp_path / "keys.db")
    monkeypatch.setattr(settings, "key_encryption_secret", SecretStr("secret"))
    app.dependency_overrides[get_token_verifier] = lambda: _good
    res = TestClient(app).get("/me/key", headers={"Authorization": "Bearer t"})
    assert res.status_code == 404


def test_unconfigured_project_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "firebase_project_id", None)
    app.dependency_overrides.clear()
    res = TestClient(app).get("/me/key", headers={"Authorization": "Bearer t"})
    assert res.status_code == 503


class TestVerifyFirebase:
    """`verify_firebase` adds the issuer and `sub` checks the library skips."""

    @pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - applies to every test here
    def _project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "firebase_project_id", PROJECT)

    def _library(self, monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]) -> list[str | None]:
        audiences: list[str | None] = []

        def fake(_token: str, _request: object, audience: str | None = None) -> dict[str, Any]:
            audiences.append(audience)
            return claims

        monkeypatch.setattr(auth.id_token, "verify_firebase_token", fake)
        return audiences

    def test_accepts_good_claims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        audiences = self._library(monkeypatch, {"iss": ISS, "sub": "u1"})
        assert auth.verify_firebase("t")["sub"] == "u1"
        assert audiences == [PROJECT]

    def test_rejects_wrong_issuer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._library(monkeypatch, {"iss": "https://securetoken.google.com/other", "sub": "u1"})
        with pytest.raises(ValueError, match="issuer"):
            auth.verify_firebase("t")

    @pytest.mark.parametrize("sub", [None, ""])
    def test_rejects_empty_sub(self, monkeypatch: pytest.MonkeyPatch, sub: str | None) -> None:
        self._library(monkeypatch, {"iss": ISS, "sub": sub})
        with pytest.raises(ValueError, match="sub"):
            auth.verify_firebase("t")

    def test_no_project_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "firebase_project_id", None)
        with pytest.raises(RuntimeError):
            auth.verify_firebase("t")
