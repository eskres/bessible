"""Indicative connection timescale from queue dates at West Weybridge GSP (Dorking Town 11kV), on saved responses."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from bessible import queue_dates as qd
from bessible.api import ckan, neso, ukpn
from bessible.config import settings
from bessible.models import AssessmentRequest, CapacityOutput, ConfirmedSite, NodeInput, Position, TitleOutput
from bessible.stages import grid

if TYPE_CHECKING:
    from bessible.models import GridOutput

FIX = Path(__file__).parent / "api" / "fixtures"
APPENDIX_G = "ukpn_appendix_g_dorking.json"  # every West Weybridge row, fetched 2026-09-28
TEC = "neso_tec_at_sites_west_weybridge.json"  # TEC rows at "West Weybridge", fetched 2026-09-28
TODAY = date(2026, 9, 28)


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def appendix_g() -> list[ukpn.AppendixGRecord]:
    return ukpn.DATASETS["appendix_g"].parse(load(APPENDIX_G)).results


def tec() -> list[neso.TecRegisterRecord]:
    r = ckan.DatastoreSearchSqlResponse[neso.TecRegisterRecord].model_validate(load(TEC))
    assert r.result is not None
    return r.result.records


def west_weybridge() -> qd.QueueDates:
    g, g_rows = qd.from_appendix_g(appendix_g(), "West Weybridge")
    t, t_rows = qd.from_tec(tec())
    return qd.QueueDates(
        gsp="West Weybridge", connections=g + t, rows={"UKPN Appendix G": g_rows, "NESO TEC": t_rows}, failed=[]
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("46296", date(2026, 10, 1)),  # Excel day serial
        ("31/10/2027", date(2027, 10, 31)),
        ("Connected", None),
        (None, None),
    ],
)
def test_appendix_g_date(text: str | None, expected: date | None) -> None:
    assert qd.appendix_g_date(text) == expected


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("West Weybridge", "WEST WEYBRIDGE", True),
        ("Hackney 132kV", "HACKNEY 132", True),
        ("Hackney 66kV", "HACKNEY 132", False),
        ("Barking C 132kV", "BARKING C (EPN)", True),
        ("Tilbury 1&7", "TILBURY", True),
        ("Willesden 132kV", "WILLESDEN 66", False),
        ("West Weybridge", "WEST HAM", False),
    ],
)
def test_same_gsp(a: str, b: str, same: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument] - a parametrize column
    assert qd.same_gsp(a, b) is same


def test_tec_site_drops_voltage_and_circuits() -> None:
    assert qd.tec_site("Wimbledon 1&2 132kV") == "Wimbledon"
    assert qd.tec_site("West Weybridge") == "West Weybridge"


def test_appendix_g_at_west_weybridge() -> None:
    got, queued = qd.from_appendix_g(appendix_g(), "West Weybridge")
    assert queued == 4  # 14 rows, 10 connected
    assert sorted(c.connection_date for c in got) == [date(2025, 10, 10), date(2026, 10, 1)]
    assert {c.status for c in got} == {"Gate 2 - Protected 26-27 able to connect enduring"}
    assert qd.from_appendix_g(appendix_g(), "Chessington") == ([], 0)


def test_tec_at_west_weybridge() -> None:
    got, queued = qd.from_tec(tec())
    assert queued == 3
    assert sorted(c.connection_date for c in got) == [date(2025, 10, 31), date(2027, 10, 31), date(2027, 10, 31)]


def test_indicative_timescale() -> None:
    t = qd.indicative_timescale(west_weybridge(), TODAY)
    assert t is not None
    assert (t.records, t.lapsed) == (3, 2)  # 2025-10-10 and 2025-10-31 have passed
    assert t.by_register == {"UKPN Appendix G": 1, "NESO TEC": 2}
    assert (t.min_months, t.median_months, t.max_months) == (0.1, 13.1, 13.1)
    assert t.confidence == pytest.approx(0.4)


def test_confidence_grows_with_count() -> None:
    values = [qd.confidence_for(n) for n in (1, 3, 10, 100)]
    assert values == sorted(values)
    assert values[-1] < qd.MAX_CONFIDENCE


def test_no_future_dates_gives_none() -> None:
    assert qd.indicative_timescale(west_weybridge(), date(2028, 1, 1)) is None


def test_artifact_names_sources_and_counts() -> None:
    queue = west_weybridge()
    t = qd.indicative_timescale(queue, TODAY)
    assert t is not None
    art = qd.timescale_artifact(t, queue, "grid-timescale-x")
    assert "median 13.1 months" in art.claim
    assert "1 from UKPN Appendix G (4 queued rows at the GSP)" in art.claim
    assert "2 from NESO TEC (3 queued rows at the GSP)" in art.claim
    assert "2 contracted dates already passed" in art.claim
    assert "ukpn-appendix-g" in art.claim
    assert str(art.source_url) == qd.TEC_URL
    assert art.confidence == t.confidence


def mock_client(calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "ukpowernetworks.opendatasoft.com":
            assert request.headers["Authorization"] == "Apikey test-key"
            return httpx.Response(200, json=load(APPENDIX_G))
        return httpx.Response(200, json=load(TEC))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_gsp_queue_dates_reads_both_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    monkeypatch.setattr(settings, "ukpn_api_key", SecretStr("test-key"))
    calls: list[str] = []
    async with mock_client(calls) as client:
        first = await qd.gsp_queue_dates("West Weybridge", client=client)
        second = await qd.gsp_queue_dates("West Weybridge", client=client)
    assert first == second
    assert len(calls) == 2  # the second read comes from out/cache
    assert first.rows == {"UKPN Appendix G": 4, "NESO TEC": 3}
    assert not first.failed


@pytest.mark.anyio
async def test_missing_key_or_failed_source_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    monkeypatch.setattr(settings, "ukpn_api_key", None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(503)))
    async with client:
        got = await qd.gsp_queue_dates("West Weybridge", client=client)
    assert got.failed == ["UKPN Appendix G", "NESO TEC"]
    assert got.connections == []


@pytest.mark.anyio
async def test_grid_stage_uses_queue_dates_when_snapshot_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(gsp: str, _today: date, artifact_id: str) -> tuple[int, qd.Artifact] | None:
        queue = west_weybridge()
        t = qd.indicative_timescale(queue, TODAY)
        assert gsp == "West Weybridge"
        assert t is not None
        return round(t.median_months), qd.timescale_artifact(t, queue, artifact_id)

    monkeypatch.setattr(grid, "queue_timescale", fake)
    monkeypatch.setattr(grid, "timescales", lambda *_: None)  # the live snapshot: no dates at any GSP
    req = AssessmentRequest(postcode="RH4 1AD")
    cap = CapacityOutput(viable=True, substation="Dorking Town 11kV", firm_mw=8.0, ceiling_mw=8.0)
    boundary = TitleOutput(area_m2=10_000.0)
    site = ConfirmedSite(position=Position(lat=51.2088, lon=-0.3475), capacity_mw=8.0, boundary=boundary)
    out: GridOutput = await grid.grid_connection(NodeInput(run_id="queue-dates", request=req, site=site, capacity=cap))
    assert out.indicative_connection_months == 13
    assert not [g for g in out.gaps if g.what == "connection_timescale"]
    art = next(a for a in out.artifacts if a.id.startswith("grid-timescale"))
    assert "NESO TEC" in art.claim
