"""Wire models (`bessible.api`) -> tidy models (`.models`). Pure functions: no I/O, so they test against fixtures.

Every function takes already-parsed API responses plus the `Site` to measure against.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import numpy as np
from shapely import wkt
from shapely.errors import ShapelyError
from shapely.geometry import LineString
from shapely.ops import unary_union

from bessible.api import natural_england, nged, npg, planning_data, sp_energy, ssen, ssen_distribution

from .geometry import Site, from_geojson, to_geometry
from .models import (
    AlcGrade,
    Coordinates,
    Designation,
    DesignationCategory,
    ElevationSample,
    FloodRisk,
    FloodZone,
    GridProject,
    Headroom,
    Land,
    Locality,
    Operator,
    OverheadLine,
    Rag,
    SourceDocument,
    Substation,
    Terrain,
    TitleBoundary,
    TransmissionProject,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from shapely.geometry.base import BaseGeometry

    from bessible.api import ea_flood, ea_lidar, neso, nominatim, postcodes_io, ukpn

ENTITY_URL = "https://www.planning.data.gov.uk/entity/{}"
LISTING_URL = "https://historicengland.org.uk/listing/the-list/list-entry/{}"
SSSI_URL = "https://designatedsites.naturalengland.org.uk/SiteDetail.aspx?SiteCode=S{}"
MIN_PROJECT_MW = 1.0  # the registers' own floor at UKPN / SSEN; applied to NGED too
VOLTS_THRESHOLD = 1000  # operating voltage above this is in volts, not kV
SAME_SUBSTATION_M = 150  # UKPN's two substation datasets share no id: rows this close are the same site
SLOPE_LIMIT_PCT = 5.0
SAMPLE_GRID = 10  # elevation labels: SAMPLE_GRID x SAMPLE_GRID over the title's box, inside points only
DOCUMENT_RANGE_M = 100  # designations this close get their citation page queued for the agents
# How far from the title a designation still matters (default DEFAULT_RANGE_M). Beyond it, it is dropped.
DEFAULT_RANGE_M = 2000
RANGE_M = {
    "priority_habitat": 100,
    "ancient_woodland": 500,
    "local_nature_reserve": 500,
    "listed_building": 500,
    "tree_preservation_zone": 100,
    "article_4_direction": 0,
    "heritage_at_risk": 500,
    "conservation_area": 1000,
    "scheduled_monument": 1000,
    "registered_park_or_garden": 1000,
    "registered_battlefield": 1000,
}
BMV_GRADES = frozenset({"Grade 1", "Grade 2", "Grade 3a"})


# ------------------------------------------- the site ------------------------------------------- #


def title_boundary(
    response: planning_data.EntityGeoJsonResponse, coords: Coordinates
) -> tuple[TitleBoundary, BaseGeometry] | None:
    """The title containing the point (the smallest, if titles overlap) and its shapely polygon in degrees."""
    found: list[tuple[float, str, int | None, BaseGeometry]] = []
    for feature in response.features:
        if feature.geometry is None or feature.geometry.coordinates is None:
            continue
        if feature.properties.dataset != "title-boundary":
            continue
        geom = from_geojson(feature.geometry.model_dump())
        props = feature.properties
        found.append((Site(coords, geom).shape_m.area, props.reference or "", props.entity, geom))
    if not found:
        return None
    area_m2, reference, entity, geom = min(found, key=itemgetter(0))
    min_lon, min_lat, max_lon, max_lat = geom.bounds
    title = TitleBoundary(
        inspire_id=reference,
        geometry=to_geometry(geom),
        area_m2=round(area_m2, 1),
        area_ha=round(area_m2 / 10_000, 3),
        perimeter_m=round(Site(coords, geom).shape_m.length, 1),
        centroid=Coordinates(lat=round(geom.centroid.y, 6), lon=round(geom.centroid.x, 6)),
        bbox=(min_lon, min_lat, max_lon, max_lat),
        source_url=ENTITY_URL.format(entity) if entity else planning_data.EntitySearchRequest.GEOJSON_URL,
    )
    return title, geom


def locality(
    postcode: postcodes_io.PostcodeResult | None,
    address: nominatim.ReverseResponse | None,
    on_site: Sequence[planning_data.EntityBase],
) -> Locality:
    """Administrative geography from postcodes.io, Nominatim and the planning.data areas the site sits in."""
    by_dataset = {e.dataset: e for e in reversed(on_site)}
    lpa = by_dataset.get("local-planning-authority")
    a = address.address if address else None
    return Locality(
        address=address.display_name if address else None,
        place=next((p for p in (a.hamlet, a.village, a.town, a.city, a.suburb) if p), None) if a else None,
        postcode=postcode.postcode if postcode else (a.postcode if a else None),
        postcode_distance_m=round(postcode.distance, 1) if postcode and postcode.distance is not None else None,
        country=postcode.country if postcode else None,
        region=postcode.region if postcode else _name(by_dataset.get("region")),
        county=(postcode.admin_county if postcode else None) or (a.county if a else None),
        district=postcode.admin_district if postcode else _name(by_dataset.get("local-authority-district")),
        parish=(postcode.parish if postcode else None) or _name(by_dataset.get("parish")),
        ward=(postcode.admin_ward if postcode else None) or _name(by_dataset.get("ward")),
        constituency=(postcode.parliamentary_constituency_2024 or postcode.parliamentary_constituency)
        if postcode
        else None,
        planning_authority=(_name(lpa) or "").removesuffix(" LPA") or None,
        planning_authority_code=lpa.reference if lpa else None,
        rural_urban=postcode.ruc21 if postcode else None,
        built_up_area=_name(by_dataset.get("built-up-area")),
    )


def _name(entity: planning_data.EntityBase | None) -> str | None:
    return entity.name if entity else None


# -------------------------------------------- terrain ------------------------------------------- #


def sample_points(site: Site) -> tuple[np.ndarray, np.ndarray]:
    """(lons, lats) of the sample grid's points that fall inside the title."""
    w, s, e, n = site.bbox
    steps = (np.arange(SAMPLE_GRID) + 0.5) / SAMPLE_GRID
    lons, lats = np.meshgrid(w + (e - w) * steps, s + (n - s) * steps)
    inside = site.contains(lons.ravel(), lats.ravel())
    return lons.ravel()[inside], lats.ravel()[inside]


