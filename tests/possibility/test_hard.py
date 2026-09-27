from __future__ import annotations

import pytest

from bessible.location import Coordinates, LocationData
from bessible.location.models import (
    AlcGrade,
    Designation,
    Deterministic,
    FloodRisk,
    Geometry,
    Grid,
    Headroom,
    Land,
    Locality,
    OverheadLine,
    SourceStatus,
    Substation,
    Terrain,
    TitleBoundary,
)
from bessible.possibility import HARD_CHECKS, Limits, Proposal, assess, hard

HERE = Coordinates(lat=51.0, lon=-1.0)
SQUARE = Geometry(type="Polygon", coordinates=[[[-1, 51], [-1, 51.001], [-0.999, 51.001], [-0.999, 51], [-1, 51]]])
SOURCE_NAMES = (
    "Planning Data: title boundary",
    "EA: flood zones",
    "Natural England: sssi",
    "NGED: network capacity map",
)


def title(area_ha=10.0):
    return TitleBoundary(
        inspire_id="123",
        geometry=SQUARE,
        area_m2=area_ha * 10_000,
        area_ha=area_ha,
        perimeter_m=400,
        centroid=HERE,
        bbox=(-1, 51, -0.999, 51.001),
        source_url="https://example.org/title",
    )


def substation(generation_mw=30.0, demand=25.0, distance_km=1.0):
    return Substation(
        name="Test Primary",
        operator="NGED",
        kind="primary",
        coords=HERE,
        distance_km=distance_km,
        headroom=Headroom(generation_mw=generation_mw, demand=demand, basis="test"),
    )


def good_site(**overrides):
    """A flat, dry, undesignated 10 ha English field next to a substation with headroom."""
    parts = {
        "locality": Locality(country="England"),
        "terrain": Terrain(
            source="ea_lidar_1m",
            resolution_m=1,
            cells=100,
            min_m=10,
            max_m=12,
            mean_m=11,
            relief_m=2,
            slope_median_pct=2,
        ),
        "flood": FloodRisk(zone=1, zone_2_pct=0, zone_3_pct=0),
        "land": Land(
            alc=[AlcGrade(grade="Grade 3b", overlap_pct=100, survey="post_1988")], best_and_most_versatile=False
        ),
        "designations": [],
        "grid": Grid(operators=["NGED"], substations=[substation()]),
    } | overrides
    return LocationData(
        coords=HERE,
        title=parts.pop("title", title()),
        deterministic=Deterministic(**parts),
        sources=[SourceStatus(name=n, url=f"https://example.org/{i}", status="ok") for i, n in enumerate(SOURCE_NAMES)],
    )


def propose(battery_mw=20.0, limits=None, **site):
    return Proposal(location=good_site(**site), battery_mw=battery_mw, limits=limits or Limits())


def designation(kind, overlap_pct, *, on_site=True):
    return Designation(
        kind=kind,
        category="ecology",
        name=kind.upper(),
        on_site=on_site,
        overlap_pct=overlap_pct,
        source="natural_england",
    )


def test_a_good_site_passes_every_check_with_evidence():
    report = assess(propose())
    assert report.possible
    assert [c.outcome for c in report.checks] == ["pass"] * len(HARD_CHECKS)
    assert [c.name for c in report.checks] == [check.__name__ for check in HARD_CHECKS]
    assert all(
        c.source_urls
        for c in report.checks
        if c.name not in {"buildable_slope", "avoids_best_farmland", "outside_green_belt"}
    )


@pytest.mark.parametrize(("area_ha", "outcome"), [(1.0, "fail"), (2.0, "warn"), (3.0, "pass")])
def test_enough_area_for_80_mwh(area_ha, outcome):
    # 80 MWh x 0.05-0.075 acres/MWh = 1.62-2.43 ha
    assert hard.enough_area(propose(title=title(area_ha))).outcome == outcome


@pytest.mark.parametrize(("slope", "outcome"), [(2, "pass"), (7, "warn"), (12, "fail")])
def test_buildable_slope(slope, outcome):
    terrain = good_site().deterministic.terrain.model_copy(update={"slope_median_pct": slope})
    assert hard.buildable_slope(propose(terrain=terrain)).outcome == outcome


@pytest.mark.parametrize(
    ("flood", "outcome"),
    [
        (FloodRisk(zone=1, zone_2_pct=0, zone_3_pct=0), "pass"),
        (FloodRisk(zone=2, zone_2_pct=30, zone_3_pct=0), "warn"),
        (FloodRisk(zone=3, zone_2_pct=0, zone_3_pct=10), "warn"),
        (FloodRisk(zone=3, zone_2_pct=0, zone_3_pct=94.3), "fail"),
    ],
)
def test_flood_zone_3_blocks_only_when_it_covers_the_title(flood, outcome):
    assert hard.outside_flood_zone_3(propose(flood=flood)).outcome == outcome


def test_limits_are_tunable():
    flood = FloodRisk(zone=3, zone_2_pct=0, zone_3_pct=10)
    strict = Limits(max_flood_zone_3_pct=5)
    assert hard.outside_flood_zone_3(propose(flood=flood, limits=strict)).outcome == "fail"


