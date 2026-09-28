"""FastAPI HTTP Server for Bessible."""

from __future__ import annotations

import logging
import math

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bessible.api import capacity, demo, events, me, runs
from bessible.auth import current_user
from bessible.config import settings
from bessible.keystore import KeyStoreError

if not settings.auth_enabled:
    logging.getLogger(__name__).warning("AUTH_ENABLED=false: sign-in is off, every caller is the local user")

app = FastAPI(
    title="Bessible API",
    description="HTTP API and SSE Events Bridge for BESS site assessment workflow",
    version="0.1.0",
)

# Enable CORS for the Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include route modules
app.include_router(runs.router)
app.include_router(demo.router)
app.include_router(capacity.router, dependencies=[Depends(current_user)])
app.include_router(events.router)
app.include_router(me.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> Response:
    """FastAPI's 422, except that `/me` errors never echo the submitted input (it may be an API key)."""
    if request.url.path.startswith("/me"):
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]},
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(KeyStoreError)
def key_store_unavailable(_request: Request, _exc: KeyStoreError) -> JSONResponse:
    """The master secret is missing or wrong: say so without detail."""
    return JSONResponse(status_code=503, content={"detail": "key storage is not configured"})


@app.get("/data/areas.geojson", tags=["data"])
async def get_areas_geojson() -> Response:
    """Serve the UKPN serving areas GeoJSON or an empty FeatureCollection if not yet ingested."""
    candidates = [
        settings.data_dir / "ukpn" / "areas.geojson",
        settings.data_dir / "areas.geojson",
        settings.data_dir.parent / "data" / "ukpn" / "areas.geojson",
    ]
    for path in candidates:
        if path.exists():
            return Response(content=path.read_text(encoding="utf-8"), media_type="application/geo+json")

    return JSONResponse(content={"type": "FeatureCollection", "features": []})


INSPIRE_MAX_SPAN_M = 3000  # a bigger box would page through thousands of town polygons
INSPIRE_MAX_FEATURES = 1500


@app.get("/inspire", tags=["data"], dependencies=[Depends(current_user)])
async def get_inspire_parcels(bbox: str) -> Response:
    """HM Land Registry INSPIRE index polygons in a box (`min_lon,min_lat,max_lon,max_lat`), from planning.data.

    The same `title-boundary` dataset as the title stage, so the user can add polygons outside its candidates.
    Features carry `TitleParcel` fields; nearest to the box centre first, at most 1,500.
    """
    import httpx  # ruff: ignore[import-outside-top-level]

    from bessible.location.geometry import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR  # ruff: ignore[import-outside-top-level]
    from bessible.models import Position  # ruff: ignore[import-outside-top-level]
    from bessible.titles import parcels, search  # ruff: ignore[import-outside-top-level]

    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        return JSONResponse(status_code=422, content={"detail": "bbox must be min_lon,min_lat,max_lon,max_lat"})
    if not (min_lon < max_lon and min_lat < max_lat):
        return JSONResponse(status_code=422, content={"detail": "bbox is empty"})
    center = Position(lat=(min_lat + max_lat) / 2, lon=(min_lon + max_lon) / 2)
    span_m = max(
        (max_lat - min_lat) * M_PER_DEG_LAT,
        (max_lon - min_lon) * M_PER_DEG_LON_EQUATOR * math.cos(math.radians(center.lat)),
    )
    if span_m > INSPIRE_MAX_SPAN_M:
        return JSONResponse(status_code=422, content={"detail": f"bbox wider than {INSPIRE_MAX_SPAN_M} m: zoom in"})
    try:
        response, truncated = await search.search_titles(search.bbox_wkt(min_lon, min_lat, max_lon, max_lat))
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"detail": f"planning.data title-boundary failed: {exc}"})
    found = parcels.parcels_from_features(response, center, limit=INSPIRE_MAX_FEATURES)
    collection = parcels.feature_collection(found)
    collection["truncated"] = truncated or len(found) == INSPIRE_MAX_FEATURES
    return JSONResponse(content=collection)


_SITE_DATA_CACHE: dict[tuple[float, float], dict[str, object]] = {}


@app.get("/site-data", tags=["data"], dependencies=[Depends(current_user)])
async def get_site_data(lat: float, lon: float) -> Response:
    """Everything `location.collate` knows about a coordinate (the LocationData object), for the map layers."""
    from bessible.location import Coordinates, collate  # ruff: ignore[import-outside-top-level]

    key = (round(lat, 5), round(lon, 5))
    if key not in _SITE_DATA_CACHE:
        location = await collate(Coordinates(lat=lat, lon=lon))
        _SITE_DATA_CACHE[key] = location.model_dump(mode="json")
    return JSONResponse(content=_SITE_DATA_CACHE[key])


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Basic health check endpoint."""
    return {"status": "ok"}
