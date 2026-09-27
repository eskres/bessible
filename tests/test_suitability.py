"""Unit tests for Suitability Engine: local sentiment, financial model, and verdict."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bessible.classifier import Classified
from bessible.models import (
    AssessmentRequest,
    CapacityOutput,
    ConfirmedSite,
    DataGap,
    DurationCase,
    FinancialInput,
    FinancialOutput,
    GridOutput,
    NodeInput,
    PlanningOutput,
    Position,
    SentimentOutput,
    SiteLandOutput,
    SynthesisInput,
    TitleOutput,
)
from bessible.stages.financial import financial_model
from bessible.stages.market import market_revenue
from bessible.stages.sentiment import local_sentiment
from bessible.stages.synthesis import synthesise
from bessible.suitability.assumptions import load_finance_assumptions
from bessible.suitability.finance import evaluate
from bessible.suitability.labels import ParagraphLabels
from bessible.suitability.research import research_local_news
from bessible.suitability.sentiment import compute_opposition_index
from bessible.suitability.verdict import check_numbers, decide


def test_paragraph_labels_schema():
    """Verify classifier question extraction for ParagraphLabels."""
    from bessible.classifier import _questions

    questions = _questions(ParagraphLabels)
    assert len(questions) == 4
    types = {q["type"] for q in questions}
    assert "noul" in types
    assert "choice" in types


def test_opposition_index_mixed_coverage():
    """Verify 2 against at 0.9 and 1 supportive at 0.6 give index > 0.5."""
    items = [
        Classified(
            text="Residents object over fire hazard",
            labels=ParagraphLabels(relevant=True, stance="against", concern="fire safety", mentions_risk=True),
            confidence={"relevant": 1.0, "stance": 0.9, "concern": 0.9},
        ),
        Classified(
            text="Second objection over fire safety",
            labels=ParagraphLabels(relevant=True, stance="against", concern="fire safety", mentions_risk=True),
            confidence={"relevant": 1.0, "stance": 0.9, "concern": 0.85},
        ),
        Classified(
            text="Local group supports green transition",
            labels=ParagraphLabels(relevant=True, stance="supportive", concern="ecology", mentions_risk=False),
            confidence={"relevant": 1.0, "stance": 0.6, "concern": 0.7},
        ),
    ]

    index, concerns = compute_opposition_index(items)
    assert index is not None
    assert index > 0.5
    assert index == pytest.approx(0.75, abs=0.01)
    assert "fire safety" in concerns


def test_opposition_index_no_relevant():
    """Verify that when no relevant coverage exists, opposition index is None."""
    items = [
        Classified(
            text="Flower festival in town center",
            labels=ParagraphLabels(relevant=False, stance="neutral", concern="other", mentions_risk=False),
            confidence={"relevant": 0.95, "stance": 0.5, "concern": 0.5},
        )
    ]
    index, concerns = compute_opposition_index(items)
    assert index is None
    assert concerns == []


def test_assumptions_validation_fails_on_missing_key():
    """Verify loading fails with KeyError naming the key when a value is removed."""
    base_assumptions = json.loads(Path("data/assumptions/finance.json").read_text(encoding="utf-8"))
    assert "battery_gbp_per_mwh" in base_assumptions

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        del base_assumptions["battery_gbp_per_mwh"]
        f.write(json.dumps(base_assumptions))
        temp_path = Path(f.name)

    try:
        with pytest.raises(KeyError) as exc_info:
            load_finance_assumptions(temp_path)
        assert "battery_gbp_per_mwh" in str(exc_info.value)
    finally:
        temp_path.unlink(missing_ok=True)


def test_finance_evaluate_spreadsheet_and_loss_making():
    """Verify CAPEX, NPV, and that loss-making case gives IRR None."""
    assumptions = load_finance_assumptions()

    # Normal 10 MW 4h case
    res_4h = evaluate(
        mw=10.0,
        duration_h=4,
        distance_km=0.5,
        firm_mw=10.0,
        budget_gbp=None,
        a=assumptions,
    )
    assert res_4h.duration_h == 4
    # CAPEX = 10 * 4 * 98.4k + 10 * 215.1k + 0.5 * 497.5k = 3.936m + 2.151m + 0.24875m = 6.33575m
    assert res_4h.capex_gbp.mid == pytest.approx(6_335_750.0, abs=100.0)
    assert res_4h.npv_gbp.mid > 0
    assert res_4h.irr is not None
    assert res_4h.irr.mid > 0.08  # clears 8% hurdle
    assert res_4h.over_budget is False

    # Budget check
    res_budget = evaluate(
        mw=10.0,
        duration_h=4,
        distance_km=0.5,
        firm_mw=10.0,
        budget_gbp=5_000_000.0,  # 5m budget < 6.34m capex
        a=assumptions,
    )
    assert res_budget.over_budget is True

    # Loss-making case: 0 revenue
    assumptions_loss = load_finance_assumptions()
    assumptions_loss.entries["revenue_4h_gbp_per_mw_year"].value.mid = 0.0
    res_loss = evaluate(
        mw=10.0,
        duration_h=4,
        distance_km=0.5,
        firm_mw=10.0,
        budget_gbp=None,
        a=assumptions_loss,
    )
    assert res_loss.irr is None
    assert res_loss.npv_gbp.mid < 0


def test_decide_verdict_rules():
    """Verify go, maybe (opposition 0.75), and unknown-sentiment cases."""
    fin = FinancialOutput(
        cases=[
            DurationCase(duration_h=2, capex_gbp=3.9e6, npv_gbp=2.1e6, irr=0.195),
            DurationCase(duration_h=4, capex_gbp=6.7e6, npv_gbp=1.9e6, irr=0.140),
            DurationCase(duration_h=8, capex_gbp=12.3e6, npv_gbp=0.1e6, irr=0.082),
        ],
        recommended_h=4,
    )

    # 1. Clear Go
    sent_go = SentimentOutput(opposition_index=0.20)
    v_go, _ = decide(fin, sent_go)
    assert v_go == "go"

    # 2. Maybe due to opposition 0.75
    sent_maybe = SentimentOutput(opposition_index=0.75, top_concerns=["fire safety"])
    v_maybe, reasons_maybe = decide(fin, sent_maybe)
    assert v_maybe == "maybe"
    assert "fire safety" in reasons_maybe[0]

    # 3. No-go due to extreme opposition >= 0.80
    sent_nogo = SentimentOutput(opposition_index=0.85)
    v_nogo, _ = decide(fin, sent_nogo)
    assert v_nogo == "no_go"

    # 4. Unknown sentiment (None) -> decided on finances alone
    v_unk, reasons_unk = decide(fin, None)
    assert v_unk == "go"
    assert any("unavailable" in r.lower() for r in reasons_unk)


def test_number_guard_verification():
    """Verify a finding stating 'IRR 14%' against a computed 9% is flagged, and correct figure passes."""
    computed_set = {9.0, 0.09, 4.0, 10.0, 6.7}

    bad_finding = "The 4-hour battery achieves an IRR of 14% with 10 MW capacity."
    unmatched_bad = check_numbers(bad_finding, computed_set)
    assert 14.0 in unmatched_bad

    good_finding = "The 4-hour battery achieves an IRR of 9% with 10 MW capacity."
    unmatched_good = check_numbers(good_finding, computed_set)
    assert unmatched_good == []


@pytest.mark.anyio
async def test_cached_news_research():
    """Verify research agent reads cached news fixture without searching."""
    res = await research_local_news(
        place="Dorking",
        lat=51.2329,
        lon=-0.3315,
        postcode="RH4 1AD",
    )
    assert res.cached is True
    assert len(res.sources) >= 1
    assert any("getsurrey.co.uk" in str(s.url) for s in res.sources)


@pytest.mark.anyio
async def test_end_to_end_suitability_stages():
    """Test local sentiment, financial model, and report synthesis stages."""
    run_id = "test-suitability-run"
    req = AssessmentRequest(postcode="RH4 1AD", budget_gbp=10_000_000.0)
    title = TitleOutput(title_number="RH12345", boundary_geojson={}, area_m2=20000.0)
    site = ConfirmedSite(position=Position(lat=51.2329, lon=-0.3315), capacity_mw=10.0, boundary=title)
    cap = CapacityOutput(viable=True, firm_mw=8.0, ceiling_mw=12.0, substation="Dorking 11kV", distance_km=0.5)

    node_in = NodeInput(run_id=run_id, request=req, site=site, capacity=cap)

    # 1. Local sentiment stage
    sent_out = await local_sentiment(node_in)
    assert sent_out.sources >= 1
    assert len(sent_out.artifacts) >= 1

    # 2. Market stage
    mkt_out = await market_revenue(node_in)
    assert mkt_out.revenue_gbp_per_mw_year > 0

    # 3. Financial stage
    grid = GridOutput(gate2_queue_position=24, indicative_connection_months=36)
    fin_in = FinancialInput(run_id=run_id, request=req, site=site, capacity=cap, grid=grid, market=mkt_out)
    fin_out = await financial_model(fin_in)
    assert len(fin_out.cases) == 3
    assert fin_out.recommended_h in (2, 4, 8)
    assert fin_out.rationale is not None

    # 4. Synthesis stage
    land = SiteLandOutput(land_use="Agricultural Grade 3")
    plan = PlanningOutput(consenting_route="LPA Planning Consent")
    all_artifacts = (
        title.artifacts + cap.artifacts + grid.artifacts + mkt_out.artifacts + sent_out.artifacts + fin_out.artifacts
    )

    synth_in = SynthesisInput(
        run_id=run_id,
        request=req,
        site=site,
        capacity=cap,
        grid=grid,
        site_land=land,
        market=mkt_out,
        financial=fin_out,
        planning=plan,
        sentiment=sent_out,
        artifacts=all_artifacts,
    )
    synth_out = await synthesise(synth_in)

    assert synth_out.verdict in ("go", "maybe", "no_go")
    assert len(synth_out.findings) >= 2
    # Verify every finding cites a valid artifact ID
    valid_ids = {a.id for a in all_artifacts}
    for f in synth_out.findings:
        for aid in f.artifact_ids:
            assert aid in valid_ids, f"Cited unknown artifact ID {aid}"

    report_path = Path(f"out/{run_id}/report.md")
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "Bessible BESS Suitability Assessment" in report_text
    assert "Storage Duration Comparison" in report_text


def test_decide_land_blockers_and_caveats():
    """Land blockers reject and land caveats caution, whatever words the check reasons use."""
    fin = FinancialOutput(
        cases=[
            DurationCase(duration_h=2, capex_gbp=3.9e6, npv_gbp=2.1e6, irr=0.195),
            DurationCase(duration_h=4, capex_gbp=6.7e6, npv_gbp=1.9e6, irr=0.140),
            DurationCase(duration_h=8, capex_gbp=12.3e6, npv_gbp=0.1e6, irr=0.082),
        ],
        recommended_h=4,
    )
    sent = SentimentOutput(opposition_index=0.20)
    blocked = SiteLandOutput(land_use="x", blockers=["32 MWh needs 0.65-0.97 ha; the title has 0.02."])
    verdict, rules = decide(fin, sent, blocked)
    assert verdict == "no_go"
    assert "0.02" in rules[0]
    caveated = SiteLandOutput(land_use="x", caveats=["Median slope 6.8% (limit 10%), relief 1.65 m."])
    assert decide(fin, sent, caveated)[0] == "maybe"
    minor = DataGap(stage="site_land", what="avoids_best_farmland", reason="Grade 3 not split.")
    assert decide(fin, sent, SiteLandOutput(land_use="x", gaps=[minor]))[0] == "go"
    material = DataGap(stage="site_land", what="outside_flood_zone_3", reason="EA failed.", could_block=True)
    verdict, rules = decide(fin, sent, SiteLandOutput(land_use="x", gaps=[material]))
    assert verdict == "maybe"
    assert "outside_flood_zone_3" in rules[0]
    both = SiteLandOutput(land_use="x", blockers=["Too small."], gaps=[material])
    assert decide(fin, sent, both)[0] == "no_go"  # a known blocker decides, whatever else is missing


def test_report_labels_each_gap():
    from bessible.stages.synthesis import _gap_lines

    lines = _gap_lines([
        DataGap(stage="site_land", what="outside_flood_zone_3", reason="EA failed.", retryable=True, could_block=True),
        DataGap(stage="site_land", what="avoids_best_farmland", reason="Grade 3 not split."),
    ])
    assert "## Data Gaps" in lines
    assert any("temporary, retry may fix; could hide a blocker" in ln for ln in lines)
    assert any("avoids_best_farmland** (no coverage here)" in ln for ln in lines)
    assert _gap_lines([]) == []
