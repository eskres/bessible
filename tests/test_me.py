"""`/me/key`: save, show last4, delete, test. No response ever contains the key."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient

from bessible.api import me
from bessible.api.app import app
from bessible.auth import User, current_user
from bessible.keystore import Keyring, KeyStore, get_key_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

KEY = "AIza" + "S" * 31 + "wxyz"


@pytest.fixture
def store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.db", Keyring("k1", {"k1": b"secret"}))


@pytest.fixture
def client(store: KeyStore) -> Iterator[TestClient]:
    app.dependency_overrides[current_user] = lambda: User(uid="alice")
    app.dependency_overrides[get_key_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_save_then_read_back_last4_only(client: TestClient) -> None:
    put = client.put("/me/key", json={"google_api_key": KEY})
    assert put.status_code == 200
    got = client.get("/me/key")
    assert got.status_code == 200
    assert set(got.json()) == {"last4", "updated_at"}
    assert got.json()["last4"] == "wxyz"
    assert KEY not in put.text
    assert KEY not in got.text


def test_get_without_key_is_404(client: TestClient) -> None:
    assert client.get("/me/key").status_code == 404


def test_delete_removes_key(client: TestClient, store: KeyStore) -> None:
    client.put("/me/key", json={"google_api_key": KEY})
    assert client.delete("/me/key").status_code == 204
    assert client.get("/me/key").status_code == 404
    assert store.credentials("alice") is None
    assert client.delete("/me/key").status_code == 204  # idempotent


def test_keys_are_per_user(client: TestClient, store: KeyStore) -> None:
    client.put("/me/key", json={"google_api_key": KEY})
    app.dependency_overrides[current_user] = lambda: User(uid="bob")
    assert client.get("/me/key").status_code == 404
    assert store.reveal("alice") == KEY


@pytest.mark.parametrize(
    "body",
    [
        {"openai_api_key": "sk-" + "a" * 40},
        {"google_api_key": KEY, "modal_token": "ak-secret-token"},
        {"google_api_key": KEY, "anthropic_api_key": "sk-ant-secret"},
        {"google_api_key": "sk-" + "a" * 40},
        {"google_api_key": ""},
        {"google_api_key": 12345},
        {},
    ],
)
def test_only_a_google_key_is_accepted(client: TestClient, body: dict[str, Any]) -> None:
    res = client.put("/me/key", json=body)
    assert res.status_code == 422
    for value in body.values():
        if isinstance(value, str) and value:
            assert value not in res.text  # the error never echoes the submitted secret
    assert client.get("/me/key").status_code == 404


def test_put_wrong_content_never_echoed_for_non_object(client: TestClient) -> None:
    res = client.put("/me/key", json=["AIzaSECRETSECRETSECRETSECRETSECRETSECRET"])
    assert res.status_code == 422
    assert "SECRET" not in res.text


class TestKeyTest:
    @pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - applies to every test here
    def _ping(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        self.pinged: list[str] = []

        async def fake(api_key: str) -> me.KeyTestResult:
            self.pinged.append(api_key)
            return me.KeyTestResult(ok=True)

        monkeypatch.setattr(me, "ping_gemini", fake)
        return self.pinged

    def test_uses_key_in_body_without_saving(self, client: TestClient) -> None:
        res = client.post("/me/key/test", json={"google_api_key": KEY})
        assert res.json() == {"ok": True, "error": None}
        assert self.pinged == [KEY]
        assert client.get("/me/key").status_code == 404

    def test_uses_stored_key_without_body(self, client: TestClient) -> None:
        client.put("/me/key", json={"google_api_key": KEY})
        res = client.post("/me/key/test")
        assert res.json()["ok"] is True
        assert self.pinged == [KEY]
        assert KEY not in res.text

    def test_no_key_anywhere_is_404(self, client: TestClient) -> None:
        assert client.post("/me/key/test").status_code == 404
        assert self.pinged == []

    def test_body_rejects_other_providers(self, client: TestClient) -> None:
        assert client.post("/me/key/test", json={"openai_api_key": "sk-x"}).status_code == 422
        assert self.pinged == []


@pytest.mark.anyio
class TestPingGemini:
    """`ping_gemini` maps Gemini's answer to a short code and sends the key in a header, not the URL."""

    def _transport(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        real = httpx.AsyncClient
        monkeypatch.setattr(me.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(record), **kw))
        return seen

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(200, None), (400, "invalid_key"), (403, "invalid_key"), (429, "rate_limited"), (500, "provider_error")],
    )
    async def test_status_mapping(self, monkeypatch: pytest.MonkeyPatch, status: int, expected: str | None) -> None:
        seen = self._transport(monkeypatch, lambda _r: httpx.Response(status, json={"error": {"message": KEY}}))
        result = await me.ping_gemini(KEY)
        assert result.ok is (status == 200)
        assert result.error == expected
        assert KEY not in result.model_dump_json()
        assert seen[0].headers["x-goog-api-key"] == KEY
        assert KEY not in str(seen[0].url)

    async def test_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_r: httpx.Request) -> httpx.Response:
            msg = "down"
            raise httpx.ConnectError(msg)

        self._transport(monkeypatch, boom)
        assert (await me.ping_gemini(KEY)).error == "unreachable"


