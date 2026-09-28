"""The signed-in user's keys: save, delete, show `last4`, test. A key is never returned once saved.

`/me/key` is the Google key every run needs. `/me/tavily-key` is optional: runs use it for Tavily news search and
page fetches instead of the server's `TAVILY_API_KEY`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any  # ruff: ignore[typing-only-standard-library-import]

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel

from bessible.api import tavily
from bessible.auth import User, current_user
from bessible.config import settings
from bessible.keystore import KeyMeta, KeyStore, KeyStoreError, Provider, get_key_store

router = APIRouter(prefix="/me", tags=["me"])

GOOGLE_KEY = re.compile(r"^(AIza|AQ\.)[0-9A-Za-z_.-]{20,}$")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FIELD = "google_api_key"
TAVILY_FIELD = "tavily_api_key"
TAVILY_KEY = re.compile(r"^tvly-[0-9A-Za-z_-]{16,}$")


class KeyTestResult(BaseModel):
    """Outcome of a key ping. `error` is a short code, never provider text (which could echo the key)."""

    ok: bool
    error: str | None = None


ACCEPTED_FIELDS = {FIELD, "google_key"}


def _google_key(body: dict[str, Any]) -> str:
    """Accept exactly one credential, a Google AI key. Errors never echo the submitted value."""
    if len(body) != 1 or not (set(body) & ACCEPTED_FIELDS):
        raise HTTPException(
            status_code=422, detail=f"Send exactly one field, {FIELD}. No other provider or token is accepted."
        )
    value = next(v for k, v in body.items() if k in ACCEPTED_FIELDS)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{FIELD} must be a non-empty string.")
    cleaned = value.strip()
    # Basic sanity check: reject other providers (e.g. OpenAI sk-..., Anthropic sk-ant-...) and control chars
    if cleaned.startswith(("sk-ant-", "sk-proj-", "sk-")) or len(cleaned) < 20 or any(c.isspace() for c in cleaned):
        raise HTTPException(status_code=422, detail=f"{FIELD} is not a valid Google AI API key.")
    return cleaned


async def ping_gemini(api_key: str) -> KeyTestResult:
    """Cheapest real call: one output token. The key goes in a header, not the URL."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            res = await http.post(
                GEMINI_URL.format(model=settings.gemini_model),
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
            )
    except httpx.HTTPError:
        return KeyTestResult(ok=False, error="unreachable")
    return _result(res)


async def ping_tavily(api_key: str) -> KeyTestResult:
    """`GET /usage`: proves the key without spending a search credit. The key goes in a header."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            res = await http.get(f"{tavily.BASE_URL}/usage", headers=tavily.auth_headers(api_key))
    except httpx.HTTPError:
        return KeyTestResult(ok=False, error="unreachable")
    return _result(res)


def _result(res: httpx.Response) -> KeyTestResult:
    if res.is_success:
        return KeyTestResult(ok=True)
    if res.status_code == httpx.codes.TOO_MANY_REQUESTS:
        return KeyTestResult(ok=False, error="rate_limited")  # the key is valid but out of quota
    if res.status_code in {httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        return KeyTestResult(ok=False, error="invalid_key")
    return KeyTestResult(ok=False, error="provider_error")


def _tavily_key(body: dict[str, Any]) -> str:
    """Accept exactly one field, a Tavily key (`tvly-...`). Errors never echo the submitted value."""
    if set(body) != {TAVILY_FIELD}:
        raise HTTPException(status_code=422, detail=f"Send exactly one field, {TAVILY_FIELD}.")
    value = body[TAVILY_FIELD]
    if not isinstance(value, str) or not TAVILY_KEY.match(value.strip()):
        raise HTTPException(status_code=422, detail=f"{TAVILY_FIELD} is not a valid Tavily API key (tvly-...).")
    return value.strip()


@router.get("/key", response_model=KeyMeta)
def get_key(
    user: Annotated[User, Depends(current_user)], store: Annotated[KeyStore, Depends(get_key_store)]
) -> KeyMeta:
    """Return `last4` and the update time. 404 if the user has no key."""
    meta = store.meta(user.uid)
    if meta is None:
        raise HTTPException(status_code=404, detail="no_key")
    return meta


@router.put("/key", response_model=KeyMeta)
def put_key(
    body: Annotated[dict[str, Any], Body()],
    user: Annotated[User, Depends(current_user)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> KeyMeta:
    """Encrypt and store the user's Google key, replacing any earlier one."""
    return store.put(user.uid, _google_key(body))


@router.delete("/key", status_code=204)
def delete_key(
    user: Annotated[User, Depends(current_user)], store: Annotated[KeyStore, Depends(get_key_store)]
) -> Response:
    """Delete the stored key. Later runs return `401 missing_google_key`."""
    store.delete(user.uid)
    return Response(status_code=204)


@router.post("/key/test", response_model=KeyTestResult)
async def check_key(
    user: Annotated[User, Depends(current_user)],
    store: Annotated[KeyStore, Depends(get_key_store)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> KeyTestResult:
    """Ping Gemini with the key in the body (before saving) or, with no body, the stored key."""
    api_key = _google_key(body) if body else await _stored(store, user.uid, "google")
    return await ping_gemini(api_key)


async def _stored(store: KeyStore, uid: str, provider: Provider) -> str:
    try:
        api_key = await asyncio.to_thread(store.reveal, uid, provider)
    except KeyStoreError as exc:
        raise HTTPException(status_code=409, detail="stored_key_unreadable") from exc
    if api_key is None:
        raise HTTPException(status_code=404, detail="no_key")
    return api_key


@router.get("/tavily-key", response_model=KeyMeta)
def get_tavily_key(
    user: Annotated[User, Depends(current_user)], store: Annotated[KeyStore, Depends(get_key_store)]
) -> KeyMeta:
    """Return `last4` and the update time of the user's Tavily key. 404 if they have none."""
    meta = store.meta(user.uid, "tavily")
    if meta is None:
        raise HTTPException(status_code=404, detail="no_key")
    return meta


@router.put("/tavily-key", response_model=KeyMeta)
def put_tavily_key(
    body: Annotated[dict[str, Any], Body()],
    user: Annotated[User, Depends(current_user)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> KeyMeta:
    """Encrypt and store the user's Tavily key, replacing any earlier one. Later runs use it."""
    return store.put(user.uid, _tavily_key(body), "tavily")


@router.delete("/tavily-key", status_code=204)
def delete_tavily_key(
    user: Annotated[User, Depends(current_user)], store: Annotated[KeyStore, Depends(get_key_store)]
) -> Response:
    """Delete the stored Tavily key. Later runs fall back to the server's key, if it has one."""
    store.delete(user.uid, "tavily")
    return Response(status_code=204)


@router.post("/tavily-key/test", response_model=KeyTestResult)
async def check_tavily_key(
    user: Annotated[User, Depends(current_user)],
    store: Annotated[KeyStore, Depends(get_key_store)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> KeyTestResult:
    """Ping Tavily with the key in the body (before saving) or, with no body, the stored key."""
    api_key = _tavily_key(body) if body else await _stored(store, user.uid, "tavily")
    return await ping_tavily(api_key)
