"""Unit tests for Suitability Engine: local sentiment, financial model, and verdict."""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from bessible.classifier import Classified
from bessible.config import settings
from bessible.location import Agentic, Coordinates, Deterministic, Locality, LocationData
from bessible.location.transform import search_terms
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
from bessible.suitability.research import (
    KEYWORD_SELECTOR,
    cache_key,
    is_local,
    news_queries,
    research_local_news,
    search_request,
    verbatim,
)
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


def test_paragraphs_that_raise_no_concern_add_no_concern():
    fact = ParagraphLabels(relevant=True, stance="neutral", concern="no concern raised", mentions_risk=False)
    fire = ParagraphLabels(relevant=True, stance="against", concern="fire safety", mentions_risk=True)
    conf = {"relevant": 0.9, "stance": 0.9, "concern": 0.9, "mentions_risk": 0.9}
    items = [Classified(text="a", labels=fact, confidence=conf), Classified(text="b", labels=fire, confidence=conf)]
    assert compute_opposition_index(items)[1] == ["fire safety"]


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


DORKING = LocationData(
    coords=Coordinates(lat=51.2329, lon=-0.3315),
    deterministic=Deterministic(
        locality=Locality(place="Dorking", district="Mole Valley", planning_authority="Mole Valley")
    ),
    agentic=Agentic(search_terms=["Dorking", "Mole Valley", "Surrey"]),
)
PAGE_TEXT = (
    "Dorking battery plan\n\n"
    "Residents in Dorking have objected to a 40MW battery storage site on farmland north of the town, citing fire risk.\n"
    "Mole Valley District Council will decide the planning application for the battery scheme next month.\n"
    "Sign up to our newsletter for the latest stories from across Surrey and beyond every day.\n"
)
US_TEXT = "Residents in Austin have objected to a 40MW battery storage site on farmland north of the city.\n"


def _tavily_body(
    url: str = "https://www.example.co.uk/news/dorking-battery", text: str = PAGE_TEXT
) -> dict[str, object]:
    """A Search API body in the wire shape, for unit tests of the logic around it (not a recorded response)."""
    return {
        "query": "Dorking battery storage BESS planning application",
        "answer": None,
        "images": [],
        "results": [
            {"title": "Dorking battery plan", "url": url, "content": "Residents...", "score": 0.8, "raw_content": text},
            {
                "title": "US story",
                "url": "https://example.com/us",
                "content": "x",
                "score": 0.9,
                "raw_content": US_TEXT,
            },
        ],
        "response_time": 1.2,
        "usage": {"credits": 1},
        "request_id": "test",
    }


def _tavily_client(status: int = 200, calls: list[str] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content)["query"])
        if status != 200:
            return httpx.Response(status, json={"detail": {"error": "boom"}})
        return httpx.Response(200, json=_tavily_body())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_news_queries_come_from_location_data():
    queries = news_queries(DORKING)
    assert 1 <= len(queries) <= 4
    assert queries[0].startswith("Dorking ")
    assert any("Mole Valley" in q for q in queries)
    assert all(len(q) <= 400 for q in queries)


def test_news_queries_skip_unparished_areas_and_wards_named_after_the_place():
    where = DORKING.deterministic.locality.model_copy(update={"parish": "Mole Valley, unparished area"})
    terms = search_terms(where)
    assert not any("unparished" in t for t in terms)
    location = DORKING.model_copy(
        update={"agentic": DORKING.agentic.model_copy(update={"search_terms": ["Dorking", "Dorking North", *terms]})}
    )
    queries = news_queries(location)
    assert len(queries) == 4
    assert not any("unparished" in q or "Dorking North" in q for q in queries)


def test_non_uk_hosts_count_only_when_they_name_the_area():
    names = ["Dorking", "Mole Valley"]
    assert is_local("https://www.example.co.uk/x", "", names)
    assert is_local("https://newleatherheadliving.wordpress.com/x", "Dorking ... Mole Valley District Council", names)
    assert not is_local("https://www.barbadosparliament.com/x.pdf", "one mention of Dorking", names)
    assert not is_local("https://example.edu/cell.pdf", "Histone H3 and histone H4", ["Histon"])  # whole words only


def test_verbatim_normalises_whitespace_only():
    assert verbatim("Residents in  Dorking\nhave objected", PAGE_TEXT)
    assert not verbatim("Residents in Dorking objected", PAGE_TEXT)  # a paraphrase
    assert not verbatim("", PAGE_TEXT)


@pytest.mark.anyio
async def test_no_key_is_not_configured_not_no_coverage():
    from bessible.suitability.sentiment import process_sentiment

    calls: list[str] = []
    research = await research_local_news(DORKING, client=_tavily_client(calls=calls))
    assert calls == []  # no key, no request
    assert research.status == "not_configured"
    assert research.queries  # what would have been searched is still recorded
    out = await process_sentiment("run-12345678", research)
    assert out.opposition_index is None
    assert "not configured" in out.artifacts[0].claim
    assert "No relevant" not in out.artifacts[0].claim
    assert out.artifacts[0].confidence < 0.5
    assert out.artifacts[0].model_used == "none"
    assert out.gaps
    assert out.gaps[0].what == "local_news"