TAVILY = "tvly-" + "t" * 20 + "tavy"


def test_tavily_key_save_read_delete(client: TestClient, store: KeyStore) -> None:
    assert client.get("/me/tavily-key").status_code == 404
    put = client.put("/me/tavily-key", json={"tavily_api_key": TAVILY})
    assert put.status_code == 200
    got = client.get("/me/tavily-key")
    assert got.json()["last4"] == "tavy"
    assert TAVILY not in put.text
    assert TAVILY not in got.text
    assert store.reveal("alice", "tavily") == TAVILY
    assert client.get("/me/key").status_code == 404  # the Google key is separate
    assert client.delete("/me/tavily-key").status_code == 204
    assert client.get("/me/tavily-key").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"tavily_api_key": KEY},
        {"tavily_api_key": "tvly-short"},
        {"google_api_key": TAVILY},
        {"tavily_api_key": TAVILY, "google_api_key": KEY},
        {},
    ],
)
def test_only_a_tavily_key_is_accepted(client: TestClient, body: dict[str, Any]) -> None:
    res = client.put("/me/tavily-key", json=body)
    assert res.status_code == 422
    for value in body.values():
        assert value not in res.text
    assert client.get("/me/tavily-key").status_code == 404


class TestTavilyKeyTest:
    @pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - applies to every test here
    def _ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pinged: list[str] = []

        async def fake(api_key: str) -> me.KeyTestResult:
            self.pinged.append(api_key)
            return me.KeyTestResult(ok=True)

        monkeypatch.setattr(me, "ping_tavily", fake)

    def test_uses_key_in_body_without_saving(self, client: TestClient) -> None:
        res = client.post("/me/tavily-key/test", json={"tavily_api_key": TAVILY})
        assert res.json() == {"ok": True, "error": None}
        assert self.pinged == [TAVILY]
        assert client.get("/me/tavily-key").status_code == 404

    def test_uses_stored_key_without_body(self, client: TestClient) -> None:
        client.put("/me/tavily-key", json={"tavily_api_key": TAVILY})
        res = client.post("/me/tavily-key/test")
        assert res.json()["ok"] is True
        assert self.pinged == [TAVILY]
        assert TAVILY not in res.text

    def test_no_key_is_404(self, client: TestClient) -> None:
        assert client.post("/me/tavily-key/test").status_code == 404
        assert self.pinged == []


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "error"), [(200, None), (401, "invalid_key"), (429, "rate_limited")])
async def test_ping_tavily_reads_usage_status(monkeypatch: pytest.MonkeyPatch, status: int, error: str | None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(me.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = await me.ping_tavily(TAVILY)
    assert res.error == error
    assert res.ok is (error is None)
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/usage"
    assert seen[0].headers["authorization"] == f"Bearer {TAVILY}"
