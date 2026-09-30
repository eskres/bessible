"""NPV and IRR for one duration case, in plain code (no language model)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from bessible.models import DebtShareCase, DurationCase

if TYPE_CHECKING:
    from bessible.assumptions import AssumptionSet
    from bessible.finance.cost import CostBreakdown

IRR_UPPER = 10.0
IRR_ITERATIONS = 200
DEBT_SHARES_PCT = range(0, 81, 5)  # the report's debt slider steps


class Financing(BaseModel):
    """Financing terms read from the assumptions file."""

    discount_rate: float
    interest_rate: float
    arrangement_fee_pct: float
    debt_share: float
    loan_term_years: int
    project_life_years: int

    @classmethod
    def from_assumptions(cls, a: AssumptionSet) -> Financing:
        """Read terms; raises `MissingAssumption` naming any absent key."""
        return cls(
            discount_rate=a.number("discount_rate_pct") / 100,
            interest_rate=a.number("interest_rate_pct") / 100,
            arrangement_fee_pct=a.number("arrangement_fee_pct"),
            debt_share=a.number("debt_share_pct") / 100,
            loan_term_years=int(a.number("loan_term_years")),
            project_life_years=int(a.number("project_life_years")),
        )


def cash_flows(cost: CostBreakdown, revenue_gbp_per_mw_year: float, curtail_pct: float, f: Financing) -> list[float]:
    """Equity cash flows for years 0..life. Year 0 is equity plus the arrangement fee."""
    debt = cost.capex_gbp * f.debt_share
    fee = debt * f.arrangement_fee_pct / 100
    annual_net = cost.mw * revenue_gbp_per_mw_year * (1 - curtail_pct / 100) - cost.opex_gbp_per_year

    n = f.loan_term_years
    payment = debt / n if f.interest_rate == 0 else debt * f.interest_rate / (1 - (1 + f.interest_rate) ** -n)

    flows = [-(cost.capex_gbp - debt + fee)]
    flows.extend(annual_net - (payment if year <= n else 0.0) for year in range(1, f.project_life_years + 1))
    return flows


def npv(rate: float, flows: list[float]) -> float:
    """Net present value with flow 0 at time zero."""
    return sum(cf / (1 + rate) ** year for year, cf in enumerate(flows))


def irr(flows: list[float]) -> float | None:
    """Internal rate of return by bisection. None when the flows never pay back."""
    if flows[0] >= 0 or sum(flows) <= 0:
        return None
    low, high = 0.0, IRR_UPPER
    if npv(high, flows) > 0:
        return None
    for _ in range(IRR_ITERATIONS):
        mid = (low + high) / 2
        if npv(mid, flows) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def returns(  # ruff: ignore[too-many-arguments,too-many-positional-arguments]
    cost: CostBreakdown,
    revenue_gbp_per_mw_year: float,
    curtail_pct: float,
    mw: float,
    a: AssumptionSet,
    budget_gbp: float | None = None,
) -> DurationCase:
    """Compute one duration case. `mw` must equal `cost.mw`."""
    if mw != cost.mw:
        msg = f"mw ({mw}) does not match the cost breakdown ({cost.mw})"
        raise ValueError(msg)
    f = Financing.from_assumptions(a)
    flows = cash_flows(cost, revenue_gbp_per_mw_year, curtail_pct, f)
    over_budget = bool(budget_gbp is not None and cost.capex_gbp > budget_gbp)
    by_debt_share = []
    for pct in DEBT_SHARES_PCT:
        at = cash_flows(cost, revenue_gbp_per_mw_year, curtail_pct, f.model_copy(update={"debt_share": pct / 100}))
        by_debt_share.append(
            DebtShareCase(
                debt_share_pct=pct, npv_gbp=round(npv(f.discount_rate, at), 2), irr=irr(at), equity_gbp=round(-at[0], 2)
            )
        )
    return DurationCase(
        duration_h=cost.duration_h,
        capex_gbp=round(cost.capex_gbp, 2),
        npv_gbp=round(npv(f.discount_rate, flows), 2),
        irr=irr(flows),
        equity_gbp=round(-flows[0], 2),
        over_budget=over_budget,
        curtailment_pct=round(curtail_pct, 2),
        by_debt_share=by_debt_share,
    )