def terrain_from_lidar(dtm: ea_lidar.DtmGrid, site: Site) -> Terrain | None:
    """Height and slope over every LIDAR cell inside the title (None when the raster has no data there)."""
    z = np.array([[np.nan if v is None else v for v in row] for row in dtm.rows], dtype=float)
    cols, rows = np.meshgrid(np.arange(dtm.width), np.arange(dtm.height))
    lons = dtm.west + (cols + 0.5) * dtm.pixel_lon
    lats = dtm.north - (rows + 0.5) * dtm.pixel_lat
    inside = site.contains(lons.ravel(), lats.ravel()).reshape(z.shape) & ~np.isnan(z)
    if not inside.any():
        return None
    dx, dy = dtm.pixel_m
    slope = np.full(z.shape, np.nan)  # central differences, as DtmGrid.slope_percent; edges stay unknown
    slope[1:-1, 1:-1] = 100 * np.hypot((z[1:-1, 2:] - z[1:-1, :-2]) / (2 * dx), (z[:-2, 1:-1] - z[2:, 1:-1]) / (2 * dy))
    heights, slopes = z[inside], slope[inside & ~np.isnan(slope)]
    s_lons, s_lats = sample_points(site)
    samples = [
        ElevationSample(lat=round(float(la), 6), lon=round(float(lo), 6), elevation_m=m)
        for lo, la in zip(s_lons, s_lats, strict=True)
        if (m := dtm.at(float(la), float(lo))) is not None
    ]
    return Terrain(
        source="ea_lidar_1m",
        resolution_m=round(max(dx, dy), 2),
        cells=int(inside.sum()),
        **_height_stats(heights),
        slope_median_pct=round(float(np.median(slopes)), 1) if slopes.size else None,
        slope_p90_pct=round(float(np.percentile(slopes, 90)), 1) if slopes.size else None,
        slope_max_pct=round(float(slopes.max()), 1) if slopes.size else None,
        share_over_5pct=round(float((slopes > SLOPE_LIMIT_PCT).mean()), 2) if slopes.size else None,
        samples=samples,
    )


def terrain_from_points(lons: Sequence[float], lats: Sequence[float], elevations: Sequence[float]) -> Terrain | None:
    """The 90 m fallback: heights only, from the sample grid."""
    if not elevations:
        return None
    samples = [
        ElevationSample(lat=round(la, 6), lon=round(lo, 6), elevation_m=m)
        for lo, la, m in zip(lons, lats, elevations, strict=True)
    ]
    return Terrain(
        source="open_meteo_90m",
        resolution_m=90,
        cells=len(samples),
        **_height_stats(np.array(elevations, dtype=float)),
        samples=samples,
    )


def _height_stats(heights: np.ndarray) -> dict[str, float]:
    lo, hi = float(heights.min()), float(heights.max())
    return {
        "min_m": round(lo, 2),
        "max_m": round(hi, 2),
        "mean_m": round(float(heights.mean()), 2),
        "relief_m": round(hi - lo, 2),
    }


# --------------------------------------------- flood -------------------------------------------- #


def flood_risk(
    response: ea_flood.FloodZoneResponse, site: Site, on_site: Sequence[planning_data.EntityBase]
) -> FloodRisk:
    """Flood zones measured against the title. Call only for sites in England (the map's coverage)."""
    zones: list[FloodZone] = []
    shapes: dict[int, list[BaseGeometry]] = {2: [], 3: []}
    for feature in response.features:
        if feature.geometry is None or feature.properties.flood_zone is None:
            continue
        geom = from_geojson(feature.geometry)
        level: Literal[2, 3] = 3 if feature.properties.flood_zone == "FZ3" else 2
        hit, overlap, distance = site.measure(geom)
        shapes[level].append(geom)
        zones.append(
            FloodZone(
                zone=level,
                flood_source=feature.properties.flood_source,
                origin=feature.properties.origin,
                overlap_pct=overlap if overlap is not None else (100.0 if hit else 0.0),
                distance_m=distance,
            )
        )
    touching = [z.zone for z in zones if z.distance_m == 0]
    return FloodRisk(
        zone=max(touching, default=1),
        zone_2_pct=_union_pct(shapes[2], site),
        zone_3_pct=_union_pct(shapes[3], site),
        flood_storage_area=any(e.dataset == "flood-storage-area" for e in on_site),
        zones=sorted(zones, key=lambda z: (z.distance_m, -z.zone)),
    )


def _union_pct(shapes: list[BaseGeometry], site: Site) -> float:
    if not shapes:
        return 0.0
    hit, overlap, _ = site.measure(unary_union(shapes))
    return overlap if overlap is not None else (100.0 if hit else 0.0)


# --------------------------------------------- land --------------------------------------------- #


