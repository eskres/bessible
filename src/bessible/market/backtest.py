"""Back-test the modelled stack against published GB BESS revenue for the same period.

uv run python -m bessible.market.backtest

Runs the live wholesale and ancillary sources as of the day after the benchmark period ends, splits the MW between
them with the stack's own `split_mw`, and prints the gap to the benchmark (`revenue_benchmark` in `market.json`) with
the arithmetic, for the `benchmark_calibration` entry.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING

from bessible.market import load_market_assumptions
from bessible.market.live import ElexonArbitrageSource, NesoResponseSource
from bessible.market.stack import split_mw, total

if TYPE_CHECKING:
    from bessible.market.sources import RevenueSource
    from bessible.models import StreamValue

BENCHMARK_LAST_DAY = date(2026, 4, 30)  # Modo: "over the twelve months to April 2026"


async def merchant_rows(wholesale: RevenueSource, ancillary: RevenueSource, duration_h: int) -> list[StreamValue]:
    """Wholesale and ancillary for one duration, with the MW split between them as in the stack."""
    return split_mw([await ancillary.fetch(duration_h), await wholesale.fetch(duration_h)])


async def main() -> None:
    """Print modelled vs benchmark for the benchmark's period."""
    a = load_market_assumptions()
    bench = a.mapping("revenue_benchmark")
    as_of = BENCHMARK_LAST_DAY + timedelta(days=1)
    wholesale = ElexonArbitrageSource(a, today=as_of)
    ancillary = NesoResponseSource(today=as_of)
    target = bench["total_gbp_per_mw_year"] - bench["capacity_market_gbp_per_mw_year"]
    for d in (2, 4, 8):
        rows = await merchant_rows(wholesale, ancillary, d)
        w = next(r for r in rows if r.stream == "wholesale")
        anc = next(r for r in rows if r.stream == "balancing_ancillary")
        modelled = total(rows)
        line = (
            f"{d}h modelled, without Capacity Market: wholesale central {w.gbp_per_mw_year:,.0f} "
            f"({w.mw_share or 1:.3f} of the MW; upper bound {w.upper_bound_gbp_per_mw_year or 0:,.0f}) + ancillary "
            f"{anc.gbp_per_mw_year:,.0f} = {modelled:,.0f}"
        )
        if d == int(bench["duration_h"]):
            line += (
                f"; benchmark without Capacity Market: {bench['total_gbp_per_mw_year']:,.0f} - "
                f"{bench['capacity_market_gbp_per_mw_year']:,.0f} = {target:,.0f}; gap {target - modelled:,.0f} "
                f"({modelled / target:.0%} of the benchmark)"
            )
        print(line)  # ruff: ignore[print]
    print(f"periods: wholesale {w.period}; ancillary {anc.period}")  # ruff: ignore[print]


if __name__ == "__main__":
    asyncio.run(main())
