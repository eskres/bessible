"""planning.data `title-boundary` searches: every INSPIRE polygon that intersects a shape.

A failed request raises (`httpx.HTTPError`), so the Temporal activity retries it. Responses are cached under
`out/cache/titles/` for a day: the index is republished monthly.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING

import httpx

from bessible.api import planning_data
from bessible.config import settings

if TYPE_CHECKING:
    from pathlib import Path

PAGE = 500
MAX_PAGES = 6  # 3,000 polygons: a town centre inside the minimum radius; the nearest are kept
CACHE_TTL_S = 24 * 3600
SOURCE_NAME = "planning.data title-boundary"


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
    return settings.cache_dir / "titles" / f"{digest}.json"


def search_url(wkt: str) -> str:
    """The first page's URL: the source to cite for a search."""
    req = planning_data.EntitySearchRequest(
        dataset=["title-boundary"], geometry=[wkt], geometry_relation="intersects", limit=PAGE
    )
    return str(httpx.URL(req.GEOJSON_URL, params=req.params()))


async def search_titles(
    wkt: str, *, client: httpx.AsyncClient | None = None, max_pages: int = MAX_PAGES
) -> tuple[planning_data.EntityGeoJsonResponse, bool]:
    """Every `title-boundary` polygon intersecting the WKT shape (degrees), and whether pages were left unread."""
    cache = _cache_path(f"{wkt}|{max_pages}")
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return planning_data.EntityGeoJsonResponse.model_validate(data["response"]), bool(data["truncated"])

    if client is None:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "bessible"}, follow_redirects=True) as own:
            return await search_titles(wkt, client=own, max_pages=max_pages)

    features: list[planning_data.EntityFeature] = []
    truncated = False
    for page in range(max_pages):
        req = planning_data.EntitySearchRequest(
            dataset=["title-boundary"],
            geometry=[wkt],
            geometry_relation="intersects",
            limit=PAGE,
            offset=page * PAGE or None,
        )
        r = await client.get(req.GEOJSON_URL, params=req.params())
        r.raise_for_status()
        got = planning_data.EntityGeoJsonResponse.model_validate(r.json())
        features += got.features
        if len(got.features) < PAGE:
            break
    else:
        truncated = True

    response = planning_data.EntityGeoJsonResponse(type="FeatureCollection", features=features)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"response": response.model_dump(mode="json", by_alias=True), "truncated": truncated}),
        encoding="utf-8",
    )
    return response, truncated


def bbox_wkt(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """WKT polygon of a lon/lat box."""
    return (
        f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, {max_lon} {max_lat}, "
        f"{min_lon} {max_lat}, {min_lon} {min_lat}))"
    )