def land(
    provisional: natural_england.ArcGisGeoJsonResponse[Any] | None,
    post_1988: natural_england.ArcGisGeoJsonResponse[Any] | None,
    site: Site,
    on_site: Sequence[planning_data.EntityBase],
) -> tuple[Land, list[SourceDocument]]:
    """Agricultural land grade(s) of the title, green belt and brownfield; plus the survey reports to read."""
    grades: list[AlcGrade] = []
    documents: list[SourceDocument] = []
    surveys: tuple[tuple[Literal["provisional", "post_1988"], Any], ...] = (
        ("provisional", provisional),
        ("post_1988", post_1988),
    )
    surveyed = unary_union([
        from_geojson(f.geometry.model_dump()) for f in (post_1988.features if post_1988 else []) if f.geometry
    ])
    for survey, response in surveys:
        shares: dict[str, list[BaseGeometry]] = {}
        for feature in response.features if response else []:
            if feature.geometry is None or not feature.properties.alc_grade:
                continue
            geom = from_geojson(feature.geometry.model_dump())
            if survey == "provisional":
                geom = geom.difference(surveyed)  # a detailed survey supersedes the 1:250k map where there is one
            if not geom.is_empty and site.measure(geom)[0]:
                shares.setdefault(feature.properties.alc_grade, []).append(geom)
                url = getattr(feature.properties, "published", None)
                if url and url.startswith("http") and all(d.url != url for d in documents):
                    documents.append(
                        SourceDocument(
                            kind="alc_survey_report",
                            title=f"Agricultural land survey {feature.properties.rpt_jobnum or ''}".strip(),
                            url=url,
                            relates_to="post-1988 agricultural land classification survey",
                        )
                    )
        grades += [AlcGrade(grade=g, overlap_pct=_union_pct(s, site), survey=survey) for g, s in shares.items()]
    grades = [g for g in grades if g.overlap_pct > 0] or grades[:1]
    grades.sort(key=lambda g: -g.overlap_pct)
    unsplit = any(g.grade == "Grade 3" for g in grades)  # only the unsurveyed remainder is still provisional
    bmv: bool | None = True if any(g.grade in BMV_GRADES for g in grades) else None if unsplit or not grades else False
    belt = next((e for e in on_site if e.dataset == "green-belt"), None)
    return (
        Land(
            alc=grades,
            best_and_most_versatile=bmv,
            green_belt=belt is not None,
            green_belt_name=belt.name if belt else None,
            brownfield=any(e.dataset == "brownfield-land" for e in on_site),
        ),
        documents,
    )


# ----------------------------------------- designations ----------------------------------------- #

# Natural England layer -> (kind, category, properties attribute holding the name, attribute holding a detail)
NE_LAYERS: dict[str, tuple[str, DesignationCategory, str | None, str | None]] = {
    "sssi": ("sssi", "ecology", "name", None),
    "sssi_irz": ("sssi_impact_risk_zone", "ecology", None, None),
    "sac": ("sac", "ecology", "sac_name", "status"),
    "spa": ("spa", "ecology", "spa_name", "status"),
    "ramsar": ("ramsar", "ecology", "name", "status"),
    "nnr": ("national_nature_reserve", "ecology", "name", None),
    "lnr": ("local_nature_reserve", "ecology", "name", None),
    "ancient_woodland": ("ancient_woodland", "ecology", "name", "status"),
    "ancient_woodland_revised": ("ancient_woodland", "ecology", "name", "status"),
    "priority_habitats": ("priority_habitat", "ecology", "main_habs", None),
    "aonb": ("national_landscape", "landscape", "name", None),  # AONBs were renamed National Landscapes
    "national_parks": ("national_park", "landscape", "name", None),
}
ON_SITE_ONLY_LAYERS = frozenset({"sssi_irz"})  # zones tile the country: only the one the site is in matters

# planning.data dataset -> (kind, category, attribute holding a detail). Datasets Natural England / the EA
# serve with geometry (SSSI, flood zones, ALC...) are taken from there instead.
PLANNING_DATASETS: dict[str, tuple[str, DesignationCategory, str | None]] = {
    "green-belt": ("green_belt", "planning", None),
    "article-4-direction-area": ("article_4_direction", "planning", "permitted_development_rights"),
    "tree-preservation-zone": ("tree_preservation_zone", "planning", "tree_preservation_zone_type"),
    "central-activities-zone": ("central_activities_zone", "planning", None),
    "conservation-area": ("conservation_area", "heritage", None),
    "listed-building": ("listed_building", "heritage", "listed_building_grade"),
    "scheduled-monument": ("scheduled_monument", "heritage", None),
    "park-and-garden": ("registered_park_or_garden", "heritage", "park_and_garden_grade"),
    "battlefield": ("registered_battlefield", "heritage", None),
    "world-heritage-site": ("world_heritage_site", "heritage", None),
    "world-heritage-site-buffer-zone": ("world_heritage_site_buffer_zone", "heritage", None),
    "heritage-at-risk": ("heritage_at_risk", "heritage", None),
    "archaeological-priority-area": ("archaeological_priority_area", "heritage", "archaeological_risk_tier"),
    "heritage-coast": ("heritage_coast", "landscape", None),
}


