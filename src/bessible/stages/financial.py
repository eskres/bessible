"""Financial model assessment stage: CAPEX, OPEX, curtailment, and returns across durations."""

from __future__ import annotations

import asyncio

from pydantic import HttpUrl

from bessible.finance import load_finance_assumptions
from bessible.finance.cost import VOLTAGE_132KV, CostBreakdown, cost
from bessible.finance.curtailment import curtailment_pct, load_demand_profile, load_duration_curve
from bessible.finance.returns import returns
from bessible.finance.sources import finance_sources
from bessible.market.stack import total
from bessible.models import Artifact, DurationCase, FinancialInput, FinancialOutput

CROSSING_KEYWORDS = ("crossing", "railway", "rail", "river", "road", "canal", "hard surface")


def _gbp_short(value: float) -> str:
    """Format pounds as £154k or £2.119m."""
    if value >= 1_000_000:
        return f"£{value / 1_000_000:g}m"
    return f"£{value / 1000:g}k"


def _detect_crossings(inp: FinancialInput) -> bool:
    """Check if site land constraints indicate route crossings or hard surfaces."""
    if inp.site_land and inp.site_land.constraints:
        return any(any(k in c.lower() for k in CROSSING_KEYWORDS) for c in inp.site_land.constraints)
    return False


async def financial_model(inp: FinancialInput) -> FinancialOutput:
    """Evaluate financial returns across 2-hour, 4-hour, and 8-hour duration cases in plain code."""
    await asyncio.sleep(0)
    mw = inp.site.capacity_mw
    # The cable is priced on the route (straight line x detour factor); without one, on the straight line as before
    route = inp.capacity.route
    straight = inp.capacity.distance_km
    if route is not None and route.distance_km > 0:
        distance_km = route.distance_km
        distance_basis = f"{route.straight_km:g} km straight line x{route.detour_factor:g} detour"
    else:
        distance_km = straight if (straight is not None and straight > 0) else 1.0
        distance_basis = "straight line" if straight else "assumed"
    firm_mw = inp.capacity.firm_mw
    ceiling_mw = inp.capacity.ceiling_mw
    budget_gbp = inp.request.budget_gbp
    crossings = _detect_crossings(inp)

    a = load_finance_assumptions()
    profile = load_demand_profile()
    max_mw = a.number("substation_max_demand_mw")
    min_mw = a.number("substation_min_demand_mw")
    curve = load_duration_curve(max_mw, min_mw, profile.values)

    cases: list[DurationCase] = []
    cost_by_duration: dict[int, CostBreakdown] = {}
    curt_by_duration: dict[int, float] = {}

    voltage_kv = inp.capacity.connection_voltage_kv

    for d in (2, 4, 8):
        c = cost(d, mw, distance_km, crossings=crossings, a=a, voltage_kv=voltage_kv)
        cost_by_duration[d] = c
        curt = curtailment_pct(mw, firm_mw, ceiling_mw, curve, d, max_mw=max_mw, min_mw=min_mw)
        curt_by_duration[d] = curt

        if inp.market.by_duration and d in inp.market.by_duration:
            rev = total(inp.market.by_duration[d])
        else:
            rev = inp.market.revenue_gbp_per_mw_year

        case = returns(c, rev, curt, mw, a, budget_gbp=budget_gbp)
        cases.append(case)

    # Reference case (4h) for summary artifacts
    c_4h = cost_by_duration[4]
    int_rate = a.number("interest_rate_pct")
    arr_fee = a.number("arrangement_fee_pct")
    disc_rate = a.number("discount_rate_pct")
    debt_share = a.number("debt_share_pct")
    loan_term = int(a.number("loan_term_years"))

    crossing_str = (
        f"crossing uplift of {a.number('crossing_uplift_pct'):g}% applied"
        if crossings
        else "no crossing uplift applied"
    )

    volt_label = f"{int(voltage_kv)} kV" if voltage_kv else "33 kV"
    cable_low, cable_high = a.pair("cable_132kv_gbp_per_km" if voltage_kv == VOLTAGE_132KV else "cable_33kv_gbp_per_km")
    rate_str = f"{_gbp_short(cable_low)}-{_gbp_short(cable_high)}/km"

    art_cost = Artifact(
        id=f"financial-cost-{inp.run_id[:8]}",
        stage="financial",
        claim=(
            f"Financing terms: interest rate {int_rate:g}%, arrangement fee {arr_fee:g}%, "
            f"discount rate {disc_rate:g}%, debt share {debt_share:g}%, loan term {loan_term} years. "
            f"{volt_label} connection ({rate_str} over {distance_km:.2f} km, {distance_basis}): "
            f"£{c_4h.connection_gbp[0]:,.0f} - £{c_4h.connection_gbp[1]:,.0f} ({crossing_str}). "
            f"OTCF fee: {c_4h.otcf_state}."
        ),
        source_url=HttpUrl("https://www.ofgem.gov.uk/"),
        confidence=0.9,
        model_used="financial-model",
    )

    art_curtailment = Artifact(
        id=f"financial-curtailment-{inp.run_id[:8]}",
        stage="financial",
        claim=(
            "Curtailment estimate (documented assumption, not measured data): "
            f"2h: {curt_by_duration[2]:.1f}%, 4h: {curt_by_duration[4]:.1f}%, 8h: {curt_by_duration[8]:.1f}%. "
            f"Demand profile: {profile.source} ({profile.date}), scaled between {min_mw:g} MW and {max_mw:g} MW. "
            f"Applied to overlap between dispatch hours and grid constraints above firm capacity ({firm_mw:g} MW)."
        ),
        source_url=HttpUrl("https://www.elexon.co.uk/"),
        confidence=0.85,
        model_used="financial-model",
    )

    case_summaries = []
    for case in cases:
        irr_str = f"{case.irr * 100:.1f}%" if case.irr is not None else "N/A"
        case_summaries.append(
            f"{case.duration_h}h (CAPEX £{case.capex_gbp:,.0f}, NPV £{case.npv_gbp:,.0f}, IRR {irr_str})"
        )

    returns_claim = f"{int(a.number('project_life_years'))}-year returns:{'; '.join(case_summaries)}."
    over_budget_cases = [f"{c.duration_h}h" for c in cases if c.over_budget]
    if over_budget_cases:
        returns_claim += f" Flagged over budget: {', '.join(over_budget_cases)}."
    if any(c.irr is None for c in cases):
        returns_claim += " Cases with N/A IRR do not pay back equity over project life."

    art_returns = Artifact(
        id=f"financial-returns-{inp.run_id[:8]}",
        stage="financial",
        claim=returns_claim,
        source_url=HttpUrl("https://www.ofgem.gov.uk/"),
        confidence=0.9,
        model_used="financial-model",
    )

    best_case = max((c for c in cases if c.irr is not None), key=lambda c: c.irr, default=None)
    rec_h = best_case.duration_h if best_case else 4
    rationale = f"Optimal returns at {rec_h}-hour duration based on financial model projections."

    return FinancialOutput(
        cases=cases,
        recommended_h=rec_h,
        rationale=rationale,
        discount_rate_pct=disc_rate,
        project_life_years=int(a.number("project_life_years")),
        debt_share_pct=debt_share,
        interest_rate_pct=int_rate,
        arrangement_fee_pct=arr_fee,
        loan_term_years=loan_term,
        sources=finance_sources(a, voltage_kv),
        artifacts=[art_cost, art_curtailment, art_returns],
    )
