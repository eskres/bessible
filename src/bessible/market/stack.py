"""Per-duration revenue stack: sourced streams, Capacity Market de-rating, the MW split, calibration, support."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.market.support import qualifying, support_stream
from bessible.models import REQUIRED_DURATION_HOURS, StreamValue

if TYPE_CHECKING:
    from bessible.assumptions import AssumptionSet
    from bessible.market.sources import RevenueSource

CALIBRATION = "benchmark_calibration"


def split_mw(rows: list[StreamValue]) -> list[StreamValue]:
    """Give wholesale only the MW that frequency response does not hold: wholesale share = 1 - ancillary share.

    Revenue and upper bound are scaled together, so the shares of the MW add up to at most 100%.
    """
    ancillary = next((r for r in rows if r.stream == "balancing_ancillary" and r.mw_share is not None), None)
    if ancillary is None or ancillary.mw_share is None:
        return rows
    left = max(0.0, 1.0 - ancillary.mw_share)
    out = []
    for r in rows:
        if r.stream == "wholesale":
            bound = r.upper_bound_gbp_per_mw_year
            r = r.model_copy(  # ruff: ignore[redefined-loop-name]
                update={
                    "gbp_per_mw_year": r.gbp_per_mw_year * left,
                    "upper_bound_gbp_per_mw_year": None if bound is None else bound * left,
                    "mw_share": left,
                }
            )
        out.append(r)
    return out


def calibration_stream(a: AssumptionSet) -> StreamValue:
    """Revenue the model does not trade (Balancing Mechanism, intraday), sized by the back-test against a benchmark."""
    entry = a.entry(CALIBRATION)
    return StreamValue(
        stream=CALIBRATION,
        gbp_per_mw_year=a.mapping(CALIBRATION)["gbp_per_mw_year"],
        source=entry.source,
        source_url=HttpUrl(entry.source_url or ""),
        as_of=date.fromisoformat(entry.published or entry.date),
        cached=False,
        placeholder=entry.status == "placeholder",
        scheme="Balancing Mechanism and intraday (benchmark calibration)",
        method=entry.derivation,
    )


async def revenue_stack(mw: float, sources: list[RevenueSource], a: AssumptionSet) -> dict[int, list[StreamValue]]:
    """Return the streams for each of 2, 4 and 8 hours. Capacity Market is scaled by the de-rating factor."""
    derating = a.mapping("capacity_market_derating")
    stack: dict[int, list[StreamValue]] = {}
    for duration_h in REQUIRED_DURATION_HOURS:
        rows: list[StreamValue] = []
        for source in sources:
            value = await source.fetch(duration_h)
            if value.stream == "capacity_market":
                factor = derating[str(duration_h)]
                value = value.model_copy(update={"gbp_per_mw_year": value.gbp_per_mw_year * factor})
            rows.append(value)
        rows = split_mw(rows)
        rows.append(calibration_stream(a))
        rows.extend(support_stream(name, a) for name in qualifying(duration_h, mw, a))
        stack[duration_h] = rows
    return stack


def total(rows: list[StreamValue]) -> float:
    """Sum of stream revenue in GBP per MW per year."""
    return sum(r.gbp_per_mw_year for r in rows)
