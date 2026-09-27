from __future__ import annotations

from datetime import date

import pytest
from pydantic_ai.models.test import TestModel

from bessible.models import NearbyProject, Position
from bessible.planning.evidence import (
    CitedStatement,
    PlanningSummary,
    load_policy,
    summarise,
    validate_citations,
)
from bessible.planning.ingest_repd import (
    RepdProject,
    RepdSnapshot,
    load_repd_snapshot,
    nearby_batteries,
    parse_repd_csv,
)

PROJECTS = [
    NearbyProject(
        id="repd-101",
        name="Dorking BESS Facility",
        mw=49.5,
        status="Application Approved",
        status_date=date(2024, 4, 18),
        distance_km=0.88,
    ),
    NearbyProject(
        id="repd-102",
        name="Brockham Battery Energy Storage",
        mw=20.0,
        status="Awaiting Construction",
        status_date=date(2023, 11, 22),
        distance_km=2.42,
    ),
]


def test_load_policy_all_have_sources():
    """Requirement 3.1: verify each policy item has an id and valid source."""
    policy = load_policy()
    assert len(policy) >= 3
    for item in policy:
        assert item.id.startswith("policy-")
        assert item.title
        assert item.statement
        assert str(item.source_url).startswith("http")


def test_validate_citations_accepts_valid():
    summary = PlanningSummary(
        statements=[
            CitedStatement(text="Dorking project was approved.", cites=["repd-101"]),
            CitedStatement(text="Batteries carved out of NSIP.", cites=["policy-nsip-carveout"]),
        ]
    )
    valid_ids = {"repd-101", "repd-102", "policy-nsip-carveout"}
    assert validate_citations(summary, valid_ids) is True


def test_validate_citations_rejects_empty_cites():
    summary = PlanningSummary(
        statements=[
            CitedStatement(text="Dorking project was approved.", cites=[]),
        ]
    )
    valid_ids = {"repd-101", "policy-nsip-carveout"}
    assert validate_citations(summary, valid_ids) is False


def test_validate_citations_rejects_unknown_id():
    summary = PlanningSummary(
        statements=[
            CitedStatement(text="Dorking project was approved.", cites=["repd-unknown-999"]),
        ]
    )
    valid_ids = {"repd-101", "policy-nsip-carveout"}
    assert validate_citations(summary, valid_ids) is False


@pytest.mark.anyio
async def test_summarise_with_valid_stub_model():
    valid_reply = {
        "statements": [
            {
                "text": "1 nearby battery facility is approved within 1 km.",
                "cites": ["repd-101"],
            },
            {
                "text": "Standalone battery storage is determined by the LPA.",
                "cites": ["policy-nsip-carveout"],
            },
        ]
    }
    stub = TestModel(custom_output_args=valid_reply)
    policy = load_policy()

    result = await summarise(PROJECTS, policy, model=stub)
    assert result is not None
    assert len(result.statements) == 2
    assert result.statements[0].cites == ["repd-101"]


@pytest.mark.anyio
async def test_summarise_rejects_uncited_statement():
    """Requirement: A summary with an uncited statement SHALL be rejected."""
    uncited_reply = {
        "statements": [
            {
                "text": "Several batteries are located nearby without citations.",
                "cites": [],
            }
        ]
    }
    stub = TestModel(custom_output_args=uncited_reply)
    policy = load_policy()

    result = await summarise(PROJECTS, policy, model=stub)
    assert result is None


@pytest.mark.anyio
async def test_summarise_rejects_unknown_citation_id():
    """Requirement: Citations must reference real records or fixed policy only."""
    invented_cite_reply = {
        "statements": [
            {
                "text": "The local council approved a huge battery project.",
                "cites": ["hallucinated-repd-id-9999"],
            }
        ]
    }
    stub = TestModel(custom_output_args=invented_cite_reply)
    policy = load_policy()

    result = await summarise(PROJECTS, policy, model=stub)
    assert result is None


CSV_HEADER = (
    "Old Ref ID,Ref ID,Record Last Updated (dd/mm/yyyy),Site Name,Technology Type,Installed Capacity (MWelec),"
    "Development Status,Development Status (short),Address,Post Code,X-coordinate,Y-coordinate,Planning Authority,"
    "Planning Application Reference,Planning Application Submitted,Planning Permission  Granted,Operational\n"
)