def natural_england_designations(
    layers: dict[str, natural_england.ArcGisGeoJsonResponse[Any]], site: Site
) -> tuple[list[Designation], list[SourceDocument]]:
    """Designations from the Natural England layers, measured against the site."""
    out: list[Designation] = []
    documents: list[SourceDocument] = []
    revised = [
        from_geojson(f.geometry.model_dump())
        for f in (layers["ancient_woodland_revised"].features if "ancient_woodland_revised" in layers else [])
        if f.geometry
    ]
    for layer, response in layers.items():
        if layer not in NE_LAYERS:
            continue
        kind, category, name_attr, detail_attr = NE_LAYERS[layer]
        for feature in response.features:
            if feature.geometry is None:
                continue
            geom = from_geojson(feature.geometry.model_dump())
            on_site, overlap, distance = site.measure(geom)
            if (layer in ON_SITE_ONLY_LAYERS and not on_site) or distance > RANGE_M.get(kind, DEFAULT_RANGE_M):
                continue
            if layer == "ancient_woodland" and any(geom.intersects(r) for r in revised):
                continue  # the same wood, re-surveyed in the revised inventory
            p = feature.properties
            name = (getattr(p, name_attr) or "").strip() or None if name_attr else None
            url = _ne_url(layer, p)
            out.append(
                Designation(
                    kind=kind,
                    category=category,
                    name=name,
                    reference=getattr(p, "ref_code", None) or (getattr(p, "code", None) and str(p.code)),
                    detail=getattr(p, detail_attr) if detail_attr else None,
                    on_site=on_site,
                    overlap_pct=overlap,
                    distance_m=distance,
                    source="natural_england",
                    source_url=url,
                    geometry=to_geometry(geom),
                )
            )
            if url and distance <= DOCUMENT_RANGE_M:
                is_irz = layer == "sssi_irz"
                documents.append(
                    SourceDocument(
                        kind="sssi_impact_risk_zone" if is_irz else "designation",
                        title="SSSI impact risk zone: when Natural England must be consulted"
                        if is_irz
                        else name or kind,
                        url=url,
                        relates_to=name,
                    )
                )
    return out, documents


def _ne_url(layer: str, props: Any) -> str | None:  # ruff: ignore[any-type] - one props model per layer
    if layer == "sssi_irz":
        return quote(props.irzurl, safe=":/?&=,()%") if props.irzurl else None
    if layer == "sssi":
        return SSSI_URL.format(props.hyperlink) if props.hyperlink else None
    link = getattr(props, "hotlink", None)
    return link if isinstance(link, str) and link.startswith("http") else None


def planning_designations(
    on_site: Sequence[planning_data.EntityBase], nearby: Sequence[planning_data.EntityBase], site: Site
) -> tuple[list[Designation], list[SourceDocument], list[str]]:
    """Designations from planning.data, plus their documents and free-text notes.

    `on_site` entities were fetched without geometry (they are large: green belt, heritage coast...), so they are
    only known to touch the title. `nearby` entities carry geometry and are measured.
    """
    out: list[Designation] = []
    documents: list[SourceDocument] = []
    notes: list[str] = []
    seen: set[int | None] = set()
    for entity, measured in [(e, True) for e in nearby] + [(e, False) for e in on_site]:
        if entity.entity in seen or entity.end_date is not None:  # an end date marks a historical entity
            continue
        geom = _entity_geometry(entity) if measured else None
        if measured and geom is None:
            continue  # nothing to measure; the on-site pass still catches it if it touches the title
        seen.add(entity.entity)
        hit, overlap, distance = site.measure(geom) if geom is not None else (True, None, None)
        near = hit or (distance is not None and distance <= DOCUMENT_RANGE_M)
        in_range = entity.dataset in PLANNING_DATASETS and (
            hit or (distance or 0) <= RANGE_M.get(PLANNING_DATASETS[entity.dataset][0], DEFAULT_RANGE_M)
        )
        if entity.dataset in PLANNING_DATASETS and in_range:
            kind, category, detail_attr = PLANNING_DATASETS[entity.dataset]
            detail = getattr(entity, detail_attr, None) if detail_attr else None
            out.append(
                Designation(
                    kind=kind,
                    category=category,
                    name=entity.name,
                    reference=entity.reference,
                    detail=f"Grade {detail}" if detail and "grade" in (detail_attr or "") else detail,
                    on_site=hit,
                    overlap_pct=overlap,
                    distance_m=0.0 if hit else distance,
                    source="planning_data",
                    source_url=ENTITY_URL.format(entity.entity),
                    geometry=to_geometry(geom) if geom is not None else None,
                )
            )
        if near:
            documents += _entity_documents(entity)
            if hit and (text := entity.notes or entity.description):
                notes.append(f"{entity.dataset} {entity.name or entity.reference or ''}: {text}".strip())
    return out, documents, notes


def _entity_geometry(entity: planning_data.EntityBase) -> BaseGeometry | None:
    text = entity.geometry or entity.point
    if not text:
        return None
    try:
        return wkt.loads(text)
    except ShapelyError:
        return None


DOCUMENT_KINDS: dict[str, Literal["article_4_direction", "brownfield_site_plan", "planning_application"]] = {
    "article-4-direction-area": "article_4_direction",
    "brownfield-land": "brownfield_site_plan",
    "planning-application": "planning_application",
}


def _entity_documents(entity: planning_data.EntityBase) -> list[SourceDocument]:
    """What an agent should read about one planning.data entity on or next to the site."""
    title = entity.name or f"{entity.dataset} {entity.reference or entity.entity}"
    note = entity.notes or entity.description
    urls: list[str] = []
    if entity.dataset == "listed-building" and entity.reference:
        urls.append(LISTING_URL.format(entity.reference))
    urls += [u for u in (entity.document_url, entity.documentation_url, getattr(entity, "site_plan_url", None)) if u]
    if entity.dataset == "infrastructure-project":
        kind: Any = "infrastructure_project"
        urls = urls or [ENTITY_URL.format(entity.entity)]
    else:
        kind = DOCUMENT_KINDS.get(entity.dataset or "", "designation")
    if entity.dataset == "planning-application" and not urls:
        urls = [ENTITY_URL.format(entity.entity)]
    return [
        SourceDocument(kind=kind, title=title, url=u, relates_to=entity.dataset, note=note)
        for u in dict.fromkeys(urls)
        if u.startswith("http")
    ]


