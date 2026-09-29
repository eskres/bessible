"""Planning risks from structured evidence: site/land check outcomes, REPD refusals and cited national guidance.

Every risk cites exactly one artifact that states the fact behind it. Nothing here reads the check reasons: a risk
follows from a check's name, outcome and facts, so rewording a reason in `possibility.hard` cannot change it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.models import Artifact, NearbyProject, PlanningRisk
from bessible.planning.ingest_repd import haversine_km
from bessible.possibility.models import CHECK_LABELS

if TYPE_CHECKING:
    from collections.abc import Callable

    from bessible.models import Position, SiteCheck
    from bessible.planning.ingest_repd import RepdSnapshot

type Facts = dict[str, bool | int | float | str | None]

# ------------------------------------------ site checks ----------------------------------------- #


def _flood(f: Facts) -> str:
    return (
        f"Flood risk: the site touches Flood Zone {f.get('zone')} "
        f"({f.get('zone_3_pct')}% of it in Zone 3, {f.get('zone_2_pct')}% in Zone 2)."
    )


def _designated(heading: str) -> Callable[[Facts], str]:
    return lambda f: f"{heading}: {f.get('designations')} covers up to {f.get('covered_pct')}% of the site."


# The site/land checks that bear on planning, and the risk a warn or fail states. Grid, area and slope checks
# decide whether a battery fits and connects, not whether it gets consent, so they raise no planning risk.
SITE_RISKS: dict[str, Callable[[Facts], str]] = {
    "outside_flood_zone_3": _flood,
    "outside_green_belt": lambda f: (
        f"Green Belt: the site is in the {f.get('green_belt_name') or 'unnamed'} Green Belt, "
        "so it needs very special circumstances."
    ),
    "avoids_best_farmland": lambda f: (
        f"Best and most versatile farmland ({f.get('grades')}): policy steers development away."
    ),
    "within_built_up_area": lambda _: (
        "Landscape and visual impact: the site touches no built-up area, so it is in open countryside."
    ),
    "clear_of_protected_landscape": _designated("Landscape and visual impact, protected landscape"),
    "clear_of_protected_ecology": _designated("Protected habitats"),
    "clear_of_protected_heritage": _designated("Heritage assets"),
}
SEVERITY = {"fail": 0, "warn": 1}
GAP_NOTE = "Its data is missing or incomplete, so the risk is unknown."


def site_risks(checks: list[SiteCheck], fallback_artifact_id: str) -> list[PlanningRisk]:
    """One risk per planning-relevant check that warned or failed; "not assessed" for one without data.

    A check cites its own artifact. `fallback_artifact_id` (the planning route artifact, which states the country)
    is cited only when a check had no source to link, e.g. an England-only layer at a site in Wales.
    """
    relevant = [c for c in checks if c.name in SITE_RISKS]
    asserted = sorted((c for c in relevant if c.outcome in SEVERITY), key=lambda c: SEVERITY[c.outcome])
    risks = [
        PlanningRisk(text=SITE_RISKS[c.name](c.facts), artifact_id=c.artifact_id or fallback_artifact_id, source=c.name)
        for c in asserted
    ]
    risks += [
        PlanningRisk(
            text=f"Not assessed: {CHECK_LABELS.get(c.name, c.name).lower()}. {GAP_NOTE}",
            artifact_id=c.artifact_id or fallback_artifact_id,
            source=c.name,
            assessed=False,
        )
        for c in relevant
        if c.outcome == "unknown"
    ]
    return risks


# ---------------------------------------- fire safety ----------------------------------------- #

NFCC_URL = (
    "https://nfcc.org.uk/our-services/building-safety/"
    "grid-scale-energy-storage-system-planning-guidance-for-fire-and-rescue-services/"
)
# Verbatim from the guidance page (approved December 2025), sections 1 "Scope" and 4 "Planning approval process".
NFCC_SCOPE = (
    "This guidance relates to battery energy storage systems (BESS) which are deployed in open air environments, "
    "with an energy capacity of one megawatt hour (MWh) or greater using lithium variant batteries."
)
NFCC_ENGAGE = "fire and rescue services should be part of early conversations regarding BESS proposals."
NFCC_NOT_CONSULTEE = (
    "the fire and rescue service are not a statutory consultee in planning applications for BESS sites."
)
NFCC_MIN_MWH = 1.0  # the guidance's own scope threshold
SHORTEST_DURATION_H = 2  # the shortest duration the financial model assesses


def fire_safety(capacity_mw: float, run_id: str) -> tuple[PlanningRisk, Artifact] | None:
    """The NFCC guidance's scope covers this battery when even its shortest duration stores 1 MWh or more."""
    if capacity_mw * SHORTEST_DURATION_H < NFCC_MIN_MWH:
        return None
    artifact = Artifact(
        id=f"planning-nfcc-{run_id[:8]}",
        stage="planning",
        claim=(
            "NFCC, Grid Scale Energy Storage System Planning: Guidance for Fire and Rescue Services (December 2025): "
            f'"{NFCC_SCOPE}" "As such, {NFCC_NOT_CONSULTEE}" "As such, {NFCC_ENGAGE}"'
        ),
        source_url=HttpUrl(NFCC_URL),
        confidence=0.95,
        model_used="none (published guidance, quoted)",
    )
    risk = PlanningRisk(
        text=(
            f"Fire safety: this {capacity_mw:g} MW battery stores 1 MWh or more, so the NFCC grid-scale BESS guidance "
            "covers it if its cells are lithium. The local fire and rescue service should be part of early "
            "conversations; it is not a statutory consultee."
        ),
        artifact_id=artifact.id,
        source="nfcc_guidance",
    )
    return risk, artifact


