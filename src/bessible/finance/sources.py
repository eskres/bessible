"""The documented assumptions behind the financial figures, formatted for the report's data sources list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from bessible.finance.cost import VOLTAGE_132KV
from bessible.models import AssumptionSource

if TYPE_CHECKING:
    from bessible.assumptions import Assumption, AssumptionSet

Group = Literal["costs", "financing", "grid"]

# (key, group, label) in the order the report lists them; the cable key is chosen by connection voltage
SOURCES: tuple[tuple[str, Group, str], ...] = (
    ("battery_gbp_per_mwh", "costs", "Battery cost"),
    ("balance_of_plant_gbp_per_mw", "costs", "Power conversion and balance of plant"),
    ("opex_gbp_per_mw_year", "costs", "Operating cost"),
    ("cable", "costs", "Connection cable"),
    ("cable_detour_factor", "costs", "Cable route detour"),
    ("crossing_uplift_pct", "costs", "Crossing uplift"),
    ("otcf_gbp_per_mw", "costs", "Queue fee (OTCF)"),
    ("oversubscription_pct", "costs", "Queue oversubscription"),
    ("debt_share_pct", "financing", "Debt share"),
    ("interest_rate_pct", "financing", "Interest rate"),
    ("arrangement_fee_pct", "financing", "Arrangement fee"),
    ("loan_term_years", "financing", "Loan term"),
    ("discount_rate_pct", "financing", "Discount rate"),
    ("project_life_years", "financing", "Project life"),
    ("substation_max_demand_mw", "grid", "Substation peak demand"),
    ("substation_min_demand_mw", "grid", "Substation minimum demand"),
)


def _number(value: float, unit: str) -> str:
    """Format one value with its unit: £ amounts with separators, percentages attached."""
    if unit.startswith("GBP"):
        return f"£{value:,.0f}{unit[3:]}"
    if unit.startswith("%"):
        return f"{value:g}{unit}"
    return f"{value:g} {unit}"


def _value(entry: Assumption) -> str:
    """Format a single number or a [low, high] pair."""
    value = entry.value
    if isinstance(value, list):
        return " to ".join(_number(float(v), entry.unit) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_number(float(v), entry.unit)}" for k, v in value.items())
    return _number(float(value or 0), entry.unit)


def finance_sources(a: AssumptionSet, voltage_kv: float | None = None) -> list[AssumptionSource]:
    """List every assumption the financial model uses, with its value, source and status."""
    kv = 132 if voltage_kv == VOLTAGE_132KV else 33
    out: list[AssumptionSource] = []
    for key, group, label in SOURCES:
        real_key, real_label = (f"cable_{kv}kv_gbp_per_km", f"{label} ({kv} kV)") if key == "cable" else (key, label)
        entry = a.entries.get(real_key)
        if entry is None or entry.value is None:
            continue
        out.append(
            AssumptionSource(
                key=real_key,
                group=group,
                label=real_label,
                value=_value(entry),
                source=entry.source,
                source_url=entry.source_url,
                publisher=entry.publisher,
                published=entry.published,
                quote=entry.quote,
                derivation=entry.derivation,
                placeholder=entry.status == "placeholder",
            )
        )
    return out