def local_plan_documents(plans: Iterable[planning_data.EntityBase], lpa_code: str | None) -> list[SourceDocument]:
    """The local plans of the site's planning authority (the register has no server-side filter for this)."""
    out: list[SourceDocument] = []
    for plan in plans:
        codes = (getattr(plan, "local_planning_authorities", None) or "").split(";")
        if not lpa_code or lpa_code not in codes or plan.end_date is not None:
            continue
        process, adopted = getattr(plan, "local_plan_process", None), getattr(plan, "adopted_date", None)
        facts = [f"status: {process}" if process else "", f"adopted {adopted}" if adopted else "", plan.notes or ""]
        out += [
            SourceDocument(
                kind="local_plan",
                title=plan.name or plan.reference or "Local plan",
                url=u,
                relates_to=lpa_code,
                note="; ".join(f for f in facts if f) or None,
            )
            for u in dict.fromkeys(u for u in (plan.document_url, plan.documentation_url) if u)
        ]
    return out


# --------------------------------------------- grid --------------------------------------------- #

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_TIA = re.compile(r"threshold.*?=\s*(\d+(?:\.\d+)?)\s*MW", re.IGNORECASE)
_DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S")


def _at(lat: float, lon: float) -> Coordinates:
    return Coordinates(lat=round(lat, 6), lon=round(lon, 6))


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(value) if isinstance(value, str) else None
    return float(match.group()) if match else None


def _floats(value: str | None) -> list[float]:
    return [float(m) for m in _NUMBER.findall(value or "")]


_NAME_KV = re.compile(r"(\d+(?:\.\d+)?)(?=\s*(?:/|kv))", re.IGNORECASE)


def _connection_kv(published: float | None, voltages: Iterable[float], name: str | None = None) -> float | None:
    """A new connection's voltage: published, else the lowest published or named voltage ("Hockley 132/11kV" -> 11).

    The highest voltage on a site is its upstream side, not the busbar a new connection joins.
    """
    if published:
        return published
    found = [v for v in voltages if v] or [float(v) for v in _NAME_KV.findall(name or "")]
    return min(found, default=None)


def _rag(value: str | None) -> Rag | None:
    v = (value or "").strip().lower()
    return v if v in {"red", "amber", "green"} else None  # type: ignore[return-value]


def _yes(value: str | None) -> bool | None:
    v = (value or "").strip().lower()
    return True if v in {"yes", "y"} else False if v in {"no", "n"} else None


def _date(value: date | datetime | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) or value is None:
        return value
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()  # ruff: ignore[call-datetime-strptime-without-zone]
        except ValueError:
            continue
    return None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def ukpn_substations(
    sites: Sequence[ukpn.GridPrimarySite], heatmap: Sequence[ukpn.CapacityHeatmapSite], site: Site
) -> list[Substation]:
    """UKPN's substation list and capacity heatmap merged into one record per substation."""
    out: list[Substation] = []
    unmatched = [s for s in sites if s.spatial_coordinates]
    for h in heatmap:
        if h.geo_point_2d is None:
            continue
        lat, lon = h.geo_point_2d.lat, h.geo_point_2d.lon
        here = Site(Coordinates(lat=lat, lon=lon))
        twin = next(
            (
                s
                for s in unmatched
                if s.spatial_coordinates
                and here.distance_km(s.spatial_coordinates.lat, s.spatial_coordinates.lon) * 1000 <= SAME_SUBSTATION_M
            ),
            None,
        )
        if twin:
            unmatched.remove(twin)
        tia = _TIA.search(h.description or "")
        out.append(
            Substation(
                name=(h.name or (twin.sitename if twin else None) or "Unnamed").strip(),
                operator="UKPN",
                kind=(h.type or "primary").lower(),
                voltage_kv=float(twin.sitevoltage) if twin and twin.sitevoltage else h.voltages,
                voltages=f"{h.voltages:g}" if h.voltages else None,
                connection_voltage_kv=_connection_kv(h.voltage, [h.voltages or 0], h.name),
                coords=_at(lat, lon),
                distance_km=site.distance_km(lat, lon),
                bsp=_dash(h.bsp),
                gsp=_dash(h.gsp),
                headroom=Headroom(
                    generation_mw=h.generationavailablecapacity,
                    generation_rag=_rag(h.generationconstraint),
                    generation_constraint=_dash(h.generationconstraintlimitingfactor),
                    demand=h.demandavailablecapacity,
                    demand_rag=_rag(h.demandconstraint),
                    demand_constraint=_dash(h.demandconstraintlimitingfactor),
                    basis="UKPN capacity heatmap (LTDS): published available capacity; connection offers are "
                    "reported separately",
                    demand_firm_mw=h.demandfirmcapacity,
                    demand_max_mw=h.demandmaximum,
                    demand_min_mw=h.demandminimum,
                    generation_firm_mw=h.generationfirmcapacity,
                    reverse_power_available_mw=h.reversepowerflowavailablecapacity,
                    generation_offers_accepted_mw=h.generationconnectionoffersacceptedcapacity,
                    generation_offers_made_mw=h.generationconnectionoffersmadecapacity,
                    generation_budget_estimates_mw=h.generationbudgetestimatesprovidedcapacity,
                    demand_offers_accepted_mw=h.loadconnectionoffersacceptedcapacity,
                    demand_offers_made_mw=h.loadconnectionoffersmadecapacity,
                    demand_budget_estimates_mw=h.loadbudgetestimatesprovidedcapacity,
                ),
                tia_threshold_mw=float(tia.group(1)) if tia else None,
                **(_ukpn_site_details(twin) if twin else {}),
            )
        )
    for s in unmatched:
        p = s.spatial_coordinates
        if p is None:
            continue
        out.append(
            Substation(
                name=(s.sitename or "Unnamed").strip(),
                operator="UKPN",
                kind="grid" if "grid" in (s.sitetype or "").lower() else "primary",
                voltage_kv=float(s.sitevoltage) if s.sitevoltage else None,
                connection_voltage_kv=_connection_kv(None, [], s.sitename),
                coords=_at(p.lat, p.lon),
                distance_km=site.distance_km(p.lat, p.lon),
                **_ukpn_site_details(s),
            )
        )
    return out