def test_parse_repd_csv_keeps_ref_id_and_spreadsheet_row():
    text = CSV_HEADER + (
        'A1,1,01/01/2020,Some Wind Farm,Wind Onshore,10,Operational,Operational,"Line one\nLine two",AB1 2CD,'
        "400000,300000,Somewhere,W/1,01/01/2018,01/06/2018,01/01/2020\n"
        ",6909,31/07/2020,Dorking Battery,Battery,6,Operational,Operational,Dorking,RH4 1AA,516935,149040,"
        "Mole Valley,MO/2016/1168,15/08/2016,12/10/2016,01/07/2020\n"
        ",7000,01/02/2024,No Capacity Yet,Battery,,Application Submitted,Application Submitted,x,,516000,149000,"
        "Mole Valley,MO/2024/1,01/02/2024,,\n"
        ",7001,01/02/2024,No Coordinates,Battery,5,Application Submitted,Application Submitted,x,,,,,,,,\n"
    )
    projects = parse_repd_csv(text)
    assert [p.ref_id for p in projects] == ["6909", "7000"]
    dorking, blank = projects
    # The wind record spans two lines of text but is one spreadsheet row, so the battery is row 3.
    assert dorking.csv_row == 3
    assert dorking.id == "repd-6909"
    assert dorking.status_date == date(2020, 7, 1)
    assert dorking.planning_ref == "MO/2016/1168"
    assert dorking.latitude == pytest.approx(51.23, abs=0.01)
    assert dorking.longitude == pytest.approx(-0.33, abs=0.01)
    assert blank.mw is None


def test_nearby_batteries_cite_the_csv_row():
    snap = RepdSnapshot(
        fetched_at=date(2026, 9, 27),
        csv_url="https://assets.publishing.service.gov.uk/media/x/REPD_Publication_Q2_2026.csv",
        projects=[
            RepdProject(
                id="repd-6909",
                ref_id="6909",
                csv_row=5316,
                name="Dorking Battery",
                mw=6,
                status="Operational",
                status_date=date(2020, 7, 1),
                latitude=51.2412,
                longitude=-0.3421,
            )
        ],
    )
    [p] = nearby_batteries(Position(lat=51.2336, lon=-0.3385), snap=snap)
    assert p.ref_id == "6909"
    assert str(p.source_url).endswith("REPD_Publication_Q2_2026.csv#row=5316")
    assert nearby_batteries(Position(lat=54.52, lon=-1.55), snap=snap) == []


def test_committed_snapshot_is_the_published_csv():
    snap = load_repd_snapshot()
    assert snap.csv_name
    assert snap.csv_name.endswith(".csv")
    assert len(snap.projects) > 1000
    assert all(p.id == f"repd-{p.ref_id}" and p.csv_row >= 2 for p in snap.projects)


@pytest.mark.anyio
async def test_regulatory_planning_wiring_with_stub():
    from bessible.models import (
        AssessmentRequest,
        CapacityOutput,
        ConfirmedSite,
        GridOutput,
        PlanningInput,
        SiteLandOutput,
        TitleOutput,
    )
    from bessible.stages.planning import regulatory_planning

    pos = Position(lat=51.2336, lon=-0.3385)
    inp = PlanningInput.model_construct(
        run_id="testrun-98765432",
        request=AssessmentRequest(postcode="RH4 3LZ"),
        site=ConfirmedSite.model_construct(
            position=pos,
            capacity_mw=15.0,
            boundary=TitleOutput.model_construct(),
            capacity=None,
            flexible_connection=False,
        ),
        capacity=CapacityOutput.model_construct(tia_threshold_mw=5),
        grid=GridOutput(),
        site_land=SiteLandOutput(land_use="Industrial", constraints=[]),
    )

    stub = TestModel(
        custom_output_args={
            "statements": [
                {
                    "text": "Dorking Battery Energy Storage System is an operational battery nearby.",
                    "cites": ["repd-6909"],
                }
            ]
        }
    )

    out = await regulatory_planning(inp, summary_model=stub)
    assert out.nearby
    repd_art = next(a for a in out.artifacts if a.id == "planning-repd-testrun-")
    assert f"Found {len(out.nearby)} battery storage project(s)" in repd_art.claim
    assert str(repd_art.source_url).endswith(".csv")

    nearest = out.nearby[0]
    row_art = next(a for a in out.artifacts if a.id.startswith(f"planning-repd-{nearest.ref_id}-"))
    assert f"REPD Ref ID {nearest.ref_id}, spreadsheet row {nearest.csv_row}" in row_art.claim
    assert str(row_art.source_url).endswith(f".csv#row={nearest.csv_row}")

    summary_art = next(a for a in out.artifacts if a.stage == "planning" and "planning-summary" in a.id)
    assert "Dorking" in summary_art.claim
