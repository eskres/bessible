"""Title stage: INSPIRE polygons, footprint shares, title numbers from free sources, and the human's choice."""

from __future__ import annotations

import csv
import io
import json
import string
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bessible.api import planning_data
from bessible.config import settings
from bessible.models import (
    AssessmentRequest,
    CapacityOutput,
    LocationOutput,
    Position,
    SiteDecision,
    TitleInput,
    TitleSiteInput,
)
from bessible.stages.title import NO_POLYGON, confirm_title_site, find_title_boundaries
from bessible.titles import hmlr, parcels
from bessible.titles.numbers import find_title_numbers

if TYPE_CHECKING:
    from typing import Any

FIXTURES = Path(__file__).parent / "api" / "fixtures"
CHADHURST = Position(lat=51.21009, lon=-0.352547)  # the Savills farm listing's own point
ORIGIN = Position(lat=51.2, lon=-0.35)
DLAT = 0.001  # ~110 m
DLON = 0.0016  # ~110 m at this latitude


def _square(lon0: float, lat0: float, dlon: float = DLON, dlat: float = DLAT) -> list[list[list[float]]]:
    return [[[lon0, lat0], [lon0 + dlon, lat0], [lon0 + dlon, lat0 + dlat], [lon0, lat0 + dlat], [lon0, lat0]]]


def _response(squares: dict[str, list[list[list[float]]]]) -> planning_data.EntityGeoJsonResponse:
    """A planning.data title-boundary FeatureCollection of invented polygons, in the wire's shape."""
    template = json.loads((FIXTURES / "planning_data_title_boundary_dorking_60m.geojson").read_text())["features"][0]
    features = []
    for i, (ref, rings) in enumerate(squares.items()):
        props = {**template["properties"], "reference": ref, "entity": 12000000000 + i}
        features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": rings}, "properties": props})
    return planning_data.EntityGeoJsonResponse.model_validate({"type": "FeatureCollection", "features": features})


# Three fields side by side, west to east: A | B | C, each ~110 m square
FIELDS = {
    "1001": _square(ORIGIN.lon, ORIGIN.lat),
    "1002": _square(ORIGIN.lon + DLON, ORIGIN.lat),
    "1003": _square(ORIGIN.lon + 2 * DLON, ORIGIN.lat),
}


def _box(lon0: float, lat0: float, dlon: float, dlat: float) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": _square(lon0, lat0, dlon, dlat)}}


# ------------------------------------------ polygon search ------------------------------------------ #


def test_real_title_boundary_search_response() -> None:
    """A real saved planning.data polygon search (300 m around the Savills farm point) parses into parcels."""
    raw = (FIXTURES / "planning_data_title_boundary_chadhurst_300m.geojson").read_text()
    response = planning_data.EntityGeoJsonResponse.model_validate_json(raw)
    found = parcels.parcels_from_features(response, CHADHURST)

    assert len(found) == len({p.inspire_id for p in found}) == len(response.features)
    assert all(p.area_m2 > 0 and p.inspire_id.isdigit() for p in found)
    assert all(p.source_url and p.source_url.startswith("https://www.planning.data.gov.uk/entity/") for p in found)
    assert all(p.title_number is None for p in found)  # the index carries no title numbers

    pin = parcels.pin_parcel(found, CHADHURST)
    assert pin is not None
    assert pin.inspire_id == "34335755"
    assert found[0].inspire_id == pin.inspire_id  # nearest first: the pin's own polygon


def test_search_radius_rules() -> None:
    assert parcels.search_radius_m(None) == parcels.MIN_RADIUS_M
    assert parcels.search_radius_m(5) == parcels.MIN_RADIUS_M
    assert parcels.search_radius_m(199) == pytest.approx(759, abs=1)  # 1.5 x the radius of a 199-acre circle
    assert parcels.search_radius_m(100_000) == parcels.MAX_RADIUS_M


# --------------------------------------------- geometry --------------------------------------------- #


