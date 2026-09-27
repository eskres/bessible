"""Unit tests for the market revenue package and assessment stage."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bessible.api import elexon, neso
from bessible.api.ckan import DatastoreSearchSqlResponse
from bessible.market import load_market_assumptions
from bessible.market.live import (
    ElexonArbitrageSource,
    NesoResponseSource,
    arbitrage_by_duration,
    daily_spread_revenue,
    prices_by_day,
    response_value,
)
from bessible.market.sources import (
    STREAM_NAMES,
    FallbackSource,
    FixtureSource,
    RevenueSource,
    default_sources,
)
from bessible.market.stack import revenue_stack, total
from bessible.market.support import qualifying, support_stream
from bessible.models import (
    AssessmentRequest,
    CapacityOutput,
    ConfirmedSite,
    NodeInput,
    Position,
    TitleOutput,
)
from bessible.stages.market import market_revenue

FIXTURES = Path(__file__).parent / "api" / "fixtures"


def fixture_sources() -> list[RevenueSource]:
    return [FixtureSource(name) for name in STREAM_NAMES]


def test_market_assumptions():
    a = load_market_assumptions()
    assert "capacity_market_derating" in a.entries
    assert "cap_and_floor" in a.entries
    assert "ultra_lds" not in a.entries  # a GBP 28m innovation grant, not a per-MW revenue stream
    assert a.number("round_trip_efficiency") == 0.85
    # Every agreed entry names the page it came from
    for key, entry in a.entries.items():
        assert entry.source_url, key
        assert entry.quote, key


@pytest.mark.anyio
async def test_revenue_stack_and_derating():
    a = load_market_assumptions()
    stack = await revenue_stack(20.0, fixture_sources(), a)

    assert set(stack.keys()) == {2, 4, 8}

    cm_2h = next(s for s in stack[2] if s.stream == "capacity_market")
    cm_4h = next(s for s in stack[4] if s.stream == "capacity_market")
    cm_8h = next(s for s in stack[8] if s.stream == "capacity_market")

    # T-4 2029/30: GBP 27.10/kW de-rated, storage de-rating 2h 0.2198, 4h 0.4396, 8h 0.8777
    assert cm_2h.gbp_per_mw_year == pytest.approx(27100 * 0.2198)
    assert cm_4h.gbp_per_mw_year == pytest.approx(27100 * 0.4396)
    assert cm_8h.gbp_per_mw_year == pytest.approx(27100 * 0.8777)

    # Shorter duration earns less Capacity Market revenue
    assert cm_2h.gbp_per_mw_year < cm_4h.gbp_per_mw_year < cm_8h.gbp_per_mw_year

    # Total equals sum of rows
    for rows in stack.values():
        assert total(rows) == sum(r.gbp_per_mw_year for r in rows)


@pytest.mark.anyio
async def test_fallback_source_on_live_failure():
    fixture_src = FixtureSource("capacity_market")
    failing_live = AsyncMock(spec=RevenueSource)
    failing_live.name = "live_cm"
    failing_live.fetch.side_effect = ConnectionError("Live endpoint timeout")

    fallback = FallbackSource(live=failing_live, fixture=fixture_src)
    val = await fallback.fetch(4)

    assert val.stream == "capacity_market"
    assert val.cached is True
    assert val.placeholder is False
    assert val.source_url.host == "assets.publishing.service.gov.uk"


def test_long_duration_support_qualifying():
    a = load_market_assumptions()

    # 2h 20MW qualifies for neither
    assert qualifying(2, 20.0, a) == []
    # 4h 120MW qualifies for neither (needs min 8h for cap_and_floor)
    assert qualifying(4, 120.0, a) == []
    # 8h 20MW does not qualify for cap_and_floor (min 100MW required)
    assert qualifying(8, 20.0, a) == []
    # 8h 100MW qualifies for cap_and_floor
    assert qualifying(8, 100.0, a) == ["cap_and_floor"]
    assert qualifying(24, 100.0, a) == ["cap_and_floor"]

    stream = support_stream("cap_and_floor", a)
    assert stream.stream == "cap_and_floor"
    assert stream.scheme == "Ofgem LDES cap and floor"
    # Cap and floor levels are set per project and not published, so nothing is added and the row says so
    assert stream.gbp_per_mw_year == 0.0
    assert stream.placeholder is True


@pytest.mark.anyio
async def test_market_revenue_stage():
    out = await market_revenue(_node_input())
    assert out.revenue_gbp_per_mw_year > 0
    assert out.by_duration is not None
    assert len(out.by_duration) == 3
    assert "capacity_market" in out.streams
    assert "balancing_ancillary" in out.streams
    assert "wholesale" in out.streams

    # Every stream has an artifact with source link
    assert len(out.artifacts) >= 3
    for art in out.artifacts:
        assert art.source_url is not None
        assert art.stage == "market"


def test_daily_spread_revenue_known_series():
    # 48 half hours: 8 at 10, 8 at 20, 16 at 50, 8 at 90, 8 at 100 GBP/MWh
    day = [10.0] * 8 + [20.0] * 8 + [50.0] * 16 + [90.0] * 8 + [100.0] * 8
    # 2h = 4 half hours: top mean 100, bottom mean 10 -> 90 x 2 MWh x 0.85
    assert daily_spread_revenue(day, 2, 0.85) == pytest.approx(90 * 2 * 0.85)
    # 4h = 8 half hours: 100 - 10 = 90 -> 90 x 4 x 0.85
    assert daily_spread_revenue(day, 4, 0.85) == pytest.approx(90 * 4 * 0.85)
    # 8h = 16 half hours: top mean 95, bottom mean 15 -> 80 x 8 x 0.85
    assert daily_spread_revenue(day, 8, 0.85) == pytest.approx(80 * 8 * 0.85)
    # Summed over days
    flat = [40.0] * 48
    by_d = arbitrage_by_duration({date(2026, 1, 1): day, date(2026, 1, 2): flat}, 1.0)
    assert by_d == {2: pytest.approx(180.0), 4: pytest.approx(360.0), 8: pytest.approx(640.0)}


def test_prices_by_day_from_real_elexon_response():
    records = elexon.MarketIndexStreamResponse.model_validate(
        json.loads((FIXTURES / "elexon_mid_stream_apx_20260920.json").read_text())
    ).root
    days = prices_by_day(records, date(2026, 9, 20), date(2026, 9, 20))
    assert list(days) == [date(2026, 9, 20)]
    assert len(days[date(2026, 9, 20)]) == 48
    # A day outside the window, or with too few priced periods, is dropped
    assert prices_by_day(records, date(2026, 9, 21), date(2026, 9, 22)) == {}
    assert prices_by_day(records[:40], date(2026, 9, 20), date(2026, 9, 20)) == {}
    assert daily_spread_revenue(days[date(2026, 9, 20)], 4, 0.85) > 0


def test_response_value_from_real_neso_responses():
    rows = []
    for name in ("current", "fy2025"):
        body = json.loads((FIXTURES / f"neso_response_summary_{name}.json").read_text())
        rows += DatastoreSearchSqlResponse[neso.ResponseProductSummary].model_validate(body).result.records
    v = response_value(rows, fleet_mw=5000.0)
    assert set(v.pair_price) == {"DC", "DM", "DR"}
    # The volume-weighted value lies between the cheapest and dearest family
    assert min(v.pair_price.values()) <= v.value_gbp_per_mw_h <= max(v.pair_price.values())
    assert v.share == pytest.approx(sum(v.pair_volume.values()) / 5000.0)
    # The share never exceeds 1, even for a tiny fleet
    assert response_value(rows, fleet_mw=1.0).share == 1.0
    with pytest.raises(ValueError, match="DCL"):
        response_value([r for r in rows if r.auction_product != "DCL"], fleet_mw=5000.0)


@pytest.mark.anyio
async def test_live_sources_use_the_day_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("bessible.market.live.CACHE_DIR", tmp_path)
    today = date(2026, 9, 27)
    for name in ("wholesale", "balancing_ancillary"):
        fixture = json.loads((FixtureSource(name).path).read_text())
        (tmp_path / f"{name}_{today.isoformat()}.json").write_text(json.dumps(fixture))
    a = load_market_assumptions()
    w = await ElexonArbitrageSource(a, today=today).fetch(4)
    b = await NesoResponseSource(today=today).fetch(4)
    # A same-day cache hit is still the live computation
    assert w.cached is False
    assert b.cached is False
    assert w.method
    assert "upper bound" in w.method
    assert b.period


@pytest.mark.anyio
async def test_live_failure_falls_back_once_and_says_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("bessible.market.live.CACHE_DIR", tmp_path)
    live = ElexonArbitrageSource(load_market_assumptions(), today=date(2026, 9, 27))
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise ConnectionError("network off")

    monkeypatch.setattr(live, "figure", boom)
    source = FallbackSource(live, FixtureSource("wholesale"))
    values = [await source.fetch(d) for d in (2, 4, 8)]
    assert calls == 1  # one live attempt per run, not one per duration
    assert all(v.cached for v in values)

    monkeypatch.setattr("bessible.stages.market.default_sources", lambda: [source])
    out = await market_revenue(_node_input())
    art = next(a for a in out.artifacts if a.id.startswith("market-wholesale"))
    assert "cached snapshot" in art.claim
    assert "Method:" in art.claim
    assert "Period:" in art.claim
    assert "Source:" in art.claim
    assert art.confidence < 0.7
    gap = next(g for g in out.gaps if g.what == "wholesale")
    assert gap.retryable


def test_default_sources_wraps_live_streams():
    names = {type(s).__name__: s for s in default_sources()}
    assert set(names) == {"FixtureSource", "FallbackSource"}
    live = [s.live.name for s in default_sources() if isinstance(s, FallbackSource)]
    assert sorted(live) == ["elexon_mid_arbitrage", "neso_response_auctions"]


def _node_input() -> NodeInput:
    req = AssessmentRequest(postcode="RH4 1AD")
    cap = CapacityOutput(viable=True, firm_mw=10.0, ceiling_mw=20.0, recommended_mw=15.0)
    title = TitleOutput(
        title_number="SY12345",
        area_m2=5000.0,
        boundary_geojson={"type": "Polygon", "coordinates": []},
    )
    site = ConfirmedSite(position=Position(lat=51.23, lon=-0.33), capacity_mw=15.0, boundary=title)
    return NodeInput(run_id="run-mkt-test", request=req, site=site, capacity=cap)