def test_green_belt_and_best_farmland_warn_but_do_not_block():
    land = Land(
        alc=[AlcGrade(grade="Grade 2", overlap_pct=100, survey="post_1988")],
        best_and_most_versatile=True,
        green_belt=True,
    )
    report = assess(propose(land=land))
    assert report.possible
    assert len(report.caveats) == 2


def test_unsplit_grade_3_is_unknown():
    land = Land(alc=[AlcGrade(grade="Grade 3", overlap_pct=100, survey="provisional")], best_and_most_versatile=None)
    assert hard.avoids_best_farmland(propose(land=land)).outcome == "unknown"


@pytest.mark.parametrize(
    ("designations", "outcome"),
    [
        ([], "pass"),
        ([designation("sssi", 0.0, on_site=False)], "pass"),  # nearby is not on the title
        ([designation("priority_habitat", 90.0)], "pass"),  # not a blocking kind
        ([designation("sssi", 12.0)], "warn"),
        ([designation("sssi", 12.0), designation("ancient_woodland", 60.0)], "fail"),
        ([designation("sssi", None)], "fail"),  # touches the title, extent unknown: assume the worst
    ],
)
def test_protected_ecology(designations, outcome):
    assert hard.clear_of_protected_ecology(propose(designations=designations)).outcome == outcome


def test_each_protection_family_has_its_own_check():
    park = [designation("national_park", 100.0)]
    assert hard.clear_of_protected_landscape(propose(designations=park)).outcome == "fail"
    assert hard.clear_of_protected_ecology(propose(designations=park)).outcome == "pass"
    monument = [designation("scheduled_monument", 70.0)]
    assert hard.clear_of_protected_heritage(propose(designations=monument)).outcome == "fail"


def test_outside_england_is_unknown_not_clear():
    report = assess(propose(title=None, locality=Locality(country="Scotland"), terrain=None, flood=None, land=None))
    assert report.possible  # nothing failed...
    assert {"title_found", "outside_flood_zone_3", "clear_of_protected_ecology"} <= set(
        report.unknowns
    )  # ...but unproven


def test_no_substation_in_reach_blocks():
    far = Grid(operators=["NGED"], substations=[substation(distance_km=8)])
    report = assess(propose(grid=far))
    assert not report.possible
    assert hard.grid_headroom(propose(grid=far)).outcome == "unknown"


def test_no_grid_data_is_unknown():
    assert hard.substation_within_reach(propose(grid=Grid())).outcome == "unknown"


@pytest.mark.parametrize(
    ("generation_mw", "demand", "outcome"),
    [(30, 25, "pass"), (30, 5, "warn"), (0, 25, "warn")],  # the smaller of import and export binds
)
def test_grid_headroom(generation_mw, demand, outcome):
    grid = Grid(operators=["NGED"], substations=[substation(generation_mw, demand)])
    check = hard.grid_headroom(propose(grid=grid))
    assert check.outcome == outcome
    assert check.facts["headroom_mw"] == min(generation_mw, demand)


def test_zero_headroom_blocks_only_when_asked():
    grid = Grid(operators=["NGED"], substations=[substation(generation_mw=0)])
    assert hard.grid_headroom(propose(grid=grid, limits=Limits(min_headroom_mw=1))).outcome == "fail"


def test_overhead_line_crossing_warns():
    line = OverheadLine(
        operator="NGED",
        voltage_kv=33,
        distance_m=0,
        crosses_site=True,
        geometry=Geometry(type="LineString", coordinates=[[-1, 51], [-0.999, 51.001]]),
    )
    grid = Grid(operators=["NGED"], substations=[substation()], lines=[line])
    assert hard.clear_of_overhead_lines(propose(grid=grid)).outcome == "warn"


def test_report_survives_a_temporal_payload():
    report = assess(propose(flood=FloodRisk(zone=3, zone_2_pct=0, zone_3_pct=95)))
    assert not report.possible
    assert report.blockers == [c.reason for c in report.checks if c.outcome == "fail"]
    assert type(report).model_validate_json(report.model_dump_json()) == report


def test_unknown_from_a_failed_source_names_it_and_links_the_attempt():
    location = good_site(flood=None)
    failed = SourceStatus(
        name="EA: flood zones", url="https://example.org/flood", status="failed", detail="ReadTimeout"
    )
    location.sources = [s for s in location.sources if s.name != "EA: flood zones"] + [failed]
    check = hard.outside_flood_zone_3(Proposal(location=location, battery_mw=20))
    assert check.outcome == "unknown"
    assert "EA: flood zones failed (ReadTimeout)" in check.reason
    assert check.source_urls == ["https://example.org/flood"]
    assert check.confidence == hard.UNKNOWN_CONFIDENCE


def test_site_land_output_carries_blockers_caveats_and_gaps_separately():
    from bessible.possibility.pipeline import site_land_output

    proposal = propose(title=title(area_ha=0.02), flood=None)
    out = site_land_output(proposal, assess(proposal), "run-1234")
    assert out.blockers
    assert "0.02" in out.blockers[0]
    assert out.constraints == out.blockers + out.caveats
    assert any(n.startswith("outside_flood_zone_3:") for n in out.not_assessed)
    assert not any("not assessed" in c.lower() for c in out.constraints)
