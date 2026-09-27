"""`collate(Coordinates) -> LocationData`: call every data source, measure against the title, tidy up.

All the I/O of the package lives here (the transforms in `.transform` are pure). A source that fails or does not
cover the location never fails the whole call: it is recorded in `LocationData.sources` and its section stays
empty / None.

Order of work:
    1. from the point alone, concurrently: title boundary, address, postcode, and every grid table
    2. once the title is known: planning designations, flood zones, Natural England layers, terrain
    3. once names are known: local plans of the planning authority, transmission queue at the grid supply points
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any, Literal

import httpx
from shapely.geometry import Point

from bessible.api import (
    ckan,
    ea_flood,
    ea_lidar,
    natural_england,
    neso,
    nged,
    nominatim,
    npg,
    open_meteo,
    opendatasoft,
    planning_data,
    postcodes_io,
    sp_energy,
    ssen,
    ssen_distribution,
    ukpn,
)
from bessible.config import settings

from . import transform
from .geometry import Site
from .models import (
    Agentic,
    Coordinates,
    Designation,
    Deterministic,
    Grid,
    Land,
    LocationData,
    SourceDocument,
    SourceStatus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import SecretStr

NEARBY_M = 2000  # designations are reported out to this distance from the title
FLOOD_MARGIN_M = 250
GRID_KM = 10.0  # substations, headroom, competing projects
LINES_M = 3000
TRANSMISSION_KM = 25.0  # transmission substations are sparse
MAX_GSPS = 4  # nearest distinct grid supply points matched against the TEC register
LIDAR_MAX_PX = 250  # longer side of the LIDAR raster; titles under 250 m stay at the native 1 m
TABLE_TTL_S = 6 * 3600  # whole-table downloads (no spatial query upstream) are reused for this long
PAGE = 500  # planning.data page size

# Fetched by the site polygon WITHOUT geometry (large polygons; we only need "touches the title")...
ON_SITE_DATASETS = [d for d in planning_data.BESS_POINT_DATASETS if d != "title-boundary"] + ["local-plan-boundary"]
# ...and these again WITH geometry out to NEARBY_M, to measure distance and overlap (small geometries).
NEARBY_DATASETS = [
    "conservation-area",
    "listed-building",
    "scheduled-monument",
    "heritage-at-risk",
    "park-and-garden",
    "battlefield",
    "world-heritage-site",
    "archaeological-priority-area",
    "tree-preservation-zone",
    "article-4-direction-area",
    "brownfield-land",
    "infrastructure-project",
    "planning-application",
]
NE_MARGIN_M: dict[str, float] = {"alc_provisional": 0, "alc_post_1988": 0, "sssi_irz": 0}  # others: NEARBY_M

_tables: dict[str, tuple[float, Any, str]] = {}  # name -> (fetched at, table, url)


class _Fetcher:
    """GETs a request model, parses the body, and logs the outcome as a `SourceStatus`."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.sources: list[SourceStatus] = []

    async def get[T](  # ruff: ignore[too-many-arguments]
        self,
        name: str,
        req: Any,  # ruff: ignore[any-type] - any `bessible.api` request model
        parse: Callable[[Any], T],
        *,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        count: Callable[[T], int] | None = None,
        raw: bool = False,
    ) -> T | None:
        """The parsed response, or None (and a "failed" source) on any error."""
        address: str = url or (req.url() if callable(getattr(req, "url", None)) else req.URL)
        full = str(httpx.URL(address, params=req.params()))
        try:
            r = await self.client.get(address, params=req.params(), headers=headers)
            r.raise_for_status()
            result = parse(r.content if raw else r.json())
        except Exception as e:  # ruff: ignore[blind-except] - one bad source must not sink the whole lookup
            self.sources.append(
                SourceStatus(name=name, url=full, status="failed", detail=f"{type(e).__name__}: {e}"[:300])
            )
            return None
        self.sources.append(SourceStatus(name=name, url=full, status="ok", records=count(result) if count else None))
        return result

    async def table[T](self, name: str, fetch: Callable[[], Awaitable[T | None]]) -> T | None:
        """A location-independent table, downloaded once per `TABLE_TTL_S` per process."""
        hit = _tables.get(name)
        if hit and time.monotonic() - hit[0] < TABLE_TTL_S:
            self.sources.append(
                SourceStatus(name=name, url=hit[2], status="ok", detail="reused from this process's cache")
            )
            return hit[1]  # type: ignore[no-any-return]
        result = await fetch()
        if result is not None:
            url = next((s.url for s in reversed(self.sources) if s.name.startswith(name)), "")
            _tables[name] = (time.monotonic(), result, url)
        return result

    def skip(self, name: str, url: str, why: str) -> None:
        """Record a source that was not called."""
        self.sources.append(SourceStatus(name=name, url=url, status="skipped", detail=why))