# ---------------------------------------- local precedent --------------------------------------- #

REFUSED = frozenset({"Application Refused", "Appeal Refused"})
PRECEDENT_RADIUS_KM = 5.0  # the same radius as the REPD evidence card
MAX_NAMED = 3  # refusals named in the risk sentence; the artifact lists them all
_COUNCIL_WORDS = re.compile(
    r"\b(lpa|council|district|borough|city|county|metropolitan|london|royal|of|the)\b|[^a-z ]", re.IGNORECASE
)


def authority_key(name: str | None) -> str:
    """'Mole Valley District Council' and REPD's 'Mole Valley' both give 'mole valley'."""
    return " ".join(_COUNCIL_WORDS.sub(" ", name or "").lower().split())


def refusals(pos: Position, lpa_name: str | None, snap: RepdSnapshot) -> list[NearbyProject]:
    """REPD battery records refused (at application or appeal) within 5 km or in the same planning authority."""
    lpa = authority_key(lpa_name)
    found: list[NearbyProject] = []
    for p in snap.projects:
        if p.status not in REFUSED:
            continue
        distance = haversine_km(pos, p.latitude, p.longitude)
        if distance > PRECEDENT_RADIUS_KM and not (lpa and authority_key(p.planning_authority) == lpa):
            continue
        found.append(
            NearbyProject(
                id=p.id,
                ref_id=p.ref_id,
                csv_row=p.csv_row,
                name=p.name,
                mw=p.mw,
                status=p.status,
                status_date=p.status_date,
                distance_km=round(distance, 2),
                planning_authority=p.planning_authority,
                planning_ref=p.planning_ref,
                source_url=snap.row_url(p),
            )
        )
    return sorted(found, key=lambda p: p.distance_km)


def _cite(p: NearbyProject) -> str:
    return f"REPD Ref ID {p.ref_id}, CSV row {p.csv_row}"


def precedent(
    refused: list[NearbyProject], lpa_name: str | None, snap: RepdSnapshot, run_id: str
) -> tuple[PlanningRisk, Artifact] | None:
    """One risk for every battery refusal nearby or in the authority, citing one artifact that lists each record."""
    if not refused:
        return None
    where = f"within {PRECEDENT_RADIUS_KM:g} km" + (f" or in {lpa_name}" if lpa_name else "")
    lines = [
        f"{p.name}: {p.capacity}, {p.status} on {p.status_date.isoformat()}, {p.distance_km:g} km away"
        + (f", {p.planning_authority}" if p.planning_authority else "")
        + (f", application {p.planning_ref}" if p.planning_ref else "")
        + f" ({_cite(p)})"
        for p in refused
    ]
    artifact = Artifact(
        id=f"planning-refusals-{run_id[:8]}",
        stage="planning",
        claim=f"{len(refused)} battery application(s) refused {where}, in DESNZ REPD {snap.csv_name or ''}: "
        + "; ".join(lines),
        details=lines,
        source_url=snap.csv_url or snap.dataset_url,
        confidence=0.95,
        model_used="none (DESNZ REPD snapshot)",
    )
    named = "; ".join(f"{p.name} ({p.status}, {p.distance_km:g} km; {_cite(p)})" for p in refused[:MAX_NAMED])
    more = f"; and {len(refused) - MAX_NAMED} more" if len(refused) > MAX_NAMED else ""
    risk = PlanningRisk(
        text=f"Local precedent: {len(refused)} battery application(s) refused {where}: {named}{more}.",
        artifact_id=artifact.id,
        source="repd_refusals",
    )
    return risk, artifact