def _dash(value: str | None) -> str | None:
    """UKPN writes "not applicable" as "-"."""
    v = (value or "").strip()
    return None if v in {"", "-"} else v


def _ukpn_site_details(s: ukpn.GridPrimarySite) -> dict[str, Any]:
    earthing = (s.siteclassification or "").strip().upper()
    return {
        "transformer_ratings_summer_mva": _floats(s.transratingsummer),
        "transformer_ratings_winter_mva": _floats(s.transratingwinter),
        "max_demand_summer_mva": s.maxdemandsummer,
        "max_demand_winter_mva": s.maxdemandwinter,
        "earthing": earthing if earthing in {"HOT", "COLD"} else None,
    }


def nged_substations(records: Sequence[nged.CapacityMapSite], site: Site) -> list[Substation]:
    """NGED primaries and bulk supply points with their headroom."""
    out: list[Substation] = []
    for r in records:
        if r.latitude is None or r.longitude is None:
            continue
        out.append(
            Substation(
                name=(r.name or "Unnamed").strip(),
                operator="NGED",
                kind=(r.type or "primary").lower(),
                voltage_kv=max(_floats(r.voltages), default=None),
                voltages=_text(r.voltages),
                connection_voltage_kv=_connection_kv(None, _floats(r.voltages), r.name),
                coords=_at(r.latitude, r.longitude),
                distance_km=site.distance_km(r.latitude, r.longitude),
                bsp=_text(r.bsp),
                gsp=nged.clean_site_name(r.gsp) if r.gsp else None,
                headroom=Headroom(
                    generation_mw=r.generation_contracted_headroom_mw,
                    generation_rag=_rag(r.generation_contracted_rag),
                    generation_constraint=_text(r.generation_constraint_limiting_factor),
                    demand=r.demand_contracted_headroom_mw,
                    demand_rag=_rag(r.demand_contracted_rag),
                    demand_constraint=_text(r.demand_constraint_limiting_factor),
                    basis="NGED network capacity map: headroom after contracted (accepted, not yet connected) schemes",
                    demand_max_mw=_float(r.demand_maximum),
                    demand_min_mw=_float(r.demand_minimum),
                    reverse_power_available_mw=_float(r.reverse_power_flow_available_capacity),
                ),
            )
        )
    return out


def ssen_distribution_substations(
    records: Sequence[ssen_distribution.HeadroomSite], site: Site
) -> tuple[list[Substation], list[str]]:
    """SSEN Distribution substations with their headroom, plus the free-text comments for the agents."""
    out: list[Substation] = []
    notes: list[str] = []
    for r in records:
        if r.lat is None or r.lon is None:
            continue
        works = [w for w in (r.substation_reinforcement_works, r.upstream_reinforcement_works) if w]
        due = r.substation_reinforcement_completion_date or r.upstream_reinforcement_completion_date
        name = (r.substation or "Unnamed").strip()
        out.append(
            Substation(
                name=name,
                operator="SSEN Distribution",
                kind=(r.substation_type or "primary").lower(),
                voltage_kv=max(_floats(r.voltage_kv), default=None),
                voltages=r.voltage_kv,
                connection_voltage_kv=_connection_kv(None, _floats(r.voltage_kv), name),
                coords=_at(r.lat, r.lon),
                distance_km=site.distance_km(r.lat, r.lon),
                bsp=ssen_distribution.clean_site_name(r.upstream_bsp) if r.upstream_bsp else None,
                gsp=ssen_distribution.clean_site_name(r.upstream_gsp) if r.upstream_gsp else None,
                headroom=Headroom(
                    generation_mw=r.estimated_generation_headroom_mw,
                    generation_rag=_rag(r.substation_generation_rag_status),
                    generation_constraint=r.generation_constraint,
                    demand=r.estimated_demand_headroom_mva,
                    demand_unit="MVA",
                    demand_rag=_rag(r.substation_demand_rag_status),
                    demand_constraint=r.demand_constraint,
                    basis="SSEN headroom dashboard: estimated headroom after connected and contracted schemes",
                    connected_generation_mw=r.connected_generation_mw,
                    contracted_generation_mw=r.contracted_generation_mw,
                    contracted_battery_demand_mva=r.contracted_bess_demand_mva,
                ),
                tia_threshold_mw=_float(r.tia_threshold),
                technical_limits_agreed=_yes(r.technical_limits_agreed_at_gsp),
                reinforcement="; ".join(works) or None,
                reinforcement_due=due,
            )
        )
        if r.substation_comment:
            notes.append(f"SSEN on {name}: {r.substation_comment}")
    return out, notes


def ssen_transmission_substations(records: Sequence[ssen.SubstationSite], site: Site) -> list[Substation]:
    """SSEN Transmission substations (north of Scotland). No headroom is published for these."""
    return [
        Substation(
            name=(r.name or "Unnamed").removesuffix(" SUBSTATION").title(),
            operator="SSEN Transmission",
            kind="transmission",
            voltage_kv=r.voltage / 1000 if r.voltage else None,
            coords=_at(p.lat, p.lon),
            distance_km=site.distance_km(p.lat, p.lon),
        )
        for r in records
        if (p := r.geo_point_2d)
    ]