def _safe[T](f: _Fetcher, name: str, build: Callable[[], T], default: T) -> T:
    """Run one section's transform; a surprise in its data empties that section instead of the whole result."""
    try:
        return build()
    except Exception as e:  # ruff: ignore[blind-except] - upstream data we have not seen before
        f.sources.append(
            SourceStatus(name=f"Tidying: {name}", url="", status="failed", detail=f"{type(e).__name__}: {e}"[:300])
        )
        return default


def _key(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret else None


def _features(response: Any) -> int:  # ruff: ignore[any-type]
    return len(response.features)


def _records(response: Any) -> int:  # ruff: ignore[any-type]
    return len(response.result.records) if response.result else 0


def _results(response: Any) -> int:  # ruff: ignore[any-type]
    return len(response.results)


# ------------------------------------------- 1. grid -------------------------------------------- #


class _GridRaw:
    """Raw rows near the point from every operator (empty lists where an operator has nothing / failed)."""

    def __init__(self) -> None:
        self.ukpn: dict[str, list[Any]] = {}
        self.ssen_t: dict[str, list[Any]] = {}
        self.sp_energy: dict[str, list[Any]] = {}
        self.npg: dict[str, list[Any]] = {}
        self.nged_capacity: list[nged.CapacityMapSite] = []
        self.nged_ecr: list[nged.EcrRecord] = []
        self.ssen_d_headroom: list[ssen_distribution.HeadroomSite] = []
        self.ssen_d_ecr: list[ssen_distribution.EcrRecord] = []
        self.ssen_d_lines: list[ssen_distribution.OverheadLine] = []


async def _fetch_grid(f: _Fetcher, c: Coordinates) -> _GridRaw:
    raw = _GridRaw()

    async def ods(module: Any, name: str, radius_m: float, key: str, into: dict[str, list[Any]]) -> None:  # ruff: ignore[any-type]
        spec = module.DATASETS[name]
        labels = {ukpn: "UKPN", ssen: "SSEN Transmission", sp_energy: "SP Energy Networks", npg: "Northern Powergrid"}
        label = labels[module]
        got = await f.get(
            f"{label}: {name}",
            spec.near(c.lat, c.lon, radius_m),
            spec.parse,
            headers=opendatasoft.auth_headers(key),
            count=_results,
        )
        into[name] = got.results if got else []

    async def nearest_rows(name: str, req: Any, model: Any, module: Any, headers: dict[str, str]) -> list[Any]:  # ruff: ignore[any-type]
        table = await f.table(name, lambda: f.get(name, req, model.model_validate, headers=headers, count=_records))
        rows = table.result.records if table and table.result else []
        return [r for _, r in module.nearest(rows, c.lat, c.lon, GRID_KM + 1)]

    async def nged_tables() -> None:
        headers = nged.auth_headers(_key(settings.nged_api_key) or "")
        raw.nged_capacity, raw.nged_ecr = await asyncio.gather(
            nearest_rows(
                "NGED: network capacity map", nged.capacity_map_request(), nged.CapacityMapResponse, nged, headers
            ),
            nearest_rows("NGED: embedded capacity register", nged.ecr_request(), nged.EcrResponse, nged, headers),
        )

    async def ssen_d_tables() -> None:
        sd, headers = ssen_distribution, ssen_distribution.HEADERS
        raw.ssen_d_headroom, raw.ssen_d_ecr = await asyncio.gather(
            nearest_rows("SSEN Distribution: headroom", sd.headroom_request(), sd.HeadroomResponse, sd, headers),
            nearest_rows(
                "SSEN Distribution: embedded capacity register", sd.ecr_request(), sd.EcrResponse, sd, headers
            ),
        )
        name = "SSEN Distribution: overhead lines 22 kV+"
        lines = await f.table(
            name,
            lambda: f.get(name, sd.lines_request(), sd.LinesResponse.model_validate, headers=headers, count=_records),
        )
        reach = LINES_M + 1000
        dlat, dlon = reach / 110_540, reach / (111_320 * math.cos(math.radians(c.lat)))
        raw.ssen_d_lines = [
            ln
            for ln in (lines.result.records if lines and lines.result else [])
            if ln.route_lat_long
            and any(
                abs(y - c.lat) <= dlat and abs(x - c.lon) <= dlon
                for x, y in sd.linestring_coordinates(ln.route_lat_long)
            )
        ]

    jobs: list[Awaitable[None]] = [nged_tables(), ssen_d_tables()]
    if key := _key(settings.ukpn_api_key):
        # Tables without a location field (LTDS table 2a, GSP project status) are whole-dataset: the snapshot ingest
        # fetches them; a radius query would send `within_distance(None, ...)`.
        spatial = [n for n, spec in ukpn.DATASETS.items() if spec.geo_field]
        jobs += [ods(ukpn, n, LINES_M + 1000 if "lines" in n else GRID_KM * 1000, key, raw.ukpn) for n in spatial]
    else:
        f.skip("UKPN", ukpn.BASE_URL, "UKPN_API_KEY is not set")
    if key := _key(settings.ssen_api_key):
        jobs += [ods(ssen, n, TRANSMISSION_KM * 1000, key, raw.ssen_t) for n in ssen.DATASETS]
    else:
        f.skip("SSEN Transmission", ssen.BASE_URL, "SSEN_API_KEY is not set")
    if key := _key(settings.spen_api_key):
        jobs += [
            ods(sp_energy, n, LINES_M + 1000 if "lines" in n else GRID_KM * 1000, key, raw.sp_energy)
            for n in sp_energy.DATASETS
        ]
    else:
        f.skip("SP Energy Networks", sp_energy.BASE_URL, "SPEN_API_KEY is not set")
    if key := _key(settings.npg_api_key):
        jobs += [ods(npg, n, GRID_KM * 1000, key, raw.npg) for n in npg.DATASETS]
    else:
        f.skip("Northern Powergrid", npg.BASE_URL, "NPG_API_KEY is not set")
    await asyncio.gather(*jobs)
    return raw


async def _build_grid(f: _Fetcher, raw: _GridRaw, site: Site) -> tuple[Grid, list[str]]:
    sd_subs, notes = transform.ssen_distribution_substations(raw.ssen_d_headroom, site)
    sp_subs = transform.sp_energy_substations(
        raw.sp_energy.get("capacity_heatmap_spd", []) + raw.sp_energy.get("capacity_heatmap_spm", []),
        site,
        point_assets=raw.sp_energy.get("substations_spd", []) + raw.sp_energy.get("substations_spm", []),
    )
    distribution = (
        transform.ukpn_substations(raw.ukpn.get("substations", []), raw.ukpn.get("capacity_heatmap", []), site)
        + transform.nged_substations(raw.nged_capacity, site)
        + sd_subs
        + sp_subs
        + transform.npg_substations(raw.npg.get("capacity_heatmap", []), site)
    )
    ssen_t_rows = raw.ssen_t.get("substations_132kv", []) + raw.ssen_t.get("substations_supergrid", [])
    transmission = sorted(transform.ssen_transmission_substations(ssen_t_rows, site), key=lambda s: s.distance_km)
    substations = sorted(distribution + transmission, key=lambda s: s.distance_km)
    projects = (
        transform.grid_projects(raw.ukpn.get("embedded_capacity_register", []), "UKPN", site)
        + transform.grid_projects(raw.nged_ecr, "NGED", site)
        + transform.grid_projects(raw.ssen_d_ecr, "SSEN Distribution", site)
        + transform.grid_projects(raw.sp_energy.get("embedded_capacity_register", []), "SP Energy Networks", site)
    )
    lines = transform.overhead_lines(
        transform.line_inputs(
            raw.ukpn.get("overhead_lines_132kv", []) + raw.ukpn.get("overhead_lines_33kv", []),
            raw.ssen_t.get("overhead_lines_132kv", []) + raw.ssen_t.get("overhead_lines_supergrid", []),
            raw.ssen_d_lines,
            raw.sp_energy.get("lines_spd", []) + raw.sp_energy.get("lines_spm", []),
        ),
        site,
        LINES_M,
    )

    # The transmission registers have no coordinates: they are matched on the names of the grid supply points
    # feeding the nearest substations.
    gsps = list(dict.fromkeys(s.gsp for s in sorted(distribution, key=lambda s: s.distance_km) if s.gsp))
    gsps = list(dict.fromkeys(gsps + [s.name for s in transmission]))[:MAX_GSPS]
    queue = []
    if gsps:
        tec = await f.get(
            f"NESO: TEC register at {', '.join(gsps)}",
            neso.tec_at_sites(gsps),
            ckan.DatastoreSearchSqlResponse[neso.TecRegisterRecord].model_validate,
            count=_records,
        )
        queue += transform.transmission_projects(tec.result.records if tec and tec.result else [], "NESO TEC")
    else:
        f.skip("NESO: TEC register", neso.BASE_URL, "no grid supply point name known near this site")
    labels: dict[str, Literal["SSEN TEC", "SSEN Embedded"]] = {
        "tec_register": "SSEN TEC",
        "embedded_register": "SSEN Embedded",
    }
    if transmission and (key := _key(settings.ssen_api_key)):
        for register, label in labels.items():
            rows = await f.get(
                f"SSEN Transmission: {register} at {transmission[0].name}",
                ssen.register_request(register, transmission[0].name),
                ssen.parse_register,
                headers=opendatasoft.auth_headers(key),
                count=_results,
            )
            queue += transform.transmission_projects(rows.results if rows else [], label)

    return (
        Grid(
            operators=list(
                dict.fromkeys(
                    [s.operator for s in substations] + [p.operator for p in projects] + [x.operator for x in lines]
                )
            ),
            substations=[
                s for s in substations if s.distance_km <= (TRANSMISSION_KM if s.kind == "transmission" else GRID_KM)
            ],
            lines=lines,
            projects=sorted((p for p in projects if p.distance_km <= GRID_KM), key=lambda p: p.distance_km),
            gsps=gsps,
            transmission_queue=sorted(queue, key=lambda p: -(p.capacity_mw or 0)),
        ),
        notes,
    )


# ---------------------------------- 2. the title and the land ----------------------------------- #


async def _fetch_terrain(f: _Fetcher, site: Site) -> Any:  # ruff: ignore[any-type] - Terrain | None
    w, s, e, n = site.bbox
    dtm = await f.get(
        "EA LIDAR: 1 m terrain model",
        ea_lidar.dtm_for_bbox(s, w, n, e, LIDAR_MAX_PX),
        ea_lidar.DtmGrid.from_geotiff,
        raw=True,
    )
    terrain = _safe(f, "terrain", lambda: transform.terrain_from_lidar(dtm, site), None) if dtm else None
    if terrain:
        return terrain
    # Not England (the service answers HTTP 500 outside its coverage) or no data: the coarse global model.
    lons, lats = transform.sample_points(site)
    if not len(lons):
        return None
    req = open_meteo.ElevationRequest(latitude=[float(v) for v in lats], longitude=[float(v) for v in lons])
    got = await f.get(
        "Open-Meteo: 90 m elevation (LIDAR unavailable)", req, open_meteo.ElevationResponse.model_validate
    )
    return transform.terrain_from_points(list(lons), list(lats), got.elevation) if got else None


async def _entities(f: _Fetcher, name: str, req: planning_data.EntitySearchRequest) -> list[planning_data.EntityBase]:
    got = await f.get(name, req, planning_data.EntitySearchResponse.model_validate, count=lambda r: len(r.entities))
    return list(got.entities) if got else []


async def _local_plans(f: _Fetcher) -> list[planning_data.EntityBase]:
    """The whole local-plan register (~1k rows, no geometry): it cannot be filtered by authority upstream."""

    async def fetch() -> list[planning_data.EntityBase] | None:
        rows: list[planning_data.EntityBase] = []
        for offset in range(0, 10 * PAGE, PAGE):
            req = planning_data.EntitySearchRequest(dataset=["local-plan"], limit=PAGE, offset=offset)
            page = await _entities(f, f"Planning Data: local plans (rows {offset}+)", req)
            rows += page
            if len(page) < PAGE:
                break
        return rows or None

    return await f.table("Planning Data: local plans", fetch) or []


# ------------------------------------------- collate -------------------------------------------- #


async def collate(coords: Coordinates, *, client: httpx.AsyncClient | None = None) -> LocationData:  # ruff: ignore[too-many-locals]
    """Everything the data sources know about a coordinate that bears on building a battery there."""
    if client is None:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "bessible"}, follow_redirects=True) as own:
            return await collate(coords, client=own)
    f = _Fetcher(client)
    lat, lon = coords.lat, coords.lon

    grid_raw = asyncio.create_task(_fetch_grid(f, coords))
    title_req = planning_data.EntitySearchRequest(
        latitude=lat, longitude=lon, dataset=["title-boundary"], geometry_relation="intersects"
    )
    titles, postcodes, address = await asyncio.gather(
        f.get(
            "Planning Data: title boundary",
            title_req,
            planning_data.EntityGeoJsonResponse.model_validate,
            url=title_req.GEOJSON_URL,
            count=_features,
        ),
        f.get(
            "Postcodes.io: nearest postcode",
            postcodes_io.ReverseGeocodeRequest(lat=lat, lon=lon, widesearch=True, limit=1),
            postcodes_io.ReverseGeocodeResponse.model_validate,
        ),
        f.get(
            "Nominatim: address", nominatim.ReverseRequest(lat=lat, lon=lon), nominatim.ReverseResponse.model_validate
        ),
    )
    found = transform.title_boundary(titles, coords) if titles else None
    title, boundary = found or (None, None)
    site = Site(coords, boundary)
    postcode = postcodes.result[0] if postcodes and postcodes.result else None
    in_england = postcode is None or postcode.country == "England"

    # 2. Everything measured against the title (or the point, when there is none).
    shape_wkt = site.buffered_wkt(0) if boundary is not None else Point(lon, lat).wkt
    min_lon, min_lat, max_lon, max_lat = site.bbox_with_margin(FLOOD_MARGIN_M)
    on_site_req = planning_data.EntitySearchRequest(
        geometry=[shape_wkt],
        geometry_relation="intersects",
        dataset=ON_SITE_DATASETS,
        exclude_field=["geometry"],
        limit=PAGE,
    )
    nearby_req = planning_data.EntitySearchRequest(
        geometry=[site.buffered_wkt(NEARBY_M)], geometry_relation="intersects", dataset=NEARBY_DATASETS, limit=PAGE
    )

    async def ne_layer(name: str) -> tuple[str, Any]:
        spec = natural_england.LAYERS[name]
        margin = NE_MARGIN_M.get(name, NEARBY_M) or None
        req = spec.in_bbox(*_lat_lon_box(site.bbox), distance_m=margin) if boundary else spec.at_point(lat, lon, margin)
        req.max_allowable_offset = 0.00001  # ~1 m: trims huge designations without visibly distorting small ones
        req.geometry_precision = 6
        return name, await f.get(f"Natural England: {name}", req, spec.parse, count=_features)

    if not in_england:
        f.skip(
            "Environment Agency / Natural England", natural_england.BASE_URL, "England-only sources; site is elsewhere"
        )
    on_site, nearby, flood, terrain, plans, *ne = await asyncio.gather(
        _entities(f, "Planning Data: designations on the title", on_site_req),
        _entities(f, f"Planning Data: designations within {NEARBY_M} m", nearby_req),
        f.get(
            "EA: flood zones",
            ea_flood.flood_zones_in_bbox(min_lat, min_lon, max_lat, max_lon),
            ea_flood.FloodZoneResponse.model_validate,
            count=_features,
        )
        if in_england
        else asyncio.sleep(0),
        _fetch_terrain(f, site) if boundary is not None else asyncio.sleep(0),
        _local_plans(f),
        *([ne_layer(n) for n in natural_england.LAYERS] if in_england else []),
    )
    layers = {name: response for name, response in ne if response is not None}

    where = transform.locality(postcode, address, on_site)
    no_land: tuple[Land | None, list[SourceDocument]] = (None, [])
    no_layers: tuple[list[Designation], list[SourceDocument]] = ([], [])
    no_entities: tuple[list[Designation], list[SourceDocument], list[str]] = ([], [], [])
    land, land_docs = _safe(
        f,
        "land",
        lambda: transform.land(layers.get("alc_provisional"), layers.get("alc_post_1988"), site, on_site),
        no_land,
    )
    ne_designations, ne_docs = _safe(
        f, "Natural England designations", lambda: transform.natural_england_designations(layers, site), no_layers
    )
    pd_designations, pd_docs, notes = _safe(
        f, "planning designations", lambda: transform.planning_designations(on_site, nearby, site), no_entities
    )
    designations = sorted(
        ne_designations + pd_designations, key=lambda d: (not d.on_site, d.distance_m or 0, -(d.overlap_pct or 0))
    )

    # 3. Needs names from the rounds above.
    try:
        grid, grid_notes = await _build_grid(f, await grid_raw, site)
    except Exception as e:  # ruff: ignore[blind-except] - as `_safe`, for the one async section
        f.sources.append(
            SourceStatus(name="Tidying: grid", url="", status="failed", detail=f"{type(e).__name__}: {e}"[:300])
        )
        grid, grid_notes = Grid(), []
    documents = transform.local_plan_documents(plans, where.planning_authority_code) + land_docs + pd_docs + ne_docs

    return LocationData(
        coords=coords,
        title=title,
        deterministic=Deterministic(
            locality=where,
            terrain=terrain,
            flood=_safe(f, "flood", lambda: transform.flood_risk(flood, site, on_site), None) if flood else None,
            land=land if (in_england and (layers or on_site)) else None,
            designations=designations,
            grid=grid,
        ),
        agentic=Agentic(
            documents=list({d.url: d for d in documents}.values()),
            notes=notes + grid_notes,
            search_terms=transform.search_terms(where),
        ),
        sources=f.sources,
    )


