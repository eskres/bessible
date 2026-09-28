"""Re-assess a recorded demo run for the site the visitor confirmed, without a model key.

A replay is recorded for one pin. When the visitor moves the pin, changes the capacity or clicks other polygons, the
stages that read free public data and fixed rules run again for their site: capacity at the pin (the check its
confirmation card showed), title confirmation, grid, site and land, market, financial, planning (without its
model-written summary) and synthesis. Local sentiment keeps the recording: the pin cannot leave the screening
radius, so the local news is the same, and reading it needs a model.
"""

from __future__ import annotations

import asyncio
import math

from bessible import activities, events, stages
from bessible.api.capacity import capacity_at
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    CapacityOutput,
    ConfirmedSite,
    FinancialInput,
    NodeInput,
    PlanningInput,
    PlanningOutput,
    Position,
    RunStatus,
    SentimentOutput,
    SiteDecision,
    SynthesisInput,
    TitleSiteInput,
)

SAME_PIN_M = 5.0  # a confirmed pin this close to the recorded one is the recorded site


def _distance_m(a: Position, b: Position) -> float:
    dy = (a.lat - b.lat) * 111_320
    dx = (a.lon - b.lon) * 111_320 * math.cos(math.radians(a.lat))
    return math.hypot(dx, dy)


def changes_site(decision: SiteDecision, gate: RunStatus, request: AssessmentRequest) -> bool:
    """The visitor chose something the recording did not: another pin, capacity, connection or polygons."""
    if gate.position is None or gate.capacity is None:
        return False
    moved = decision.position is not None and _distance_m(decision.position, gate.position) > SAME_PIN_M
    recorded_mw = round(gate.capacity.recommended_mw, 3)
    resized = decision.capacity_mw is not None and round(decision.capacity_mw, 3) != recorded_mw
    flex = decision.flexible_connection is not None and decision.flexible_connection != request.flexible_connection
    clicked = decision.title_ids is not None or bool(decision.added_ids) or bool(decision.user_title_numbers)
    return moved or resized or flex or clicked


def _recorded_sentiment(run_id: str, recorded: AssessmentResult) -> SentimentOutput | None:
    """The recording's sentiment: the pin stays within the screening radius, so the local news is the same."""
    sentiment = recorded.sentiment
    if sentiment is not None and sentiment.opposition_index is not None:
        events.emit(
            run_id, "sentiment", f"Local sentiment assessed (opposition index: {sentiment.opposition_index:.2f})"
        )
    return sentiment


async def _planning(inp: PlanningInput) -> PlanningOutput:
    """The planning stage without a model: the route, TIA and nearby projects, no written summary."""
    events.emit(inp.run_id, "planning", "Evaluating consenting routes and planning risk profile")
    res = await stages.planning.regulatory_planning(inp)
    events.emit(inp.run_id, "planning", f"Consenting route determined: {res.consenting_route}")
    return res


async def _confirm(
    run_id: str, request: AssessmentRequest, gate: RunStatus, decision: SiteDecision, recommended_mw: float
) -> ConfirmedSite:
    """The visitor's site: their pin, capacity and polygons, measured like the live confirmation step."""
    if gate.boundary is None or gate.position is None:
        msg = "The recording has no title or position at the confirmation step"
        raise ValueError(msg)
    position = decision.position or gate.position
    capacity_mw = decision.capacity_mw if decision.capacity_mw is not None else recommended_mw
    site_title = await activities.confirm_title_site(
        TitleSiteInput(
            run_id=run_id,
            title=gate.boundary,
            origin=gate.position,
            position=position,
            capacity_mw=capacity_mw,
            footprint_geojson=decision.footprint_geojson,
            title_ids=decision.title_ids,
            added_ids=decision.added_ids,
            user_title_numbers=decision.user_title_numbers,
        )
    )
    return ConfirmedSite(
        position=position,
        capacity_mw=capacity_mw,
        boundary=site_title,
        footprint_geojson=decision.footprint_geojson,
        flexible_connection=(
            decision.flexible_connection if decision.flexible_connection is not None else request.flexible_connection
        ),
    )


async def _capacity_at_pin(
    run_id: str, request: AssessmentRequest, recorded: CapacityOutput, gate: RunStatus, decision: SiteDecision
) -> CapacityOutput:
    """The capacity where the visitor confirmed, as their confirmation card showed it; the recording's if unmoved."""
    if (
        decision.position is None
        or gate.position is None
        or _distance_m(decision.position, gate.position) <= SAME_PIN_M
    ):
        return recorded
    flexible = decision.flexible_connection if decision.flexible_connection is not None else request.flexible_connection
    events.emit(run_id, "capacity", "Evaluating grid capacity and primary substations")
    here = await capacity_at(decision.position, flexible=flexible, run_id=run_id)
    if here.out_of_area or not here.viable:
        events.emit(run_id, "capacity", "No viable capacity at the confirmed pin: keeping the original proposal")
        return recorded
    events.emit(
        run_id, "capacity", f"Grid capacity evaluated: {here.recommended_mw:g} MW at {here.substation} (viable)"
    )
    return here


async def rerun(
    run_id: str,
    request: AssessmentRequest,
    gate: RunStatus,
    recorded: AssessmentResult,
    decision: SiteDecision,
) -> AssessmentResult:
    """The assessment of the visitor's site; its `site.boundary` is the confirmed title the map reads.

    `gate` is the recorded status at the confirmation step: it holds the capacity proposal, the title candidates
    and the pin they were searched around. Trace events go to `out/<run_id>/events.jsonl`, like a live run's.
    """
    if gate.capacity is None or gate.boundary is None:
        msg = "The recording has no capacity or title at the confirmation step"
        raise ValueError(msg)
    capacity = await _capacity_at_pin(run_id, request, gate.capacity, gate, decision)
    site = await _confirm(run_id, request, gate, decision, capacity.recommended_mw)

    node_in = NodeInput(run_id=run_id, request=request, site=site, capacity=capacity)
    grid, site_land, market = await asyncio.gather(
        activities.grid_connection(node_in), activities.site_land(node_in), activities.market_revenue(node_in)
    )
    sentiment = _recorded_sentiment(run_id, recorded)

    common = {"run_id": run_id, "request": request, "site": site, "capacity": capacity, "grid": grid}
    financial, planning = await asyncio.gather(
        activities.financial_model(FinancialInput(**common, market=market, site_land=site_land)),
        _planning(PlanningInput(**common, site_land=site_land)),
    )

    analysis = [grid, site_land, market, *([sentiment] if sentiment else []), financial, planning]
    artifacts = (
        [a for a in recorded.artifacts if a.stage == "location"]
        + capacity.artifacts
        + gate.boundary.artifacts
        + site.boundary.artifacts
        + [a for out in analysis for a in out.artifacts]
    )
    report = await activities.synthesise(
        SynthesisInput(
            **common,
            site_land=site_land,
            market=market,
            financial=financial,
            planning=planning,
            sentiment=sentiment,
            artifacts=artifacts,
        )
    )
    gaps = [g for out in (grid, site_land, market, sentiment) if out for g in out.gaps]
    return AssessmentResult(
        status="completed",
        report=report,
        financial=financial,
        sentiment=sentiment,
        site=site,
        capacity=capacity,
        artifacts=artifacts + report.artifacts,
        gaps=gaps,
        run_id=run_id,
        postcode=recorded.postcode,
        run_dir=f"out/{run_id}",
    )