def sp_energy_substations(
    records: Sequence[sp_energy.CapacityHeatmapSite],
    site: Site,
    point_assets: Sequence[sp_energy.GisPointAsset] = (),
) -> list[Substation]:
    """SP Energy Networks (SPD and SPM) substations with their headroom, plus point assets."""
    out: list[Substation] = []
    seen_names: set[str] = set()
    for r in records:
        lat = r.effective_lat
        lon = r.effective_lon
        if lat is None or lon is None:
            continue
        v_kv = r.effective_voltage_kv
        if v_kv is None and r.voltage:
            v_kv = max(_floats(r.voltage), default=None)

        name = r.effective_name
        seen_names.add(name.lower())
        bsp = _text(r.bsp)
        gsp = sp_energy.clean_site_name(r.effective_gsp) if r.effective_gsp else None

        gen_avail = _float(r.generationavailablecapacity)
        dem_avail = _float(r.demandavailablecapacity)

        out.append(
            Substation(
                name=name,
                operator="SP Energy Networks",
                kind=(r.type or "primary").lower(),
                voltage_kv=v_kv,
                voltages=_text(r.voltage),
                connection_voltage_kv=_connection_kv(r.effective_voltage_kv, _floats(r.voltage), name),
                coords=_at(lat, lon),
                distance_km=site.distance_km(lat, lon),
                bsp=bsp,
                gsp=gsp,
                headroom=Headroom(
                    generation_mw=gen_avail,
                    generation_rag=_rag(r.generationconstraint),
                    generation_constraint=_text(r.generationconstraintlimitingfactor),
                    demand=dem_avail,
                    demand_rag=_rag(r.demandconstraint),
                    demand_constraint=_text(r.demandconstraintlimitingfactor),
                    basis="SP Energy Networks capacity heatmap: available headroom",
                    demand_firm_mw=_float(r.demandfirmcapacity),
                    demand_max_mw=_float(r.demandmaximum),
                    demand_min_mw=_float(r.demandminimum),
                    generation_firm_mw=_float(r.generationfirmcapacity),
                    reverse_power_available_mw=_float(r.reversepowerflowavailablecapacity),
                    generation_offers_accepted_mw=_float(r.generationconnectionofferacceptedcapacity),
                    generation_offers_made_mw=_float(r.generationconnectionoffermadecapacity),
                    generation_budget_estimates_mw=_float(r.generationbudgetestimatesprovidedcapacity),
                    demand_offers_accepted_mw=_float(r.loadconnectionoffersacceptedcapacity),
                    demand_offers_made_mw=_float(r.loadconnectionoffersmadecapacity),
                    demand_budget_estimates_mw=_float(r.loadbudgetestimatesprovidedcapacity),
                ),
            )
        )

    for pt in point_assets:
        p = pt.geo_point_2d
        if p is None or not pt.sub_name:
            continue
        clean_name = pt.sub_name.strip()
        if clean_name.lower() in seen_names:
            continue
        seen_names.add(clean_name.lower())
        v_kv = _float(pt.voltage)
        if v_kv and v_kv > VOLTS_THRESHOLD:
            v_kv /= 1000
        out.append(
            Substation(
                name=clean_name,
                operator="SP Energy Networks",
                kind=(pt.asset_type or "substation").lower(),
                voltage_kv=v_kv,
                voltages=str(pt.voltage) if pt.voltage else None,
                coords=_at(p.lat, p.lon),
                distance_km=site.distance_km(p.lat, p.lon),
            )
        )
    return out


def _limiting_factor(value: str | None) -> str | None:
    """Northern Powergrid repeats the RAG colour: "Red - Fault Level" -> "Fault Level"; "Green" -> None."""
    _, _, factor = (value or "").partition(" - ")
    return factor.strip() or None


def npg_substations(records: Sequence[npg.CapacityHeatmapSite], site: Site) -> list[Substation]:
    """Northern Powergrid primaries, BSPs and GSPs with their LTDS heatmap headroom.

    `gsp` / `bsp` are NPg asset ids ("GSP-000038"), not names, so they are left out: they would never match NESO.
    """
    out: list[Substation] = []
    for r in records:
        if r.latitude is None or r.longitude is None:
            continue
        out.append(
            Substation(
                name=(r.name or "Unnamed").strip(),
                operator="Northern Powergrid",
                kind=(r.type or "primary").lower(),
                voltage_kv=r.voltages,
                voltages=f"{r.voltages:g}" if r.voltages else None,
                connection_voltage_kv=_connection_kv(None, [r.voltages or 0], r.name),
                coords=_at(r.latitude, r.longitude),
                distance_km=site.distance_km(r.latitude, r.longitude),
                headroom=Headroom(
                    generation_mw=r.generationavailablecapacity,
                    generation_rag=_rag(r.generationconstraint),
                    generation_constraint=_limiting_factor(r.generationconstraintlimitingfactor),
                    demand=r.demandavailablecapacity,
                    demand_rag=_rag(r.demandconstraint),
                    demand_constraint=_limiting_factor(r.demandconstraintlimitingfactor),
                    basis="Northern Powergrid capacity heatmap (LTDS): published available capacity",
                    demand_firm_mw=r.demandfirmcapacity,
                    demand_max_mw=r.demandmaximum,
                    demand_min_mw=r.demandminimum,
                    generation_firm_mw=r.generationfirmcapacity,
                    reverse_power_available_mw=r.reversepowerflowavailablecapacity,
                ),
            )
        )
    return out


