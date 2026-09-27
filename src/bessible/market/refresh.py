"""Rewrite the live-backed fixtures from a live run, so the fallback is a real, dated snapshot.

uv run python -m bessible.market.refresh
"""

from __future__ import annotations

import asyncio
import json

from bessible.market import load_market_assumptions
from bessible.market.live import ElexonArbitrageSource, LiveFigure, NesoResponseSource
from bessible.market.sources import MARKET_FIXTURES


def write_fixture(figure: LiveFigure) -> None:
    """Write one stream's fixture from its live figure."""
    body = figure.model_dump(mode="json")
    body["status"] = "agreed"
    body["note"] = (
        f"Snapshot of the live computation on {figure.as_of.isoformat()} (bessible.market.refresh). "
        "Served only when the live source fails, and then flagged cached."
    )
    path = MARKET_FIXTURES / f"{figure.stream}.json"
    if path.exists():  # a cross-check is written by hand; keep it across refreshes
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("cross_check"):
            body["cross_check"] = old["cross_check"]
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path}: {figure.gbp_per_mw_year_by_duration}")  # ruff: ignore[print]


async def main() -> None:
    """Compute both live streams and write their fixtures."""
    write_fixture(await ElexonArbitrageSource(load_market_assumptions()).figure())
    write_fixture(await NesoResponseSource().figure())


if __name__ == "__main__":
    asyncio.run(main())
