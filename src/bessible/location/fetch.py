"""Web page fetching and caching for property link location extraction."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from bessible.api import tavily
from bessible.config import settings

if TYPE_CHECKING:
    from pathlib import Path

FETCH_TIMEOUT_S = 15.0
TAVILY_TIMEOUT_S = 30.0  # Tavily's own page timeout is 20 s, see `_fetch_with_tavily`
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_USER_AGENT = "Bessible/1.0 (+https://bessible.example.com; property site assessment bot)"


class PageUnavailable(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    """Raised when a property web page cannot be fetched, timed out, or returned an error."""


class UnsafeUrl(PageUnavailable):
    """Raised when a URL (or a redirect target) resolves to a non-public address (SSRF guard)."""


def _is_public_ip(ip_str: str) -> bool:
    """Return False for loopback, private, link-local (incl. cloud metadata), or reserved addresses."""
    addr = ipaddress.ip_address(ip_str)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _resolve_safe_ip(hostname: str) -> str:
    """Resolve hostname to one public IP, or raise `UnsafeUrl` if none of its addresses are public."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except OSError as exc:
        msg = f"Could not resolve host '{hostname}': {exc}"
        raise UnsafeUrl(msg) from exc
    for info in infos:
        ip = str(info[4][0])
        if _is_public_ip(ip):
            return ip
    msg = f"Host '{hostname}' resolves to a non-public address"
    raise UnsafeUrl(msg)


async def _pin_request(url: str) -> tuple[httpx.URL, str]:
    """Validate the URL is safe to fetch, returning an IP-pinned URL and the original hostname.

    A property link is fully attacker-controlled input fetched server-side from inside the
    Docker network, so this blocks SSRF into internal services (Temporal, other containers)
    and cloud metadata endpoints (e.g. 169.254.169.254). Connecting to the IP resolved here,
    rather than letting the URL's hostname resolve again at TCP-connect time, closes a
    DNS-rebinding gap: a host that answers with a public IP when checked but a different,
    internal IP moments later at the real connect. The original hostname still goes out as
    the `Host` header and (for https) the TLS SNI/cert-verification name, via the `sni_hostname`
    extension, so virtual hosting and certificate checks behave exactly as if we had not pinned.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        msg = f"Unsupported URL scheme in '{url}'"
        raise UnsafeUrl(msg)
    if not parsed.hostname:
        msg = f"URL has no host: '{url}'"
        raise UnsafeUrl(msg)
    ip = await _resolve_safe_ip(parsed.hostname)
    return httpx.URL(url).copy_with(host=ip), parsed.hostname


def _page_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


def page_cache_path(url: str) -> Path:
    """Return the committed fixture path for a URL (curated demo pages, added on purpose)."""
    return settings.data_dir / "fixtures" / "pages" / f"{_page_key(url)}.json"


def live_cache_path(url: str) -> Path:
    """Return the live cache path for a URL: gitignored, where every fetched page is written."""
    return settings.cache_dir / "pages" / f"{_page_key(url)}.json"


def cached_page_text(url: str) -> str | None:
    """The page's text from the committed fixtures, else the live cache; None when it was never fetched."""
    for path in (page_cache_path(url), live_cache_path(url)):
        if path.exists():
            return str(json.loads(path.read_text(encoding="utf-8")).get("text", ""))
    return None


def html_to_text(raw_html: str) -> str:
    """Extract readable text content from raw HTML."""
    clean = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", raw_html)
    clean = re.sub(r"(?s)<!--.*?-->", " ", clean)
    clean = re.sub(r"(?i)<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", clean)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    lines: list[str] = []
    for line in clean.splitlines():
        stripped = re.sub(r"\s+", " ", line).strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


