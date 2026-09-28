"""Check the dev environment is ready: secrets, Temporal server, Modal login.

uv run python scripts/check_env.py          # config only
uv run python scripts/check_env.py --live   # also send one tiny prompt to Gemini and the Modal model
"""

from __future__ import annotations

import asyncio
import sys
from importlib.util import find_spec

from pydantic_ai import Agent
from temporalio.client import Client

from bessible import llm
from bessible.classifier import modal_enabled
from bessible.config import settings


def check(name: str, ok: bool, hint: str = "", required: bool = True) -> bool:
    label = "OK " if ok else ("MISSING" if required else "skip")
    print(f"{label:8} {name}" + ("" if ok else f"  ({hint})"))
    return ok or not required


async def temporal_reachable() -> bool:
    try:
        await asyncio.wait_for(
            Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), timeout=3
        )
        return True
    except Exception:
        return False


def ping(name: str, make_model) -> bool:
    try:
        out = Agent(make_model()).run_sync("Reply with exactly: pong").output
        return check(f"{name} replies", "pong" in out.lower(), f"got {out[:60]!r}")
    except Exception as e:
        return check(f"{name} replies", False, f"{type(e).__name__}: {str(e)[:120]}")


def modal_checks() -> list[bool]:
    """Modal is the classifier's optional second opinion: only check the login when a token is configured."""
    if find_spec("modal") is None:
        return [check("Modal classifier", False, "optional: `modal` not installed, classifying with Gemini", False)]
    if (settings.modal_token_id is None) != (settings.modal_token_secret is None):
        return [check("MODAL_TOKEN_ID + MODAL_TOKEN_SECRET", False, "set both or neither")]
    return [check("Modal login", modal_enabled(), "optional: set the Modal token or run `uv run modal setup`", False)]


def main() -> None:
    results = [
        check("FIREBASE_PROJECT_ID", settings.firebase_project_id is not None, "set it in .env"),
        check("GOOGLE_API_KEY", settings.google_api_key is not None, "set it in .env"),
        check("KEY_ENCRYPTION_SECRET", settings.key_encryption_secret is not None, "set it in .env (see .env.example)"),
        check("PYDANTIC_AI_GATEWAY_API_KEY", settings.pydantic_ai_gateway_api_key is not None, "set it in .env"),
        check("LOGFIRE_TOKEN", settings.logfire_token is not None, "optional: tracing", required=False),
        check("TYPESAFE_API_KEY", settings.typesafe_api_key is not None, "optional: Jev", required=False),
        check(
            "HMLR_API_KEY",
            settings.hmlr_api_key is not None,
            "optional: CCOD / OCOD title numbers (then `uv run python scripts/hmlr_ownership.py`)",
            required=False,
        ),
        check(
            f"Temporal server at {settings.temporal_address}",
            asyncio.run(temporal_reachable()),
            "run `temporal server start-dev` in another terminal",
        ),
        *modal_checks(),
    ]
    if "--live" in sys.argv:
        llm.setup_logfire()
        results += [
            ping(f"Gemini ({settings.gemini_model})", llm.developer_model),
            ping(f"Modal via gateway ({settings.modal_model})", llm.modal_model),
        ]
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
