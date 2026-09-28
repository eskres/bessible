"""INSPIRE polygons as `TitleParcel`s, and the site they make with a BESS footprint. Pure geometry: no I/O.

Areas and shares are measured on a local metric frame around the pin (`location.geometry.Site`), good to well
under 1% across the few kilometres a site spans.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from shapely.geometry import Point
from shapely.ops import unary_union

from bessible.footprint import M2_PER_ACRE
from bessible.location.geometry import Site, from_geojson, to_geometry
from bessible.location.models import Coordinates
from bessible.models import Geometry, Position, TitleParcel

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from shapely.geometry.base import BaseGeometry

    from bessible.api import planning_data

ENTITY_URL = "https://www.planning.data.gov.uk/entity/{}"
MIN_RADIUS_M = 300.0  # design.md: 1.5 x the radius of a circle of the stated area, at least 300 m, at most 3 km
MAX_RADIUS_M = 3000.0
RADIUS_FACTOR = 1.5
MIN_SHARE_PCT = 0.5  # a polygon under less of the footprint than this is a boundary sliver, not part of the site
MAX_CANDIDATES = 600  # nearest first: keeps the workflow state and the map layer small in towns


def search_radius_m(acres: float | None) -> float:
    """How far around the pin to look for polygons, for a site of `acres` (None: the minimum)."""
    if not acres or acres <= 0:
        return MIN_RADIUS_M
    r = RADIUS_FACTOR * math.sqrt(acres * M2_PER_ACRE / math.pi)
    return min(MAX_RADIUS_M, max(MIN_RADIUS_M, r))


def _frame(origin: Position | Coordinates) -> Site:
    return Site(Coordinates(lat=origin.lat, lon=origin.lon))


def circle_wkt(center: Position, radius_m: float) -> str:
    """WKT polygon (degrees) of a circle around `center`, short enough for a query string."""
    frame = _frame(center)
    return str(frame.to_deg(Point(0, 0).buffer(radius_m, quad_segs=8)).wkt)


def parcels_from_features(
    response: planning_data.EntityGeoJsonResponse, origin: Position, *, limit: int = MAX_CANDIDATES
) -> list[TitleParcel]:
    """Every `title-boundary` feature as a parcel, nearest to `origin` first, at most `limit`, each id once."""
    frame = _frame(origin)
    pin = Point(0, 0)
    found: dict[str, tuple[float, TitleParcel]] = {}
    for feature in response.features:
        props = feature.properties
        if feature.geometry is None or feature.geometry.coordinates is None or props.dataset != "title-boundary":
            continue
        geom = from_geojson(feature.geometry.model_dump())
        if geom.is_empty or geom.area == 0:
            continue
        ref = props.reference or str(props.entity or "")
        if not ref or ref in found:
            continue
        shape_m = frame.to_m(geom)
        parcel = TitleParcel(
            inspire_id=ref,
            geometry=Geometry.model_validate(to_geometry(geom).model_dump()),
            area_m2=round(shape_m.area, 1),
            source_url=ENTITY_URL.format(props.entity) if props.entity else None,
        )
        found[ref] = (shape_m.distance(pin), parcel)
    ranked = sorted(found.values(), key=lambda item: (item[0], item[1].area_m2))
    return [p for _, p in ranked[:limit]]


def shape_of(parcel: TitleParcel) -> BaseGeometry:
    """The parcel's shapely geometry in degrees."""
    return from_geojson(parcel.geometry.model_dump())


def pin_parcel(parcels: Iterable[TitleParcel], pos: Position) -> TitleParcel | None:
    """The polygon under the pin (the smallest, if polygons overlap), or None when the pin is on none."""
    point = Point(pos.lon, pos.lat)
    under = [p for p in parcels if shape_of(p).covers(point)]
    return min(under, key=lambda p: p.area_m2) if under else None


