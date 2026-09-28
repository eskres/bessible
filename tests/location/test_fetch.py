"""Tests for location/fetch.py page fetching, caching, and text extraction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from bessible.location.fetch import (
    PageUnavailable,
    UnsafeUrl,
    fetch_page_text,
    html_to_text,
    live_cache_path,
    page_cache_path,
)


def test_html_to_text():
    html_doc = """
    <html>
      <head>
        <title>Property Title</title>
        <style>body { color: red; }</style>
        <script>console.log("secret");</script>
      </head>
      <body>
        <h1>Land at Milton Park</h1>
        <p>A prime development site located at Sutton Courtenay, Oxfordshire, OX14 4TE.</p>
        <div>Coordinates: 51.6256, -1.2756</div>
      </body>
    </html>
    """
    text = html_to_text(html_doc)
    assert "Land at Milton Park" in text
    assert "OX14 4TE" in text
    assert "51.6256, -1.2756" in text
    assert "console.log" not in text
    assert "color: red" not in text


@pytest.mark.anyio
async def test_fetch_page_text_caching(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)

    test_url = "https://example.com/property/123"
    html_content = "<html><body><h1>Property at Didcot</h1><p>Postcode OX14 4TE</p></body></html>"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        status_code=200,
        content=html_content.encode("utf-8"),
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", test_url),
    )
    mock_client.get.return_value = mock_resp

    # First call: fetches via client
    text_1 = await fetch_page_text(test_url, client=mock_client)
    assert "Property at Didcot" in text_1
    assert "OX14 4TE" in text_1
    assert mock_client.get.call_count == 1

    # Verify the page went to the live cache, never to the committed fixtures
    cache_file = live_cache_path(test_url)
    assert cache_file.exists()
    assert not page_cache_path(test_url).exists()
    cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached_data["url"] == test_url
    assert "Property at Didcot" in cached_data["text"]

    # Second call: client is never called (network off simulation)
    failing_client = AsyncMock(spec=httpx.AsyncClient)
    failing_client.get.side_effect = RuntimeError("Network is disconnected!")

    text_2 = await fetch_page_text(test_url, client=failing_client)
    assert text_2 == text_1
    assert failing_client.get.call_count == 0


@pytest.mark.anyio
async def test_fetch_page_text_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    test_url = "https://example.com/nonexistent"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        status_code=404,
        request=httpx.Request("GET", test_url),
    )
    mock_client.get.return_value = mock_resp

    with pytest.raises(PageUnavailable, match="HTTP 404"):
        await fetch_page_text(test_url, client=mock_client)


@pytest.mark.anyio
async def test_fetch_page_text_network_error(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    test_url = "https://example.com/broken"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(PageUnavailable, match="Could not fetch"):
        await fetch_page_text(test_url, client=mock_client)


@pytest.mark.anyio
async def test_fetch_page_text_rejects_bad_scheme(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)

    with pytest.raises(UnsafeUrl):
        await fetch_page_text("file:///etc/passwd")


@pytest.mark.anyio
async def test_fetch_page_text_rejects_loopback_ip_literal(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)

    with pytest.raises(UnsafeUrl):
        await fetch_page_text("http://127.0.0.1:8000/internal")


@pytest.mark.anyio
async def test_fetch_page_text_rejects_cloud_metadata_ip(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)

    with pytest.raises(UnsafeUrl):
        await fetch_page_text("http://169.254.169.254/latest/meta-data/")


@pytest.mark.anyio
async def test_fetch_page_text_rejects_redirect_to_internal_address(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    test_url = "https://example.com/redirector"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    redirect_resp = httpx.Response(
        status_code=302,
        headers={"location": "http://127.0.0.1:8000/internal"},
        request=httpx.Request("GET", test_url),
    )
    mock_client.get.return_value = redirect_resp

    with pytest.raises(UnsafeUrl):
        await fetch_page_text(test_url, client=mock_client)


def _blocked_client(url: str, tavily_body: dict[str, object]) -> AsyncMock:
    """A portal that answers 403 to a direct GET, and a Tavily extract that answers `tavily_body`."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(status_code=403, request=httpx.Request("GET", url))
    client.post.return_value = httpx.Response(
        status_code=200, json=tavily_body, request=httpx.Request("POST", "https://api.tavily.com/extract")
    )
    return client


@pytest.mark.anyio
async def test_fetch_page_text_falls_back_to_tavily_on_403(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    monkeypatch.setattr("bessible.config.settings.tavily_api_key", SecretStr("tvly-test"))
    test_url = "https://example.com/property/blocked"
    body = {"results": [{"url": test_url, "raw_content": "Land at Milton Park, OX14 4TE"}], "failed_results": []}
    client = _blocked_client(test_url, body)

    text = await fetch_page_text(test_url, client=client)

    assert text == "Land at Milton Park, OX14 4TE"
    assert client.post.call_args.kwargs["json"]["urls"] == [test_url]
    assert live_cache_path(test_url).exists()  # the next run reads the cache, not Tavily


@pytest.mark.anyio
async def test_fetch_page_text_reports_both_failures(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    monkeypatch.setattr("bessible.config.settings.tavily_api_key", SecretStr("tvly-test"))
    test_url = "https://example.com/property/gone"
    client = _blocked_client(test_url, {"results": [], "failed_results": [{"url": test_url, "error": "Timed out"}]})

    with pytest.raises(PageUnavailable, match=r"HTTP 403.*fallback: Tavily could not fetch the page: Timed out"):
        await fetch_page_text(test_url, client=client)


@pytest.mark.anyio
async def test_fetch_page_text_never_sends_unsafe_urls_to_tavily(monkeypatch, tmp_path):
    monkeypatch.setattr("bessible.config.settings.data_dir", tmp_path)
    monkeypatch.setattr("bessible.config.settings.tavily_api_key", SecretStr("tvly-test"))
    client = AsyncMock(spec=httpx.AsyncClient)

    with pytest.raises(UnsafeUrl):
        await fetch_page_text("http://169.254.169.254/latest/meta-data/", client=client)
    client.post.assert_not_called()
