"""Market revenue projections stage: sourced streams, de-rating, and long-duration support."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import HttpUrl

from bessible.assumptions import AssumptionSet
from bessible.market import load_market_assumptions
from bessible.market.sources import default_sources
from bessible.market.stack import CALIBRATION, revenue_stack, total
from bessible.models import Artifact, DataGap, MarketOutput, NodeInput, StreamValue


async def market_revenue(inp: NodeInput) -> MarketOutput:
    """Project revenue streams across ancillary services, trading, capacity market, and support schemes."""
    a = load_market_assumptions()
    sources = default_sources()
    mw = inp.site.capacity_mw

    stack = await revenue_stack(mw, sources, a)
    four_hour_rows = stack.get(4, [])
    revenue_4h = total(four_hour_rows)
    streams_4h = {r.stream: r.gbp_per_mw_year for r in four_hour_rows}

    # Generate one artifact per stream across all durations
    seen_streams: set[str] = set()
    artifacts: list[Artifact] = []

    # Priority order of durations to select stream values from
    for duration_h in (4, 2, 8):
        for val in stack.get(duration_h, []):
            if val.stream in seen_streams:
                continue
            seen_streams.add(val.stream)

            artifacts.append(
                Artifact(
                    id=f"market-{val.stream}-{inp.run_id[:8]}",
                    stage="market",
                    claim=_claim(val, duration_h),
                    source_url=val.source_url,
                    confidence=_confidence(val),
                    model_used="market-assumptions",
                )
            )
    artifacts.append(_benchmark_artifact(stack, a, inp.run_id))

    return MarketOutput(
        revenue_gbp_per_mw_year=revenue_4h,
        streams=streams_4h,
        by_duration=stack,
        artifacts=artifacts,
        gaps=_gaps(stack),
    )


def _gaps(stack: dict[int, list[StreamValue]]) -> list[DataGap]:
    """One gap per stream served from a fallback: a failed live source (retryable) or a placeholder figure."""
    gaps: dict[str, DataGap] = {}
    for val in (v for rows in stack.values() for v in rows):
        if val.stream in gaps:
            continue
        if val.cached and val.stream in LIVE_STREAMS:
            reason = f"Live source failed; used the cached snapshot from {val.as_of.isoformat()}."
            gaps[val.stream] = DataGap(
                stage="market", what=val.stream, reason=reason, sources=[val.source], retryable=True
            )
        elif val.placeholder:
            reason = "Placeholder figure: no published value yet."
            gaps[val.stream] = DataGap(stage="market", what=val.scheme or val.stream, reason=reason)
    return list(gaps.values())


# How far the method itself can be trusted, before live/cached/placeholder: a published auction price is exact; the
# wholesale dispatch is computed on a full year of prices but plans one cycle a day on a simple forecast; ancillary
# rests on an estimated participation share; the calibration is one 2h benchmark, carried to 4h and 8h.
METHOD_CONFIDENCE = {"capacity_market": 0.95, "wholesale": 0.75, "balancing_ancillary": 0.6, CALIBRATION: 0.5}
CACHED_PENALTY = 0.1
STALE_DAYS = 90  # a snapshot older than this loses another STALE_PENALTY
STALE_PENALTY = 0.1
PLACEHOLDER_CONFIDENCE = 0.3
LIVE_STREAMS = {"wholesale", "balancing_ancillary"}  # the rest are committed by design, so "cached" is expected


def _confidence(val: StreamValue) -> float:
    """Confidence from the method, lowered for a cached snapshot and again for a stale one, floored for a placeholder."""
    if val.placeholder:
        return PLACEHOLDER_CONFIDENCE
    conf = METHOD_CONFIDENCE.get(val.stream, 0.8)
    if val.cached and val.stream in LIVE_STREAMS:
        conf -= CACHED_PENALTY
        if (datetime.now(UTC).date() - val.as_of).days > STALE_DAYS:
            conf -= STALE_PENALTY
    return round(conf, 2)


def _claim(val: StreamValue, duration_h: int) -> str:
    """Say the central figure, live or cached, the upper bound, the MW share, the method, the period and the source."""
    name = val.scheme or val.stream.replace("_", " ").capitalize()
    if val.stream in LIVE_STREAMS:
        freshness = f"cached snapshot from {val.as_of.isoformat()} (live source failed)" if val.cached else "live"
    else:
        freshness = f"committed data, published {val.as_of.isoformat()}"
    parts = [f"{name}: £{val.gbp_per_mw_year:,.0f}/MW/year central estimate ({duration_h}h basis), {freshness}."]
    if val.placeholder:
        parts.append("PLACEHOLDER value.")
    if val.upper_bound_gbp_per_mw_year is not None:
        parts.append(
            f"Upper bound for context (perfect foresight, not used): £{val.upper_bound_gbp_per_mw_year:,.0f}/MW/year."
        )
    if val.mw_share is not None:
        parts.append(f"Uses {val.mw_share:.0%} of the MW.")
    if val.method:
        parts.append(f"Method: {val.method}.")
    if val.period:
        parts.append(f"Period: {val.period}.")
    parts.append(f"Source: {val.source}.")
    return " ".join(parts)


def _benchmark_artifact(stack: dict[int, list[StreamValue]], a: AssumptionSet, run_id: str) -> Artifact:
    """Modelled stack vs the published GB BESS benchmark, and the back-test that sized the calibration."""
    bench = a.entry("revenue_benchmark")
    b = a.mapping("revenue_benchmark")
    d = int(b["duration_h"])
    rows = stack.get(d, [])
    modelled = total(rows)
    uncalibrated = total([r for r in rows if r.stream != CALIBRATION])
    totals = ", ".join(f"{h}h £{total(r):,.0f}" for h, r in sorted(stack.items()))
    claim = (
        f"Back-test against {bench.source}: £{b['total_gbp_per_mw_year']:,.0f}/MW/year for a {d}h GB battery. "
        f"This run models {d}h at "
        f"£{uncalibrated:,.0f}/MW/year before calibration ({uncalibrated / b['total_gbp_per_mw_year']:.0%} of it) and "
        f"£{modelled:,.0f} after ({modelled / b['total_gbp_per_mw_year']:.0%}). Calibration, on the benchmark's own "
        f"period: {a.entry(CALIBRATION).derivation} Stack totals: {totals}."
    )
    return Artifact(
        id=f"market-backtest-{run_id[:8]}",
        stage="market",
        claim=claim,
        source_url=HttpUrl(bench.source_url or ""),
        confidence=METHOD_CONFIDENCE[CALIBRATION],
        model_used="market-assumptions",
    )
