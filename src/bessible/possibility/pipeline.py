"""Possibility results in the shapes the assessment pipeline already collates (`bessible.models`).

The final report is built only from `Artifact`s gathered in `SynthesisInput`, so each check becomes one artifact
and the whole report fills the `site_land` stage's `SiteLandOutput`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.models import Artifact, DataGap, SiteCheck, SiteLandOutput

from .hard import can_block

if TYPE_CHECKING:
    from .models import Check, PossibilityReport, Proposal

OUTCOME_WORDS = {"pass": "OK", "warn": "Caveat", "fail": "Blocker", "unknown": "Unknown"}
MAX_URL = 2083  # HttpUrl's limit; a query with a many-polygon site's WKT can pass it


def _citable(url: str) -> str:
    """The URL, or its endpoint without the query when the query (a long site WKT) makes it too long to store."""
    return url if len(url) <= MAX_URL else url.split("?", 1)[0]


def artifact_from(check: Check, run_id: str) -> Artifact | None:
    if not check.source_urls:
        return None  # an artifact must point at evidence
    return Artifact(
        id=f"site_land-{check.name}-{run_id[:8]}",
        stage="site_land",
        claim=f"{OUTCOME_WORDS[check.outcome]}: {check.label} — {check.reason}",
        details=check.details,
        source_url=HttpUrl(_citable(check.source_urls[0])),
        confidence=check.confidence,
        model_used=check.produced_by,
    )


def site_land_output(proposal: Proposal, report: PossibilityReport, run_id: str) -> SiteLandOutput:
    land = proposal.location.deterministic.land
    grades = ", ".join(f"{g.grade} {g.overlap_pct:g}%" for g in land.alc) if land and land.alc else "unknown"
    cited = [(check, artifact_from(check, run_id)) for check in report.checks]
    artifacts = [a for _, a in cited if a]
    return SiteLandOutput(
        land_use=f"Agricultural land classification: {grades}",
        constraints=report.blockers + report.caveats,
        blockers=report.blockers,
        caveats=report.caveats,
        gaps=[
            DataGap(
                stage="site_land",
                what=c.name,
                reason=c.reason,
                sources=c.failed_sources,
                retryable=bool(c.failed_sources),
                could_block=can_block(c.name, proposal.limits),
            )
            for c in report.checks
            if c.outcome == "unknown"
        ],
        checks=[
            SiteCheck(name=c.name, outcome=c.outcome, facts=c.facts, artifact_id=a.id if a else None) for c, a in cited
        ],
        artifacts=artifacts,
    )