def grid_projects(
    records: Sequence[
        ukpn.EmbeddedCapacityRecord
        | nged.EcrRecord
        | ssen_distribution.EcrRecord
        | sp_energy.EmbeddedCapacityRecord
    ],
    operator: Operator,
    site: Site,
) -> list[GridProject]:
    """Embedded capacity register rows (the same Ofgem columns at every operator) of at least 1 MW."""
    out: list[GridProject] = []
    for r in records:
        point = getattr(r, "spatialcoordinates_customer", None) or getattr(r, "coordinates", None)
        lat = point.lat if point else getattr(r, "lat", None)
        lon = point.lon if point else getattr(r, "lon", None)
        capacity = _float(r.registered_capacity_1_mw)
        if lat is None or lon is None or (capacity is not None and capacity < MIN_PROJECT_MW):
            continue
        source = (r.energy_source_1 or "").lower()
        status = (r.connection_status or "").lower()
        export = _float(
            getattr(r, "maximum_export_capacity_mw", None)
            or getattr(r, "connected_maximum_export_capacity_mw", None)
        )
        imp = _float(
            getattr(r, "maximum_import_capacity_mw", None)
            or getattr(r, "connected_maximum_import_capacity_mw", None)
        )
        out.append(
            GridProject(
                name=(r.customer_site or "").strip().title() or None,
                operator=operator,
                coords=_at(lat, lon),
                distance_km=site.distance_km(lat, lon),
                technology=(r.energy_conversion_technology_1 or "").strip().capitalize() or None,
                is_storage=source.startswith("stored"),
                is_solar=source.startswith("solar"),
                capacity_mw=capacity,
                storage_mwh=_float(r.storage_capacity_1_mwh),
                status="connected" if status.startswith("connected") else "accepted" if "accepted" in status else None,
                connected_on=_date(r.date_connected),
                target_energisation=_date(r.target_energisation_date),
                max_export_mw=export,
                max_import_mw=imp,
                flexible_connection=_yes(r.flexible_connection_yes_no),
                in_queue=_yes(r.in_a_connection_queue_y_n),
                primary=_text(r.primary),
                bsp=_text(r.bulk_supply_point),
                gsp=_text(r.grid_supply_point),
            )
        )
    return out


def overhead_lines(
    lines: Iterable[tuple[Operator, float | None, dict[str, Any]]], site: Site, max_m: float
) -> list[OverheadLine]:
    """(operator, kV, GeoJSON geometry) triples -> lines within `max_m` of the site, nearest first."""
    out: list[OverheadLine] = []
    for operator, kv, geojson in lines:
        geom = from_geojson(geojson)
        hit, _, distance = site.measure(geom)
        if distance <= max_m:
            out.append(
                OverheadLine(
                    operator=operator, voltage_kv=kv, distance_m=distance, crosses_site=hit, geometry=to_geometry(geom)
                )
            )
    return sorted(out, key=lambda ln: ln.distance_m)


def line_inputs(
    ukpn_lines: Sequence[ukpn.OverheadLine],
    ssen_t_lines: Sequence[ssen.OverheadLine],
    ssen_d_lines: Sequence[ssen_distribution.OverheadLine],
    sp_lines: Sequence[sp_energy.GisLineAsset] = (),
) -> list[tuple[Operator, float | None, dict[str, Any]]]:
    """The operators' line rows as (operator, kV, GeoJSON geometry)."""
    out: list[tuple[Operator, float | None, dict[str, Any]]] = [
        ("UKPN", _float(ln.voltage), ln.geo_shape.geometry.model_dump()) for ln in ukpn_lines if ln.geo_shape
    ]
    out += [
        ("SSEN Transmission", ln.voltage / 1000 if ln.voltage else None, ln.geo_shape.geometry.model_dump())
        for ln in ssen_t_lines
        if ln.geo_shape
    ]
    for ln in ssen_d_lines:
        if not ln.route_lat_long:
            continue
        coordinates = ssen_distribution.linestring_coordinates(ln.route_lat_long)
        if len(coordinates) > 1 and LineString(coordinates).is_valid:
            out.append((
                "SSEN Distribution",
                _float(ln.nominal_voltage_pp),
                {"type": "LineString", "coordinates": coordinates},
            ))
    for ln in sp_lines:
        if not ln.geo_shape:
            continue
        kv = _float(ln.voltage)
        if kv and kv > VOLTS_THRESHOLD:
            kv /= 1000
        out.append(("SP Energy Networks", kv, ln.geo_shape.geometry.model_dump()))
    return out


def transmission_projects(
    records: Sequence[neso.TecRegisterRecord | ssen.RegisterRecord],
    register: Literal["NESO TEC", "SSEN TEC", "SSEN Embedded"],
) -> list[TransmissionProject]:
    """TEC / embedded register rows at the site's grid supply points, largest first."""
    out = [
        TransmissionProject(
            name=r.project_name,
            connection_site=r.connection_site,
            capacity_mw=r.cumulative_total_capacity_mw,
            connected_mw=r.mw_connected,
            status=r.project_status,
            plant_type=r.plant_type,
            is_storage=ssen.STORAGE_PLANT_TYPE.lower() in (r.plant_type or "").lower(),
            effective_from=r.mw_effective_from,
            agreement_type=getattr(r, "agreement_type", None),
            gate=int(g) if (g := getattr(r, "gate", None)) else None,
            listed_on=register,
        )
        for r in records
    ]
    return sorted(out, key=lambda p: -(p.capacity_mw or 0))


def search_terms(where: Locality) -> list[str]:
    """Place names for the local news / sentiment search, most specific first."""
    parish = where.parish if where.parish and not where.parish.endswith("unparished area") else None
    names = (where.place, parish, where.ward, where.district, where.planning_authority, where.county)
    return list(dict.fromkeys(n for n in names if n))