def test_footprint_on_one_polygon() -> None:
    candidates = parcels.parcels_from_features(_response(FIELDS), ORIGIN)
    footprint = parcels.footprint_shape(_box(ORIGIN.lon + 0.0004, ORIGIN.lat + 0.0003, 0.0006, 0.0004))

    site = parcels.select_site(candidates, footprint=footprint, origin=ORIGIN)

    assert [p.inspire_id for p in site] == ["1001"]
    assert site[0].footprint_overlap_pct == pytest.approx(100, abs=0.01)


def test_footprint_across_three_polygons_shares_add_up() -> None:
    candidates = parcels.parcels_from_features(_response(FIELDS), ORIGIN)
    # From the middle of A to the middle of C: 1/4 on A, 1/2 on B, 1/4 on C
    footprint = parcels.footprint_shape(_box(ORIGIN.lon + DLON / 2, ORIGIN.lat + 0.0002, 2 * DLON, 0.0005))

    site = parcels.select_site(candidates, footprint=footprint, origin=ORIGIN)

    shares = {p.inspire_id: p.footprint_overlap_pct for p in site}
    assert set(shares) == {"1001", "1002", "1003"}
    assert sum(shares.values()) == pytest.approx(100, abs=0.05)
    assert shares["1002"] == pytest.approx(50, abs=0.1)
    assert site[0].inspire_id == "1002"  # largest share first
    assert parcels.uncovered_pct(footprint, candidates, ORIGIN) == pytest.approx(0, abs=0.05)


def test_clicked_polygons_win_over_the_footprint() -> None:
    candidates = parcels.parcels_from_features(_response(FIELDS), ORIGIN)
    footprint = parcels.footprint_shape(_box(ORIGIN.lon + 0.0004, ORIGIN.lat + 0.0003, 0.0006, 0.0004))

    site = parcels.select_site(candidates, footprint=footprint, origin=ORIGIN, title_ids=["1002", "1003"])

    assert {p.inspire_id for p in site} == {"1002", "1003"}
    assert all(p.footprint_overlap_pct == 0 for p in site)


def test_pin_on_no_polygon() -> None:
    candidates = parcels.parcels_from_features(_response(FIELDS), ORIGIN)
    assert parcels.pin_parcel(candidates, Position(lat=ORIGIN.lat - 0.001, lon=ORIGIN.lon)) is None


# ------------------------------------------- stage, offline ------------------------------------------ #


def _title_input(pos: Position, **request: Any) -> TitleInput:
    req = AssessmentRequest(**({"position": pos} | request))
    cap = CapacityOutput(viable=True, firm_mw=20, ceiling_mw=20, recommended_mw=20)
    return TitleInput(
        run_id="test-titles", request=req, location=LocationOutput(postcode="RH5 6AA", position=pos), capacity=cap
    )


def _fake_search(monkeypatch: pytest.MonkeyPatch, squares: dict[str, list[list[list[float]]]]) -> None:
    async def fake(*_args: object, **_kwargs: object) -> tuple[planning_data.EntityGeoJsonResponse, bool]:
        return _response(squares), False

    monkeypatch.setattr("bessible.titles.search.search_titles", fake)


