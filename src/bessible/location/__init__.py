"""Everything we know about a location that bears on building a battery there: `Coordinates` -> `LocationData`.

    from bessible.location import Coordinates, collate

    data = await collate(Coordinates(lat=51.246403, lon=-2.198739))
    data.title                 the title boundary: the area everything is measured against
    data.deterministic         facts to compute on: locality, terrain, flood, land, designations, grid
    data.agentic               material for agents: documents to read, free-text notes, search terms
    data.sources               every upstream request and whether it worked (URLs double as artifact sources)

`uv run python -m bessible.location <lat> <lon>` prints the result as JSON.

    models      the tidy classes (no I/O)
    collate     the orchestration and all HTTP
    transform   pure wire-model -> tidy-model functions
    geometry    measuring against the site (shapely on a local metric frame)
"""

from __future__ import annotations

from .collate import collate, locality
from .extract import ExtractedLocation, LocationNotFound, extract_location, resolve_from_link
from .fetch import PageUnavailable, fetch_page_text
from .models import (
    Agentic,
    Coordinates,
    Designation,
    Deterministic,
    Grid,
    Land,
    Locality,
    LocationData,
    Terrain,
    TitleBoundary,
)

__all__ = [
    "Agentic",
    "Coordinates",
    "Designation",
    "Deterministic",
    "ExtractedLocation",
    "Grid",
    "Land",
    "Locality",
    "LocationData",
    "LocationNotFound",
    "PageUnavailable",
    "Terrain",
    "TitleBoundary",
    "collate",
    "extract_location",
    "fetch_page_text",
    "locality",
    "resolve_from_link",
]
