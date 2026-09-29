from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl

from bessible.models import (
    Artifact,
    AssessmentRequest,
    CapacityOutput,
    ConfirmedSite,
    GridOutput,
    PlanningInput,
    Position,
    SiteLandOutput,
    TitleOutput,
)
from bessible.planning.route import (
    ROUTE_ENGLAND,
    ROUTE_OUTSIDE_ENGLAND,
    LpaLookup,
    consenting_route,
    lookup_lpa,
)
from bessible.stages.planning import regulatory_planning

FIXTURE = Path(__file__).parent / "api" / "fixtures" / "planning_data_entity_darlington.json"
POS = Position(lat=54.52, lon=-1.55)
POS_WALES = Position(lat=51.48, lon=-3.18)


def client(handler: httpx.MockTransport | None = None) -> httpx.AsyncClient:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.params["dataset"] == "local-planning-authority"
        assert request.url.params["latitude"] == "54.52"
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

    return httpx.AsyncClient(transport=handler or httpx.MockTransport(ok))


@pytest.mark.anyio
async def test_lookup_lpa_finds_authority():
    lpa = await lookup_lpa(POS, client())
    assert lpa == LpaLookup(entity=626002, reference="E60000002", name="Darlington LPA", country="England")
    assert lpa.source_url == "https://www.planning.data.gov.uk/entity/626002"


@pytest.mark.anyio
async def test_lookup_lpa_none_when_no_match():
    empty = httpx.MockTransport(lambda _r: httpx.Response(200, json={"entities": [], "links": {}, "count": 0}))
    assert await lookup_lpa(POS, client(empty)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 404])
async def test_lookup_lpa_none_on_http_error(status):
    down = httpx.MockTransport(lambda _r: httpx.Response(status, text="nope"))
    assert await lookup_lpa(POS, client(down)) is None


@pytest.mark.anyio
async def test_lookup_lpa_falls_back_to_postcodes_for_wales():
    def mock_router(request: httpx.Request) -> httpx.Response:
        if "planning.data.gov.uk" in request.url.host:
            return httpx.Response(200, json={"entities": [], "links": {}, "count": 0})
        if "postcodes.io" in request.url.host:
            body = {
                "status": 200,
                "result": [
                    {
                        "postcode": "CF10 1EP",
                        "outcode": "CF10",
                        "incode": "1EP",
                        "quality": 1,
                        "country": "Wales",
                        "admin_district": "Cardiff",
                        "codes": {"admin_district": "W06000015"},
                    }
                ],
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    cl = httpx.AsyncClient(transport=httpx.MockTransport(mock_router))
    lpa = await lookup_lpa(POS_WALES, cl)
    assert lpa is not None
    assert lpa.country == "Wales"
    assert lpa.name == "Cardiff"
    assert lpa.reference == "W06000015"


def test_route_does_not_depend_on_capacity():
    lpa = LpaLookup(entity=1, reference="E60000001", name="Test LPA", country="England")
    s6 = consenting_route(lpa, mw=6.0)
    s49 = consenting_route(lpa, mw=49.0)
    assert s6.route == ROUTE_ENGLAND
    assert s49.route == ROUTE_ENGLAND
    assert s6.route == s49.route
    assert "no Development Consent Order threshold" in s6.note


def test_route_unknown_lpa_keeps_route():
    s = consenting_route(None)
    assert s.route == ROUTE_ENGLAND
    assert s.lpa is None
    assert "could not be found" in s.note


def test_route_outside_england_flagged():
    lpa_wales = LpaLookup(entity=None, reference="W06000015", name="Cardiff", country="Wales")
    s = consenting_route(lpa_wales)
    assert s.route == ROUTE_OUTSIDE_ENGLAND
    assert "The site is in Wales" in s.note
    assert "does not apply" in s.note


def planning_input(
    constraints: list[str] | None = None,
    land_artifacts: list[Artifact] | None = None,
) -> PlanningInput:
    req = AssessmentRequest(postcode="DL1 1AA")
    cap = CapacityOutput.model_construct()
    site = ConfirmedSite.model_construct(
        position=POS, capacity_mw=6.0, boundary=TitleOutput.model_construct(), capacity=None, flexible_connection=False
    )
    art = land_artifacts or [
        Artifact(
            id="site_land-1234",
            stage="site_land",
            claim="Land constraint analysis",
            source_url=HttpUrl("https://magic.defra.gov.uk"),
            confidence=0.9,
            model_used="dummy",
        )
    ]
    return PlanningInput.model_construct(
        run_id="planrun-1234",
        request=req,
        site=site,
        capacity=cap,
        grid=GridOutput(),
        site_land=SiteLandOutput(land_use="Agricultural", constraints=constraints or [], artifacts=art),
    )


@pytest.mark.anyio
async def test_stage_names_lpa_with_source():
    out = await regulatory_planning(planning_input())
    assert "Darlington LPA" in out.consenting_route
    art = out.artifacts[0]
    assert str(art.source_url) == "https://www.planning.data.gov.uk/entity/626002"
    assert "Darlington LPA" in art.claim


@pytest.mark.anyio
async def test_stage_unknown_lpa(monkeypatch):
    async def none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("bessible.stages.planning.lookup_lpa", none)
    out = await regulatory_planning(planning_input())
    assert out.consenting_route == ROUTE_ENGLAND
    assert "Authority unknown" in out.artifacts[0].claim


@pytest.mark.anyio
async def test_stage_wales_flagged(monkeypatch):
    async def wales(*_a: object, **_k: object) -> LpaLookup:
        return LpaLookup(entity=None, reference="W06000015", name="Cardiff", country="Wales")

    monkeypatch.setattr("bessible.stages.planning.lookup_lpa", wales)
    out = await regulatory_planning(planning_input())
    assert ROUTE_OUTSIDE_ENGLAND in out.consenting_route
    assert "Wales" in out.artifacts[0].claim
