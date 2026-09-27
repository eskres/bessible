"""Stored model outputs, so replaying the same input gives the same answer.

Gemini's paragraph picks and labels vary between runs on identical input, and the opposition index moves with them.
Each output is stored under `settings.cache_dir/<kind>/<key>.json` (gitignored), keyed by a hash of everything
that shaped it (prompt, schema, paragraph text). `record` copies an entry to `data/recorded/<kind>/`, committed
next to the Tavily recordings, so the offline demo replays the same picks, labels and index.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from bessible.config import settings

log = logging.getLogger(__name__)

RECORDED_ROOT = settings.data_dir / "recorded"


def key(*parts: Any) -> str:  # ruff: ignore[any-type]
    """A stable hash of the inputs that shaped an output."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _paths(kind: str, k: str) -> tuple[Path, Path]:
    return settings.cache_dir / kind / f"{k}.json", RECORDED_ROOT / kind / f"{k}.json"


def load(kind: str, k: str) -> dict[str, Any] | None:
    """The stored output: the local cache first, then a committed recording. None when neither has it."""
    for path in _paths(kind, k):
        if path.exists():
            try:
                entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                return entry
            except (OSError, ValueError) as e:
                log.warning("Ignoring unreadable stored output %s: %s", path, e)
    return None


def save(kind: str, k: str, data: dict[str, Any]) -> None:
    """Store an output in the local cache. A read-only checkout still runs, it just does not store."""
    path = _paths(kind, k)[0]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not store %s output: %s", kind, e)


def record(kind: str, k: str) -> bool:
    """Copy a stored output into the committed recordings. False when this run did not store it."""
    cached, recorded = _paths(kind, k)
    if not cached.exists():
        return False
    recorded.parent.mkdir(parents=True, exist_ok=True)
    recorded.write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
    return True
