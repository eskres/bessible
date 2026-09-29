"""Regulatory and planning consenting stage."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.config import settings
from bessible.models import Artifact, PlanningInput, PlanningOutput, PlanningRisk
from bessible.planning.evidence import load_policy, summarise
from bessible.planning.ingest_repd import DATASET_URL, get_repd_snapshot, nearby_batteries
from bessible.planning.risks import fire_safety, precedent, refusals, site_risks
from bessible.planning.route import consenting_route, lookup_lpa
from bessible.planning.tia import tia_statement

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from bessible.models import NearbyProject, SiteLandOutput

log = logging.getLogger(__name__)

MAX_PROJECT_ARTIFACTS = 5  # one card each for the nearest REPD projects; the summary card lists them all


def _repd_cite(p: NearbyProject) -> str:
    """`REPD Ref ID 1234, spreadsheet row 5678`: enough to find the record by hand in the CSV."""
    if p.ref_id is None:
        return f"REPD record {p.id}"
    return f"REPD Ref ID {p.ref_id}, spreadsheet row {p.csv_row}"


def derive_planning_risks(site_land: SiteLandOutput, planning_art_id: str) -> list[PlanningRisk]:
    """The site/land checks' planning risks, each citing its check's artifact (see `planning.risks`)."""
    return site_risks(site_land.checks, planning_art_id)


async def regulatory_planning(
    inp: PlanningInput,
    *,
    summary_model: Model | str | None = None,
) -> PlanningOutput:
    """Assess planning jurisdiction, consenting pathways, statutory risk factors, and nearby planning evidence."""
    lpa = await lookup_lpa(inp.site.position)
    statement = consenting_route(lpa, mw=inp.site.capacity_mw)

    if lpa and lpa.country != "England":
        claim = f"{statement.route}. Site in {lpa.country} ({lpa.name}). {statement.note}"
        source_url = HttpUrl(lpa.source_url)
        confidence = 0.95
        route_display = f"{statement.route} ({lpa.country})"
    elif lpa:
        claim = f"{statement.route}. Authority: {lpa.name} ({lpa.reference}). {statement.note}"
        source_url = HttpUrl(lpa.source_url)
        confidence = 0.95
        route_display = f"{statement.route} - {lpa.name}"
    else:
        claim = f"{statement.route}. Authority unknown. {statement.note}"
        source_url = HttpUrl("https://www.planning.data.gov.uk/dataset/local-planning-authority")
        confidence = 0.6
        route_display = statement.route

    planning_art = Artifact(
        id=f"planning-{inp.run_id[:8]}",
        stage="planning",
        claim=claim,
        source_url=source_url,
        confidence=confidence,
        model_used="none (planning.data.gov.uk lookup)",
    )

    # F2: Transmission Impact Assessment
    threshold = inp.capacity.tia_threshold_mw if inp.capacity else None
    capacity_mw = inp.site.capacity_mw
    tia = tia_statement(threshold, capacity_mw)
    tia_art = Artifact(
        id=f"planning-tia-{inp.run_id[:8]}",
        stage="planning",
        claim=tia.statement,
        source_url=tia.source_url,
        confidence=0.95 if tia.threshold_mw is not None else 0.5,
        model_used="none (deterministic rule)",
    )

    # F3: REPD planning evidence and summary. Each project is cited by REPD Ref ID and CSV row so it can be checked.
    try:
        snap = get_repd_snapshot()
        nearby = nearby_batteries(inp.site.position, snap=snap)
        repd_url = snap.csv_url or snap.dataset_url
        source_name = f"DESNZ REPD, {snap.csv_name}" if snap.csv_name else "DESNZ REPD snapshot"
        source_name += f" (fetched {snap.fetched_at.isoformat()})"
    except Exception:
        log.warning("Could not load REPD snapshot", exc_info=True)
        snap = None
        nearby = []
        repd_url = HttpUrl(DATASET_URL)
        source_name = "DESNZ REPD (snapshot unavailable)"

    if nearby:
        repd_claim = f"Found {len(nearby)} battery storage project(s) within 5 km in {source_name}: " + "; ".join(
            f"{p.name} ({p.capacity}, {p.status}, {p.distance_km:g} km; {_repd_cite(p)})" for p in nearby
        )
    else:
        repd_claim = f"No battery storage projects found within 5 km in {source_name}."

    repd_art = Artifact(
        id=f"planning-repd-{inp.run_id[:8]}",
        stage="planning",
        claim=repd_claim,
        source_url=repd_url,
        confidence=0.95,
        model_used="none (DESNZ REPD snapshot)",
    )
    project_arts = [
        Artifact(
            id=f"planning-repd-{p.ref_id or p.id}-{inp.run_id[:8]}",
            stage="planning",
            claim=(
                f"{p.name}: {p.capacity} battery, {p.status} (as of {p.status_date.isoformat()}), "
                f"{p.distance_km:g} km from the site"
                + (f"; planning application {p.planning_ref}" if p.planning_ref else "")
                + (f" at {p.planning_authority}" if p.planning_authority else "")
                + f". Source: {_repd_cite(p)}."
            ),
            source_url=p.source_url or repd_url,
            confidence=0.95,
            model_used="none (DESNZ REPD snapshot)",
        )
        for p in nearby[:MAX_PROJECT_ARTIFACTS]
    ]

    artifacts = [planning_art, tia_art, repd_art, *project_arts]

    # Risks: the site's own first, then refusal precedent and the fire guidance, with the gaps last
    lpa_name = lpa.name if lpa else None
    refused = precedent(refusals(inp.site.position, lpa_name, snap), lpa_name, snap, inp.run_id) if snap else None
    cited = [r for r in (refused, fire_safety(capacity_mw, inp.run_id)) if r]
    artifacts += [a for _, a in cited]
    site = derive_planning_risks(inp.site_land, planning_art.id)
    risks = [r for r in site if r.assessed] + [r for r, _ in cited] + [r for r in site if not r.assessed]

    # Summarise with the run's model, when the activity supplies one
    policy = load_policy()
    summary = None
    if summary_model is not None:
        try:
            summary = await summarise(nearby, policy, model=summary_model)
        except Exception:
            log.warning("Planning summary generation failed", exc_info=True)

    if summary and summary.statements:
        summary_claim = " ".join(s.text for s in summary.statements)
        summary_art = Artifact(
            id=f"planning-summary-{inp.run_id[:8]}",
            stage="planning",
            claim=summary_claim,
            source_url=repd_url,
            confidence=0.9,
            model_used=summary.model_used or settings.gemini_model,
        )
        artifacts.append(summary_art)

    return PlanningOutput(
        consenting_route=route_display,
        risks=risks,
        artifacts=artifacts,
        tia=tia,
        nearby=nearby,
    )