@pytest.mark.anyio
async def test_failed_news_search_is_a_retryable_gap_not_no_coverage(monkeypatch: pytest.MonkeyPatch):
    from bessible.suitability.sentiment import process_sentiment

    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))
    research = await research_local_news(DORKING, client=_tavily_client(status=500))
    assert research.status == "failed"
    assert research.retryable
    out = await process_sentiment("run-12345678", research)
    assert out.opposition_index is None
    assert out.gaps[0].retryable
    assert "not assessed" in out.artifacts[0].claim
    assert out.artifacts[0].confidence < 0.5


@pytest.mark.anyio
async def test_live_search_keeps_uk_pages_and_verbatim_paragraphs_then_caches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))
    calls: list[str] = []
    today = date(2026, 9, 27)
    research = await research_local_news(DORKING, client=_tavily_client(calls=calls), today=today)
    assert research.status == "searched"
    assert len(calls) == len(research.queries)
    assert research.credits == len(calls)
    assert research.results == 2 * len(calls)
    assert [str(s.url) for s in research.sources] == ["https://www.example.co.uk/news/dorking-battery"]  # .com dropped
    assert research.selected_by == KEYWORD_SELECTOR  # no model on this run
    paragraphs = research.sources[0].paragraphs
    assert paragraphs
    assert all(verbatim(p, PAGE_TEXT) for p in paragraphs)
    assert not any("newsletter" in p for p in paragraphs)

    # Second run: served from the dated cache under out/, no request, no credits, even without a key.
    monkeypatch.setattr(settings, "tavily_api_key", None)
    again = await research_local_news(DORKING, client=_tavily_client(calls=calls), today=today)
    assert len(calls) == len(research.queries)
    assert again.cached
    assert not again.recorded
    assert again.credits == 0
    assert again.fetched_on == today
    assert again.sources == research.sources
    assert (settings.cache_dir / "tavily").is_dir()


@pytest.mark.anyio
async def test_model_pick_not_in_page_text_is_dropped(monkeypatch: pytest.MonkeyPatch):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    real = "Residents in Dorking have objected to a 40MW battery storage site on farmland north of the town, citing fire risk."
    invented = "Hundreds of Dorking residents marched against the battery site on Saturday."

    def pick(_messages: list[object], info: AgentInfo) -> ModelResponse:
        picks = [{"article": 0, "quote": real}, {"article": 0, "quote": invented}, {"article": 7, "quote": real}]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"picks": picks})])

    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))
    research = await research_local_news(DORKING, model=FunctionModel(pick), client=_tavily_client())
    assert research.dropped == 2  # the invented quote and the pick from an article that does not exist
    assert research.sources[0].paragraphs == [real]
    assert research.selected_by is not None
    assert research.selected_by != KEYWORD_SELECTOR


@pytest.mark.anyio
async def test_recorded_response_serves_the_offline_demo(monkeypatch: pytest.MonkeyPatch):
    from bessible.suitability import research as research_module
    from bessible.suitability.sentiment import process_sentiment

    today = date(2026, 9, 27)
    recorded_dir = settings.cache_dir.parent / "recorded"
    recorded_dir.mkdir(parents=True)
    for q in news_queries(DORKING):
        req = search_request(q, today)
        entry = {"fetched_on": "2026-09-20", "recorded": True, "request": req.params(), "response": _tavily_body()}
        (recorded_dir / f"{cache_key(req)}.json").write_text(json.dumps(entry))
    monkeypatch.setattr(research_module, "RECORDED_DIR", recorded_dir)

    research = await research_local_news(DORKING, client=_tavily_client(status=500), today=today)  # no key
    assert research.recorded
    assert research.cached
    assert research.fetched_on == date(2026, 9, 20)
    out = await process_sentiment("run-12345678", research)
    search_art = next(a for a in out.artifacts if a.id.startswith("sentiment-search"))
    assert "recorded Tavily responses fetched 2026-09-20" in search_art.claim
    assert "Dorking battery storage BESS planning application" in search_art.claim
    index_art = next(a for a in out.artifacts if a.id.startswith("sentiment-index"))
    quotes = [a for a in out.artifacts if a.id.startswith("sentiment-p")]
    assert quotes
    assert all(str(a.source_url) == "https://www.example.co.uk/news/dorking-battery" for a in quotes)
    assert index_art.model_used == ", ".join(sorted({a.model_used for a in quotes}))  # the classifier that ran


@pytest.mark.anyio
async def test_searched_and_found_nothing_is_its_own_outcome(monkeypatch: pytest.MonkeyPatch):
    from bessible.suitability.sentiment import process_sentiment

    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_tavily_body(text="Nothing about energy here, only a long story about a village fete.")
        )

    research = await research_local_news(DORKING, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert research.status == "searched"
    assert not research.sources
    out = await process_sentiment("run-12345678", research)
    art = out.artifacts[0]
    assert "not assessed" not in art.claim
    assert "not configured" not in art.claim
    assert "Tavily searched" in art.claim
    assert "UK or local pages read" in art.claim
    assert 0.2 < art.confidence < 0.9
    assert art.model_used.startswith("Tavily search")
    assert out.opposition_index is None


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
    sent_out = await local_sentiment(node_in)  # offline: no Tavily key
    assert sent_out.opposition_index is None
    assert "not configured" in sent_out.artifacts[0].claim

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
