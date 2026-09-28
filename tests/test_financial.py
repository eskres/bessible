"""Unit tests for the financial model package and assessment stage."""

from __future__ import annotations

import pytest

from bessible.assumptions import AssumptionSet, MissingAssumption
from bessible.finance import load_finance_assumptions
from bessible.finance.cost import cost
from bessible.finance.curtailment import curtailment_pct, load_demand_profile, load_duration_curve
from bessible.finance.returns import returns
from bessible.models import (
    AssessmentRequest,
    CapacityOutput,
    ConfirmedSite,
    FinancialInput,
    GridOutput,
    MarketOutput,
    Position,
    SiteLandOutput,
    TitleOutput,
)
from bessible.stages.financial import financial_model


def test_missing_assumption():
    empty_set = AssumptionSet(entries={})
    with pytest.raises(MissingAssumption) as exc_info:
        empty_set.number("battery_gbp_per_mwh")
    assert "battery_gbp_per_mwh" in str(exc_info.value)


def test_cost_scaling_and_connection():
    a = load_finance_assumptions()

    # CAPEX grows with duration
    c2 = cost(2, 20.0, 1.0, crossings=False, a=a)
    c4 = cost(4, 20.0, 1.0, crossings=False, a=a)
    c8 = cost(8, 20.0, 1.0, crossings=False, a=a)
    assert c2.capex_gbp < c4.capex_gbp < c8.capex_gbp

    # Connection cost for 1 km (NGED 33 kV: £154k - £841k per km)
    assert c2.connection_gbp == (154000.0, 841000.0)

    # Connection cost for 3 km
    c3km = cost(4, 20.0, 3.0, crossings=False, a=a)
    assert c3km.connection_gbp == (462000.0, 2523000.0)

    # Crossing uplift (25%)
    c_cross = cost(4, 20.0, 1.0, crossings=True, a=a)
    assert c_cross.connection_gbp == (192500.0, 1051250.0)
    assert c_cross.crossing_uplift_applied is True

    # 132 kV connection cost (NGED: £2.119m - £3.171m per km)
    c132 = cost(4, 80.0, 1.0, crossings=False, a=a, voltage_kv=132)
    assert c132.connection_gbp == (2119000.0, 3171000.0)

    # 132 kV connection with crossing uplift (25%)
    c132_cross = cost(4, 80.0, 2.0, crossings=True, a=a, voltage_kv=132)
    assert c132_cross.connection_gbp == (2119000.0 * 2 * 1.25, 3171000.0 * 2 * 1.25)


def test_otcf_thresholds():
    a = load_finance_assumptions()

    # Default in finance.json is 210% (Ofgem CMP470: ~90 GW vs ~29 GW) -> active
    c = cost(4, 20.0, 1.0, crossings=False, a=a)
    assert c.otcf_gbp == (60000.0, 500000.0)
    assert c.otcf_state.startswith("active")
    assert "proposed" in c.otcf_state

    # Between 25% and 50% -> inactive
    a_mid = load_finance_assumptions()
    a_mid.entries["oversubscription_pct"].value = 30
    c_mid = cost(4, 20.0, 1.0, crossings=False, a=a_mid)
    assert c_mid.otcf_gbp is None
    assert "inactive" in c_mid.otcf_state

    # Below 25% -> inactive
    a_low = load_finance_assumptions()
    a_low.entries["oversubscription_pct"].value = 20
    c_low = cost(4, 20.0, 1.0, crossings=False, a=a_low)
    assert c_low.otcf_gbp is None
    assert "inactive" in c_low.otcf_state


def test_curtailment_logic():
    a = load_finance_assumptions()
    profile = load_demand_profile()
    max_mw = a.number("substation_max_demand_mw")
    min_mw = a.number("substation_min_demand_mw")
    curve = load_duration_curve(max_mw, min_mw, profile.values)

    assert max(curve) == pytest.approx(max_mw)
    assert min(curve) == pytest.approx(min_mw)

    # Within firm headroom: 0% curtailment
    curt_firm = curtailment_pct(10.0, 10.0, 25.0, curve, 4, max_mw=max_mw, min_mw=min_mw)
    assert curt_firm == 0.0

    # Above firm headroom: positive curtailment
    curt_flex = curtailment_pct(20.0, 10.0, 25.0, curve, 4, max_mw=max_mw, min_mw=min_mw)
    assert curt_flex > 0.0


