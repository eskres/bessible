"""Planning risks come from check outcomes, REPD refusals and quoted guidance, and each cites an artifact of the run."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import HttpUrl

from bessible.location.models import FloodRisk, Land, Locality
from bessible.models import (
    AssessmentRequest,
    CapacityOutput,
    ConfirmedSite,
    GridOutput,
    PlanningInput,
    PlanningOutput,
    PlanningRisk,
    Position,
    SiteLandOutput,
    TitleOutput,
)
from bessible.planning import risks
from bessible.planning.ingest_repd import RepdProject, RepdSnapshot
from bessible.planning.route import LpaLookup
from bessible.possibility import Proposal, assess
from bessible.possibility.pipeline import site_land_output
from bessible.stages.planning import derive_planning_risks, regulatory_planning
from tests.possibility.test_hard import good_site

RUN = "riskrun-1234"
PLANNING_ART = "planning-riskrun-"
SITE = Position(lat=51.0, lon=-1.0)


def site_land(**site) -> SiteLandOutput:
    proposal = Proposal(location=good_site(**site), battery_mw=20)
    return site_land_output(proposal, assess(proposal), RUN)


def urban() -> SiteLandOutput:
    """(a) In a built-up area, no designations: `good_site` as it stands."""
    return site_land()


def rural_green_belt_flood_zone_3() -> SiteLandOutput:
    """(b) Countryside in the Metropolitan Green Belt, 40 % in Flood Zone 3."""
    return site_land(
        locality=Locality(country="England"),
        flood=FloodRisk(zone=3, zone_2_pct=10, zone_3_pct=40),
        land=Land(green_belt=True, green_belt_name="Metropolitan Green Belt", best_and_most_versatile=False),
    )


def with_gaps() -> SiteLandOutput:
    """(c) The flood and planning.data layers failed: flood, land, built-up area and heritage are unknown."""
    location = good_site(flood=None, land=None)
    failed = {"EA: flood zones", "Planning Data: designations on the title"}
    location.sources = [
        s.model_copy(update={"status": "failed", "detail": "HTTP 503"}) if s.name in failed else s
        for s in location.sources
    ]
    proposal = Proposal(location=location, battery_mw=20)
    return site_land_output(proposal, assess(proposal), RUN)


def assert_cited(found: list[PlanningRisk], land: SiteLandOutput) -> None:
    ids = {a.id for a in land.artifacts} | {PLANNING_ART}
    assert all(r.artifact_id in ids for r in found), [r.artifact_id for r in found]


def test_urban_site_has_no_site_risks():
    land = urban()
    assert derive_planning_risks(land, PLANNING_ART) == []


def test_rural_green_belt_flood_zone_3_site():
    land = rural_green_belt_flood_zone_3()
    found = derive_planning_risks(land, PLANNING_ART)
    assert [(r.source, r.artifact_id) for r in found] == [
        ("outside_flood_zone_3", f"site_land-outside_flood_zone_3-{RUN[:8]}"),
        ("outside_green_belt", f"site_land-outside_green_belt-{RUN[:8]}"),
        ("within_built_up_area", f"site_land-within_built_up_area-{RUN[:8]}"),
    ]
    assert found[0].text == "Flood risk: the site touches Flood Zone 3 (40.0% of it in Zone 3, 10.0% in Zone 2)."
    assert "Metropolitan Green Belt" in found[1].text
    assert found[2].text.startswith("Landscape and visual impact")
    assert all(r.assessed for r in found)
    assert_cited(found, land)


def test_data_gaps_say_not_assessed_and_cite_the_gap():
    land = with_gaps()
    found = derive_planning_risks(land, PLANNING_ART)
    assert {r.source for r in found} == {
        "outside_flood_zone_3",
        "outside_green_belt",
        "avoids_best_farmland",
        "within_built_up_area",
        "clear_of_protected_heritage",  # planning.data serves heritage; ecology and landscape come from Natural England
    }
    assert not any(r.assessed for r in found)
    assert all(r.text.startswith("Not assessed: ") for r in found)
    by_source = {r.source: r for r in found}
    # the gap's own artifact, which names the failed source
    flood = next(a for a in land.artifacts if a.id == by_source["outside_flood_zone_3"].artifact_id)
    assert "EA: flood zones failed" in flood.claim
    assert_cited(found, land)


def test_risks_do_not_read_reason_text():
    """Rewording a check's reason cannot change the risk: only name, outcome and facts count."""
    land = rural_green_belt_flood_zone_3()
    reworded = land.model_copy(update={"constraints": ["something else entirely"], "caveats": [], "blockers": []})
    assert derive_planning_risks(reworded, PLANNING_ART) == derive_planning_risks(land, PLANNING_ART)


def test_a_check_without_its_own_artifact_cites_the_fallback():
    land = SiteLandOutput(
        land_use="x", checks=[{"name": "outside_flood_zone_3", "outcome": "unknown", "artifact_id": None}]
    )
    (risk,) = derive_planning_risks(land, PLANNING_ART)
    assert risk.artifact_id == PLANNING_ART
    assert not risk.assessed


# ------------------------------------------ REPD refusals ----------------------------------------- #


