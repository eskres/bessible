"""Market revenue projections stage: sourced streams, de-rating, and long-duration support."""

from __future__ import annotations

from bessible.market import load_market_assumptions
from bessible.market.sources import default_sources
from bessible.market.stack import revenue_stack, total
from bessible.models import Artifact, MarketOutput, NodeInput, StreamValue


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

    return MarketOutput(
        revenue_gbp_per_mw_year=revenue_4h,
        streams=streams_4h,
        by_duration=stack,
        artifacts=artifacts,
    )


# How far the method itself can be trusted, before live/cached/placeholder: a published auction price is exact;
# arbitrage is a perfect-foresight upper bound; ancillary rests on an estimated participation share.
METHOD_CONFIDENCE = {"capacity_market": 0.95, "wholesale": 0.7, "balancing_ancillary": 0.6}
CACHED_PENALTY = 0.1
PLACEHOLDER_CONFIDENCE = 0.3
LIVE_STREAMS = {"wholesale", "balancing_ancillary"}  # the rest are committed by design, so "cached" is expected


def _confidence(val: StreamValue) -> float:
    """Confidence from the method, then lowered for a cached snapshot, and floored for a placeholder."""
    if val.placeholder:
        return PLACEHOLDER_CONFIDENCE
    base = METHOD_CONFIDENCE.get(val.stream, 0.8)
    return round(base - CACHED_PENALTY, 2) if val.cached and val.stream in LIVE_STREAMS else base


def _claim(val: StreamValue, duration_h: int) -> str:
    """Say live or cached, the method, the period covered and the source."""
    name = val.scheme or val.stream.replace("_", " ").capitalize()
    if val.stream in LIVE_STREAMS:
        freshness = f"cached snapshot from {val.as_of.isoformat()} (live source failed)" if val.cached else "live"
    else:
        freshness = f"committed data, published {val.as_of.isoformat()}"
    parts = [f"{name}: £{val.gbp_per_mw_year:,.0f}/MW/year ({duration_h}h basis), {freshness}."]
    if val.placeholder:
        parts.append("PLACEHOLDER value.")
    if val.method:
        parts.append(f"Method: {val.method}.")
    if val.period:
        parts.append(f"Period: {val.period}.")
    parts.append(f"Source: {val.source}.")
    return " ".join(parts)