async def _execute_fetch(url: str, client: httpx.AsyncClient | None) -> httpx.Response:
    """GET the URL, following redirects manually so each hop is pinned and re-checked.

    A naive one-time host check before an httpx `follow_redirects=True` GET can be bypassed by a
    server that responds 200 to the check but redirects to an internal address on the real request.
    """
    owns_client = client is None
    active = client if client is not None else httpx.AsyncClient(timeout=FETCH_TIMEOUT_S)
    try:
        current_url = url
        for _ in range(MAX_REDIRECTS + 1):
            pinned_url, hostname = await _pin_request(current_url)
            headers = {"User-Agent": DEFAULT_USER_AGENT, "Host": hostname}
            extensions = {"sni_hostname": hostname} if pinned_url.scheme == "https" else {}
            resp = await active.get(pinned_url, follow_redirects=False, headers=headers, extensions=extensions)
            if not resp.is_redirect:
                return resp
            location = resp.headers.get("location")
            if not location:
                return resp
            current_url = str(httpx.URL(current_url).join(location))
        msg = f"Too many redirects fetching '{url}'"
        raise PageUnavailable(msg)
    finally:
        if owns_client:
            await active.aclose()


async def _fetch_direct(url: str, client: httpx.AsyncClient | None) -> str:
    """GET the page ourselves and return its text. Raises `PageUnavailable` (or `UnsafeUrl`)."""
    try:
        resp = await _execute_fetch(url, client)
    except UnsafeUrl:
        raise
    except Exception as exc:
        msg = f"Could not fetch property page at '{url}': {exc}"
        raise PageUnavailable(msg) from exc

    if resp.status_code >= HTTPStatus.BAD_REQUEST:
        msg = f"HTTP {resp.status_code} fetching page '{url}'"
        raise PageUnavailable(msg)

    content_type = resp.headers.get("content-type", "")
    return html_to_text(resp.text) if ("html" in content_type or "<html" in resp.text[:500].lower()) else resp.text


async def _fetch_with_tavily(url: str, client: httpx.AsyncClient | None) -> str:
    """The page text through Tavily's Extract API (1 credit), for portals that block server IPs.

    Savills answers 403 to data-centre addresses, so a deployed server cannot read a listing a laptop can.
    Raises `PageUnavailable` without an operator `TAVILY_API_KEY`, or when Tavily cannot fetch the page either.
    """
    key = settings.tavily_api_key
    if key is None:
        msg = "no TAVILY_API_KEY for the fallback"
        raise PageUnavailable(msg)
    req = tavily.ExtractRequest(urls=[url], extract_depth="basic", format="text", timeout=20)
    owns_client = client is None
    active = client if client is not None else httpx.AsyncClient(timeout=TAVILY_TIMEOUT_S)
    try:
        resp = await active.post(req.URL, json=req.params(), headers=tavily.auth_headers(key.get_secret_value()))
    except Exception as exc:
        msg = f"Tavily extract failed: {exc}"
        raise PageUnavailable(msg) from exc
    finally:
        if owns_client:
            await active.aclose()
    if resp.status_code != HTTPStatus.OK:
        msg = f"Tavily extract answered HTTP {resp.status_code}"
        raise PageUnavailable(msg)
    body = tavily.ExtractResponse.model_validate(resp.json())
    text = next((r.raw_content for r in body.results if r.raw_content.strip()), None)
    if text is None:
        why = body.failed_results[0].error if body.failed_results else "no page text"
        msg = f"Tavily could not fetch the page: {why}"
        raise PageUnavailable(msg)
    return text


async def fetch_page_text(url: str, *, client: httpx.AsyncClient | None = None) -> str:
    """Fetch property page text with URL-keyed disk caching.

    A committed fixture (data/fixtures/pages/<sha1>.json), else the live cache (out/cache/pages/<sha1>.json), is
    returned without making any network requests. Else the page is fetched directly, and when that fails (e.g. a
    portal's 403 to a server IP) through Tavily. Fetched pages are written to the live cache only.

    Raises:
        PageUnavailable: if neither a direct fetch nor Tavily can read the page.
        UnsafeUrl: if the URL points at a non-public address (never retried through Tavily).
    """
    cached = cached_page_text(url)
    if cached is not None:
        return cached

    try:
        text = await _fetch_direct(url, client)
    except UnsafeUrl:
        raise
    except PageUnavailable as direct:
        try:
            text = await _fetch_with_tavily(url, client)
        except PageUnavailable as fallback:
            msg = f"{direct} (fallback: {fallback})"
            raise PageUnavailable(msg) from direct

    cache = live_cache_path(url)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "text": text,
    }
    cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return text