def footprint_shape(footprint_geojson: dict[str, Any] | None) -> BaseGeometry | None:
    """The footprint's polygon (a Feature or a bare geometry), or None."""
    if not footprint_geojson:
        return None
    geom = footprint_geojson.get("geometry", footprint_geojson)
    if not isinstance(geom, dict) or geom.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    return from_geojson(geom)


def with_overlap(parcels: Iterable[TitleParcel], footprint: BaseGeometry | None, origin: Position) -> list[TitleParcel]:
    """Each parcel with `footprint_overlap_pct` set: the share of the footprint's area that lies on it."""
    if footprint is None:
        return [p.model_copy(update={"footprint_overlap_pct": None}) for p in parcels]
    frame = _frame(origin)
    foot_m = frame.to_m(footprint)
    total = foot_m.area
    out = []
    for p in parcels:
        pct = 100 * frame.to_m(shape_of(p)).intersection(foot_m).area / total if total else 0.0
        out.append(p.model_copy(update={"footprint_overlap_pct": round(pct, 2)}))
    return out


def select_site(
    candidates: Sequence[TitleParcel],
    *,
    footprint: BaseGeometry | None,
    origin: Position,
    title_ids: Sequence[str] | None = None,
    pin: TitleParcel | None = None,
) -> list[TitleParcel]:
    """The polygons the confirmed site covers, largest share of the footprint first.

    `title_ids` (the user's clicks) win when given; else every candidate under at least `MIN_SHARE_PCT` of the
    footprint; with neither, the pin polygon.
    """
    measured = with_overlap(candidates, footprint, origin)
    if title_ids is not None:
        wanted = set(title_ids)
        chosen = [p for p in measured if p.inspire_id in wanted]
    elif footprint is not None:
        chosen = [p for p in measured if (p.footprint_overlap_pct or 0) >= MIN_SHARE_PCT]
    else:
        chosen = [p for p in measured if pin is not None and p.inspire_id == pin.inspire_id]
    return sorted(chosen, key=lambda p: (-(p.footprint_overlap_pct or 0), -p.area_m2))


def shape_area_m2(geom: BaseGeometry, origin: Position) -> float:
    """Area of a shape in degrees, in m²."""
    return float(_frame(origin).to_m(geom).area)


def uncovered_pct(footprint: BaseGeometry, parcels: Iterable[TitleParcel], origin: Position) -> float:
    """Share of the footprint on none of `parcels` (a road, a river, unregistered land, or not searched)."""
    frame = _frame(origin)
    foot_m = frame.to_m(footprint)
    if foot_m.area == 0:
        return 0.0
    covered = unary_union([frame.to_m(shape_of(p)) for p in parcels]).intersection(foot_m).area if parcels else 0.0
    return round(max(0.0, 100 * (1 - covered / foot_m.area)), 2)


def outside_search(footprint: BaseGeometry, center: Position, radius_m: float) -> bool:
    """Whether part of the footprint lies outside the circle the candidates came from."""
    frame = _frame(center)
    return not Point(0, 0).buffer(radius_m, quad_segs=16).contains(frame.to_m(footprint))


def union_feature(parcels: Sequence[TitleParcel], origin: Position) -> tuple[dict[str, Any], float]:
    """The parcels' union as a GeoJSON Feature (properties: the INSPIRE ids), and its area in m²."""
    if not parcels:
        return {}, 0.0
    merged = unary_union([shape_of(p) for p in parcels])
    area = _frame(origin).to_m(merged).area
    feature = {
        "type": "Feature",
        "geometry": to_geometry(merged).model_dump(),
        "properties": {"inspire_ids": [p.inspire_id for p in parcels], "area_m2": round(area, 1)},
    }
    return feature, round(area, 1)


def feature_collection(parcels: Iterable[TitleParcel]) -> dict[str, Any]:
    """Parcels as a GeoJSON FeatureCollection (for the map and the run folder)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": p.geometry.model_dump(),
                "properties": p.model_dump(exclude={"geometry"}),
            }
            for p in parcels
        ],
    }
