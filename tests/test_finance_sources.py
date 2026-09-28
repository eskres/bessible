"""The data sources list behind the financial figures."""

from __future__ import annotations

from bessible.finance import load_finance_assumptions
from bessible.finance.sources import finance_sources


def test_lists_the_discount_rate_with_its_source() -> None:
    sources = {s.key: s for s in finance_sources(load_finance_assumptions())}
    rate = sources["discount_rate_pct"]
    assert rate.value == "10.36%"
    assert rate.source_url
    assert not rate.placeholder


def test_cable_follows_the_connection_voltage() -> None:
    a = load_finance_assumptions()
    assert "cable_33kv_gbp_per_km" in {s.key for s in finance_sources(a)}
    keys = {s.key for s in finance_sources(a, voltage_kv=132)}
    assert "cable_132kv_gbp_per_km" in keys
    assert "cable_33kv_gbp_per_km" not in keys


def test_marks_placeholders() -> None:
    sources = {s.key: s for s in finance_sources(load_finance_assumptions())}
    assert sources["project_life_years"].placeholder
    assert sources["battery_gbp_per_mwh"].value == "£98,400/MWh"
