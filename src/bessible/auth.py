"""Firebase sign-in: verify the ID token on every non-demo request and yield who is calling.

With `AUTH_ENABLED=false` (local dev) there is no sign-in: every caller is `LOCAL_USER`.

Uses `google-auth`, not `firebase-admin`: `verify_firebase_token` needs no service-account file, it only fetches
Google's public certs (cached here) and checks signature, expiry and audience. The issuer and `sub` are checked
explicitly because the library does not.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Annotated, Any

import cachecontrol
import requests
from fastapi import Depends, Header, HTTPException
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from pydantic import BaseModel

from bessible.config import settings

TokenVerifier = Callable[[str], dict[str, Any]]  # a runtime alias: FastAPI resolves it in `current_user`'s signature
ISSUER_PREFIX = "https://securetoken.google.com/"
BEARER = "bearer "


class User(BaseModel):
    """The signed-in caller. `uid` owns runs and the stored key."""

    uid: str
    email: str | None = None


LOCAL_USER = User(uid="local")


@functools.cache
def _certs_request() -> Request:
    """One HTTP session for all verifications, wrapped so Google's certs are cached instead of fetched per call."""
    return Request(session=cachecontrol.CacheControl(requests.Session()))


def verify_firebase(token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its claims. Blocking: call it off the event loop.

    Raises:
        ValueError: the token is invalid, expired, for another project or issuer, or has no `sub`.
        RuntimeError: `FIREBASE_PROJECT_ID` is not set.
    """
    project = settings.firebase_project_id
    if not project:
        msg = "FIREBASE_PROJECT_ID is not set"
        raise RuntimeError(msg)
    claims: dict[str, Any] = id_token.verify_firebase_token(  # type: ignore[no-untyped-call]
        token, _certs_request(), audience=project
    )
    if claims.get("iss") != f"{ISSUER_PREFIX}{project}":
        msg = "wrong issuer"
        raise ValueError(msg)
    if not claims.get("sub"):
        msg = "missing sub"
        raise ValueError(msg)
    return claims


def get_token_verifier() -> TokenVerifier:
    """FastAPI dependency: the verifier function (tests override this with a fake)."""
    return verify_firebase


def _allowed(claims: dict[str, Any]) -> bool:
    """True if `ALLOWED_EMAILS` is unset, or the token carries a verified email on the list."""
    if not settings.allowed_emails:
        return True
    allowed = {e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()}
    email = str(claims.get("email", "")).lower()
    return bool(claims.get("email_verified")) and email in allowed


async def current_user(
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """FastAPI dependency for every non-demo route: 401 without a valid Firebase ID token."""
    if not settings.auth_enabled:
        return LOCAL_USER
    unauthorized = HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
    if authorization is None or not authorization.lower().startswith(BEARER):
        raise unauthorized
    token = authorization[len(BEARER) :].strip()
    try:
        claims = await asyncio.to_thread(verifier, token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="sign-in is not configured") from exc
    except (ValueError, GoogleAuthError) as exc:
        raise unauthorized from exc
    if not _allowed(claims):
        raise HTTPException(status_code=403, detail="this account is not allowed")
    return User(uid=str(claims["sub"]), email=claims.get("email"))