def repd(ref: str, status: str, lat: float, lon: float, authority: str) -> RepdProject:
    return RepdProject(
        id=f"repd-{ref}",
        ref_id=ref,
        csv_row=int(ref) + 1,
        name=f"Battery {ref}",
        mw=49.9,
        status=status,
        status_date=date(2025, 3, 1),
        latitude=lat,
        longitude=lon,
        planning_authority=authority,
        planning_ref=f"25/{ref}",
    )


SNAP = RepdSnapshot(
    fetched_at=date(2026, 7, 1),
    csv_url=HttpUrl("https://assets.publishing.service.gov.uk/media/x/REPD_Q2_2026.csv"),
    projects=[
        repd("100", "Application Refused", 51.01, -1.0, "Elsewhere"),  # ~1.1 km: nearby
        repd("200", "Appeal Refused", 52.0, -1.0, "Testshire"),  # ~111 km, same authority
        repd("300", "Operational", 51.005, -1.0, "Testshire"),  # nearby, not refused
        repd("400", "Application Refused", 53.0, -1.0, "Far Away"),  # neither
    ],
)


def test_refusals_nearby_or_in_the_same_authority():
    found = risks.refusals(SITE, "Testshire District Council", SNAP)
    assert [p.ref_id for p in found] == ["100", "200"]
    assert str(found[0].source_url).endswith("#row=101")


def test_authority_names_match_across_sources():
    assert risks.authority_key("Mole Valley District Council") == risks.authority_key("Mole Valley")
    assert risks.authority_key("Darlington LPA") == "darlington"
    assert risks.authority_key("London Borough of Southwark") == "southwark"


def test_refusal_nearby_produces_a_cited_risk():
    refused = risks.refusals(SITE, "Testshire", SNAP)
    risk, artifact = risks.precedent(refused, "Testshire", SNAP, RUN)
    assert risk.artifact_id == artifact.id == f"planning-refusals-{RUN[:8]}"
    assert risk.source == "repd_refusals"
    assert "REPD Ref ID 100, CSV row 101" in risk.text
    assert "REPD Ref ID 200, CSV row 201" in artifact.claim
    assert len(artifact.details) == 2
    assert str(artifact.source_url) == str(SNAP.csv_url)


def test_no_refusals_no_risk():
    assert risks.precedent([], "Testshire", SNAP, RUN) is None


# ---------------------------------------------- fire --------------------------------------------- #


def test_fire_safety_quotes_nfcc_scope():
    risk, artifact = risks.fire_safety(20, RUN)
    assert risk.artifact_id == artifact.id
    assert risks.NFCC_SCOPE in artifact.claim
    assert str(artifact.source_url) == risks.NFCC_URL
    assert "not a statutory consultee" in risk.text


def test_fire_safety_out_of_scope_below_one_mwh():
    assert risks.fire_safety(0.4, RUN) is None


# --------------------------------------------- the stage ------------------------------------------ #


def stage_input(land: SiteLandOutput) -> PlanningInput:
    site = ConfirmedSite.model_construct(
        position=SITE,
        capacity_mw=20.0,
        boundary=TitleOutput.model_construct(),
        capacity=None,
        flexible_connection=False,
    )
    return PlanningInput.model_construct(
        run_id=RUN,
        request=AssessmentRequest(postcode="DL1 1AA"),
        site=site,
        capacity=CapacityOutput.model_construct(),
        grid=GridOutput(),
        site_land=land,
    )


@pytest.fixture
def offline(monkeypatch):
    async def lpa(*_a: object, **_k: object) -> LpaLookup:
        return LpaLookup(entity=1, reference="E60000001", name="Testshire LPA")

    monkeypatch.setattr("bessible.stages.planning.lookup_lpa", lpa)
    monkeypatch.setattr("bessible.stages.planning.get_repd_snapshot", lambda: SNAP)


@pytest.mark.anyio
@pytest.mark.parametrize("make", [urban, rural_green_belt_flood_zone_3, with_gaps])
async def test_every_risk_cites_an_artifact_of_the_run(offline, make):  # ruff: ignore[unused-function-argument]
    land = make()
    out = await regulatory_planning(stage_input(land))
    run_ids = {a.id for a in land.artifacts + out.artifacts}
    assert out.risks
    assert all(r.artifact_id in run_ids for r in out.risks)
    assert {"repd_refusals", "nfcc_guidance"} <= {r.source for r in out.risks}
    assert not any("adjacent countryside" in r.text for r in out.risks)  # the old fixed lines are gone


@pytest.mark.anyio
async def test_urban_and_rural_risks_differ(offline):  # ruff: ignore[unused-function-argument]
    town = await regulatory_planning(stage_input(urban()))
    country = await regulatory_planning(stage_input(rural_green_belt_flood_zone_3()))
    assert {r.source for r in town.risks} == {"repd_refusals", "nfcc_guidance"}
    assert {r.source for r in country.risks} - {r.source for r in town.risks} == {
        "outside_flood_zone_3",
        "outside_green_belt",
        "within_built_up_area",
    }


def test_legacy_string_risks_still_load():
    out = PlanningOutput.model_validate({"consenting_route": "x", "risks": ["Noise assessment [planning-abcd]"]})
    assert out.risks[0].text == "Noise assessment"
    assert out.risks[0].artifact_id == "planning-abcd"