AREA_DATASETS = ["local-planning-authority", "local-authority-district", "parish", "ward", "region", "built-up-area"]


async def locality(coords: Coordinates, *, client: httpx.AsyncClient | None = None) -> LocationData:
    """The administrative geography and search terms of `collate`, from 3 calls instead of ~40.

    For stages that run beside `site_land` and need names, not the land. Areas are looked up at the point, not the
    title.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "bessible"}, follow_redirects=True) as own:
            return await locality(coords, client=own)
    f = _Fetcher(client)
    lat, lon = coords.lat, coords.lon
    areas_req = planning_data.EntitySearchRequest(
        latitude=lat, longitude=lon, dataset=AREA_DATASETS, exclude_field=["geometry"], limit=PAGE
    )
    postcodes, address, areas = await asyncio.gather(
        f.get(
            "Postcodes.io: nearest postcode",
            postcodes_io.ReverseGeocodeRequest(lat=lat, lon=lon, widesearch=True, limit=1),
            postcodes_io.ReverseGeocodeResponse.model_validate,
        ),
        f.get(
            "Nominatim: address", nominatim.ReverseRequest(lat=lat, lon=lon), nominatim.ReverseResponse.model_validate
        ),
        _entities(f, "Planning Data: areas at the point", areas_req),
    )
    postcode = postcodes.result[0] if postcodes and postcodes.result else None
    where = transform.locality(postcode, address, areas)
    return LocationData(
        coords=coords,
        deterministic=Deterministic(locality=where),
        agentic=Agentic(search_terms=transform.search_terms(where)),
        sources=f.sources,
    )


def _lat_lon_box(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) -> (min_lat, min_lon, max_lat, max_lon), the API builders' order."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lat, min_lon, max_lat, max_lon