def test_returns_and_budget():
    a = load_finance_assumptions()
    c4 = cost(4, 10.0, 1.0, crossings=False, a=a)

    # Known repeatable calculation
    r1 = returns(c4, 94000.0, 5.0, 10.0, a, budget_gbp=5_000_000.0)
    r2 = returns(c4, 94000.0, 5.0, 10.0, a, budget_gbp=5_000_000.0)
    assert r1.capex_gbp == r2.capex_gbp
    assert r1.npv_gbp == r2.npv_gbp
    assert r1.irr == r2.irr
    assert r1.over_budget is True  # capex > 5m

    # Equity is the capex the loan does not cover, plus the arrangement fee on the loan
    debt = c4.capex_gbp * a.number("debt_share_pct") / 100
    expected_equity = c4.capex_gbp - debt + debt * a.number("arrangement_fee_pct") / 100
    assert r1.equity_gbp == pytest.approx(expected_equity, abs=0.01)

    # Budget check flags over budget appropriately
    r_high_budget = returns(c4, 94000.0, 5.0, 10.0, a, budget_gbp=50_000_000.0)
    assert r_high_budget.over_budget is False


@pytest.mark.anyio
async def test_financial_stage_end_to_end():
    req = AssessmentRequest(postcode="RH4 1AD", budget_gbp=15_000_000.0)
    cap = CapacityOutput(viable=True, firm_mw=8.0, ceiling_mw=16.0, recommended_mw=12.0, distance_km=1.5)
    title = TitleOutput(
        title_number="SY12345",
        area_m2=5000.0,
        boundary_geojson={"type": "Polygon", "coordinates": []},
    )
    site = ConfirmedSite(position=Position(lat=51.23, lon=-0.33), capacity_mw=12.0, boundary=title)
    grid = GridOutput()
    land = SiteLandOutput(
        land_use="Industrial",
        constraints=["Railway crossing required for 33kV cable route", "Low flood risk"],
    )
    market = MarketOutput(
        revenue_gbp_per_mw_year=94000.0,
        streams={"wholesale": 45000.0, "capacity_market": 24000.0, "balancing_ancillary": 25000.0},
    )

    inp = FinancialInput(
        run_id="run-fin-test",
        request=req,
        site=site,
        capacity=cap,
        grid=grid,
        market=market,
        site_land=land,
    )

    out = await financial_model(inp)
    assert len(out.cases) == 3
    assert [c.duration_h for c in out.cases] == [2, 4, 8]

    # Crossing uplift applied because of railway crossing constraint
    cost_art = next(a for a in out.artifacts if "cost" in a.id)
    assert "crossing uplift of 25% applied" in cost_art.claim
    assert "interest rate 6.23%" in cost_art.claim

    # Curtailment artifact states demand profile and documented assumption
    curt_art = next(a for a in out.artifacts if "curtailment" in a.id)
    assert "documented assumption, not measured data" in curt_art.claim
    assert "Demand profile:" in curt_art.claim

    # Returns artifact documents returns and flags cases over budget if applicable
    ret_art = next(a for a in out.artifacts if "returns" in a.id)
    assert "25-year returns:" in ret_art.claim

    # Financing terms travel with the output so the report can explain the figures
    assert out.debt_share_pct == 70
    assert out.loan_term_years == 15
    assert all(c.equity_gbp for c in out.cases)


@pytest.mark.anyio
async def test_financial_model_132kv_rate():
    req = AssessmentRequest(postcode="RH4 1AD", battery_mw=80.0)
    title = TitleOutput(
        viable=True, title_number="SY12345", boundary_polygon=[[51.23, -0.33], [51.24, -0.33]], area_m2=50000.0
    )
    site = ConfirmedSite(
        name="Test 80MW Site",
        postcode="RH4 1AD",
        position=Position(lat=51.23, lon=-0.33),
        capacity_mw=80.0,
        substation="Leatherhead 132kV",
        boundary=title,
    )
    cap = CapacityOutput(
        viable=True,
        substation="Leatherhead 132kV",
        connection_voltage_kv=132.0,
        firm_mw=85.0,
        ceiling_mw=100.0,
        recommended_mw=85.0,
        distance_km=2.0,
    )
    grid = GridOutput(viable=True)
    market = MarketOutput(
        revenue_gbp_per_mw_year=88000.0,
        streams={"wholesale": 45000.0, "capacity_market": 24000.0, "balancing_ancillary": 25000.0},
    )

    inp = FinancialInput(
        run_id="run-132kv-fin",
        request=req,
        site=site,
        capacity=cap,
        grid=grid,
        market=market,
    )

    out = await financial_model(inp)
    cost_art = next(a for a in out.artifacts if "cost" in a.id)
    assert "132 kV connection" in cost_art.claim
    assert "£2.119m-£3.171m/km" in cost_art.claim
    # 2 km at £2.119m - £3.171m = £4,238,000 - £6,342,000
    assert "£4,238,000" in cost_art.claim
    assert "£6,342,000" in cost_art.claim