@pytest.mark.anyio
async def test_stage_pin_on_no_polygon_draws_no_box(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_search(monkeypatch, FIELDS)
    pos = Position(lat=ORIGIN.lat - 0.001, lon=ORIGIN.lon)  # just south of the fields: a lane

    out = await find_title_boundaries(_title_input(pos))

    assert out.pin_parcel is None
    assert NO_POLYGON in out.notes
    assert out.boundary_geojson == {}
    assert out.area_m2 == 0
    assert len(out.candidates) == len(FIELDS)
    assert out.artifacts[0].model_used == "planning.data title-boundary"
    assert "no INSPIRE polygon" in out.artifacts[0].claim


@pytest.mark.anyio
async def test_stage_then_confirm_across_two_polygons(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_search(monkeypatch, FIELDS)
    pin = Position(lat=ORIGIN.lat + 0.0005, lon=ORIGIN.lon + DLON * 0.9)  # in A, near B
    before = await find_title_boundaries(_title_input(pin))
    assert before.pin_parcel is not None
    assert before.pin_parcel.inspire_id == "1001"
    assert before.title_number is None

    footprint = _box(ORIGIN.lon + DLON * 0.8, ORIGIN.lat + 0.0003, DLON * 0.4, 0.0004)  # half on A, half on B
    after = await confirm_title_site(
        TitleSiteInput(
            run_id="test-titles",
            title=before,
            origin=pin,
            position=pin,
            capacity_mw=20,
            footprint_geojson=footprint,
            user_title_numbers={"1002": "SY123456"},
        )
    )

    assert after.inspire_ids == ["1001", "1002"] or after.inspire_ids == ["1002", "1001"]
    assert sum(p.footprint_overlap_pct or 0 for p in after.site_parcels) == pytest.approx(100, abs=0.05)
    b = next(p for p in after.site_parcels if p.inspire_id == "1002")
    assert (b.title_number, b.title_source, b.title_link) == ("SY123456", "user", "polygon")
    assert after.title_number is None  # the pin polygon (A) has no number
    assert after.candidates == []  # the confirmed output stays small for later stages
    assert after.area_m2 == pytest.approx(2 * 110 * 111, rel=0.05)
    claims = [a.claim for a in after.artifacts]
    assert any(c.startswith("INSPIRE polygon 1001") and "title number not known" in c for c in claims)
    assert any("SY123456" in c and "entered by the user, not checked" in c for c in claims)
    assert any(c.startswith("User confirmed the site") and "2 polygon(s)" in c for c in claims)
    assert {a.model_used for a in after.artifacts} == {"planning.data title-boundary", "user"}


@pytest.mark.anyio
async def test_stage_reads_title_numbers_from_the_cached_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_search(monkeypatch, FIELDS)
    url = "https://example.com/farm/1"
    page = settings.cache_dir / "pages"
    page.mkdir(parents=True, exist_ok=True)
    from bessible.location.fetch import live_cache_path

    live_cache_path(url).write_text(
        json.dumps({
            "url": url,
            "text": "Lot 1, 12 acres. The land is registered under title numbers SY123456 and SY654321.",
        })
    )
    inp = _title_input(Position(lat=ORIGIN.lat + 0.0005, lon=ORIGIN.lon + 0.0005))
    inp = inp.model_copy(update={"request": AssessmentRequest(property_url=url)})

    out = await find_title_boundaries(inp)

    assert [(n.title_number, n.source, n.link) for n in out.title_numbers] == [
        ("SY123456", "listing", "site"),
        ("SY654321", "listing", "site"),
    ]
    listing_arts = [a for a in out.artifacts if a.model_used == "listing text"]
    assert len(listing_arts) == 2
    assert all(str(a.source_url) == url for a in listing_arts)


# ------------------------------------------ the human's choice ---------------------------------------- #


def test_unknown_title_ids_rejected() -> None:
    decision = SiteDecision(confirmed=True, title_ids=["1001", "9999"])
    with pytest.raises(ValueError, match="9999"):
        decision.check_titles({"1001", "1002"})
    SiteDecision(confirmed=True, title_ids=["1001"]).check_titles({"1001", "1002"})


def test_added_ids_and_user_numbers_checked() -> None:
    with pytest.raises(ValueError, match="Not INSPIRE ids"):
        SiteDecision(confirmed=True, added_ids=["DROP TABLE"]).check_titles(set())
    with pytest.raises(ValueError, match="not candidates"):
        SiteDecision(confirmed=True, user_title_numbers={"42": "SY1"}).check_titles({"1001"})
    with pytest.raises(ValueError, match="not a title number"):
        SiteDecision(confirmed=True, user_title_numbers={"1001": "AB1 2CD X"})
    assert SiteDecision(confirmed=True, user_title_numbers={"1001": " sy 123456 "}).user_title_numbers == {
        "1001": "SY123456"
    }


# ------------------------------------------- listing text ------------------------------------------- #


def test_listing_pattern_finds_stated_title_numbers() -> None:
    found = find_title_numbers("The farm is registered under title numbers SY123456 and SY654321.")
    assert [f.title_number for f in found] == ["SY123456", "SY654321"]
    assert found[0].evidence == "The farm is registered under title numbers SY123456 and SY654321"


def test_listing_pattern_ignores_postcodes_grid_references_and_lots() -> None:
    text = (
        "Postcode AB1 2CD. OS grid reference TQ123456 (TQ 1234 5678). Lot 1 of 12 acres, Lot 2 (LOT12). "
        "Agent reference SY123456. What3words ///notes.indeed.filed. The title to the land is good."
    )
    assert find_title_numbers(text) == []


def test_listing_pattern_stops_at_the_list_end() -> None:
    found = find_title_numbers("Registered under title SY12345; grid reference TQ123456. Title No: K1234 or K5678")
    assert [f.title_number for f in found] == ["SY12345", "K1234", "K5678"]


# ---------------------------------------------- CCOD / OCOD ------------------------------------------ #


def _ownership_zip(path: Path) -> Path:
    """An invented CCOD monthly file: companies only, no real names."""
    header = [
        "Title Number", "Tenure", "Property Address", "District", "County", "Region", "Postcode",
        "Multiple Address Indicator", "Price Paid", "Proprietor Name (1)", "Company Registration No. (1)",
    ]  # fmt: skip
    rows = [
        ["SY100001", "Freehold", "Land at Example Farm, Nowhere Lane", "MOLE VALLEY", "SURREY", "SOUTH EAST",
         "RH5 6AA", "N", "", "EXAMPLE LAND LIMITED", string.octdigits],
        ["SY100002", "Leasehold", "Unit 1, Example Yard", "MOLE VALLEY", "SURREY", "SOUTH EAST",
         "RH5 6AB", "N", "", "SAMPLE HOLDINGS PLC", "07654321"],
    ]  # fmt: skip
    buf = io.StringIO()
    csv.writer(buf).writerows([header, *rows])
    zpath = path / "CCOD_FULL_2026_09.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("CCOD_FULL_2026_09.csv", buf.getvalue())
    return zpath


def test_ccod_index_and_lookup(tmp_path: Path) -> None:
    hmlr.build_index("ccod", _ownership_zip(tmp_path))

    assert hmlr.indexed_file("ccod") == "CCOD_FULL_2026_09.zip"
    rows = hmlr.lookup("ccod", ["rh5 6aa"])
    assert [(r.title_number, r.proprietor, r.tenure) for r in rows] == [
        ("SY100001", "EXAMPLE LAND LIMITED", "Freehold")
    ]
    assert hmlr.lookup("ocod", ["RH5 6AA"]) == []  # no OCOD index: nothing, no error


def test_paid_datasets_refused() -> None:
    with pytest.raises(hmlr.HmlrError, match="only the free"):
        hmlr.index_path("nps")


@pytest.mark.anyio
async def test_stage_reports_ccod_matches_as_site_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pydantic import SecretStr

    hmlr.build_index("ccod", _ownership_zip(tmp_path))
    monkeypatch.setattr(settings, "hmlr_api_key", SecretStr("test"))
    _fake_search(monkeypatch, FIELDS)

    async def no_nearby(*_args: object) -> list[str]:
        return []

    monkeypatch.setattr("bessible.stages.title._nearby_postcodes", no_nearby)

    out = await find_title_boundaries(_title_input(Position(lat=ORIGIN.lat + 0.0005, lon=ORIGIN.lon + 0.0005)))

    [number] = out.title_numbers
    assert (number.title_number, number.source, number.link) == ("SY100001", "ccod", "site")
    assert number.proprietor == "EXAMPLE LAND LIMITED"
    art = next(a for a in out.artifacts if a.model_used == "HMLR CCOD")
    assert "EXAMPLE LAND LIMITED" in art.claim
    assert "not tied to a polygon" in art.claim
    assert str(art.source_url) == hmlr.DATASET_URL["ccod"]
    assert any("OCOD not downloaded" in n for n in out.notes)
