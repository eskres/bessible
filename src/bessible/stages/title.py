"""Title stage: the INSPIRE polygons around the site, before and after the human places the BESS footprint.

Before confirmation (`find_title_boundaries`): the polygon under the pin and every polygon around it (the
candidates the map offers), plus title numbers from public data, best first: the listing's own text, then
HMLR CCOD / OCOD (company-owned land). After confirmation (`confirm_title_site`): the polygons the footprint (or
the user's clicks) cover, each with its share of the footprint, and the numbers the user typed.

Rules: an INSPIRE id is not a title number, so a number is only ever taken from a named source; nothing here
automates Find a Property or any other HMLR service whose terms forbid it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import HttpUrl

from bessible.api import planning_data, postcodes_io
from bessible.config import settings
from bessible.footprint import M2_PER_ACRE, footprint_polygon, reserved_acres
from bessible.location.fetch import cached_page_text
from bessible.models import Artifact, Position, TitleInput, TitleNumber, TitleOutput, TitleParcel, TitleSiteInput
from bessible.titles import hmlr, parcels, search
from bessible.titles.numbers import find_title_numbers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shapely.geometry.base import BaseGeometry

INDICATIVE = (
    "INSPIRE polygons show the indicative extent of a registered title, not its legal boundary "
    "(gov.uk/guidance/inspire-index-polygons-spatial-data)"
)
NO_POLYGON = (
    "The pin is on no INSPIRE polygon: a road, a river, unregistered land, or outside England "
    "(the title-boundary dataset covers England only)"
)
INSPIRE_GUIDANCE = "https://www.gov.uk/guidance/inspire-index-polygons-spatial-data"
MAX_OWNERSHIP_ROWS = 12  # CCOD / OCOD numbers reported per site; the rest are counted in a note
NEARBY_POSTCODES_M = 150  # CCOD / OCOD rows at postcodes this close to the pin are site candidates
HA = 10_000


def _write(run_id: str, file_name: str, data: dict[str, Any]) -> None:
    run_dir = Path(f"out/{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / file_name).write_text(json.dumps(data), encoding="utf-8")


def _url(value: str | None) -> HttpUrl | None:
    return HttpUrl(value) if value else None


def footprint_acres(capacity_mw: float) -> float:
    """The mid-range reserved area of a 4 h battery of `capacity_mw` (the size the map draws)."""
    low, high = reserved_acres(capacity_mw, 4)
    return (low + high) / 2


# ------------------------------------------ before HITL ------------------------------------------ #


async def find_title_boundaries(inp: TitleInput, *, client: httpx.AsyncClient | None = None) -> TitleOutput:
    """The pin polygon, the candidates around it, and every title number a free source gives for the site."""
    if client is None:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "bessible"}, follow_redirects=True) as own:
            return await find_title_boundaries(inp, client=own)
    pos = inp.location.position
    acres = footprint_acres(inp.capacity.recommended_mw) if inp.capacity.recommended_mw > 0 else None
    radius = parcels.search_radius_m(acres)
    wkt = parcels.circle_wkt(pos, radius)
    response, truncated = await search.search_titles(wkt, client=client)  # raises on failure: the activity retries
    candidates = parcels.parcels_from_features(response, pos)
    pin = parcels.pin_parcel(candidates, pos)

    notes: list[str] = []
    if pin is None:
        notes.append(NO_POLYGON)
    if truncated or len(candidates) == parcels.MAX_CANDIDATES:
        notes.append(f"Dense area: the {len(candidates)} polygons nearest the pin are shown")

    numbers, number_notes, number_artifacts = await _title_numbers(inp, client)
    notes += number_notes

    site = [pin] if pin else []
    boundary, area = parcels.union_feature(site, pos)
    await asyncio.to_thread(_write, inp.run_id, "title_candidates.geojson", parcels.feature_collection(candidates))
    await asyncio.to_thread(
        _write, inp.run_id, "boundary.geojson", boundary or {"type": "FeatureCollection", "features": []}
    )

    search_url = search.search_url(wkt)
    if pin is not None:
        claim = (
            f"The pin is on INSPIRE polygon {pin.inspire_id} ({pin.area_m2 / HA:.2f} ha); "
            f"{len(candidates)} polygons within {radius:,.0f} m. {INDICATIVE}"
        )
        confidence = 0.9 if not truncated else 0.8
    else:
        claim = f"{NO_POLYGON}; {len(candidates)} polygons within {radius:,.0f} m"
        confidence = 0.8 if candidates else 0.6
    search_art = Artifact(
        id=f"title-{inp.run_id[:8]}-search",
        stage="title",
        claim=claim,
        source_url=_url(pin.source_url if pin and pin.source_url else search_url),
        file_path="title_candidates.geojson",
        confidence=confidence,
        model_used=search.SOURCE_NAME,
    )
    return TitleOutput(
        title_number=None,  # the free index has none; a number only ever comes linked to the site, below
        boundary_geojson=boundary,
        area_m2=area,
        pin_parcel=pin,
        candidates=candidates,
        site_parcels=site,
        inspire_ids=[p.inspire_id for p in site],
        title_numbers=numbers,
        search_radius_m=round(radius, 1),
        notes=notes,
        artifacts=[search_art, *number_artifacts],
    )


async def _title_numbers(
    inp: TitleInput, client: httpx.AsyncClient
) -> tuple[list[TitleNumber], list[str], list[Artifact]]:
    """Title numbers for the site from the listing text, then CCOD / OCOD; with notes on what was skipped."""
    numbers: list[TitleNumber] = []
    notes: list[str] = []
    listing_url = inp.request.property_url or inp.request.link
    if listing_url and not inp.request.position:
        text = cached_page_text(str(listing_url))
        if text is None:
            notes.append("Listing page not cached: no title numbers read from it")
        else:
            found = find_title_numbers(text)
            numbers += [
                TitleNumber(
                    title_number=f.title_number, source="listing", source_url=str(listing_url), evidence=f.evidence
                )
                for f in found
            ]
            if not found:
                notes.append("The listing states no title number")

    if settings.hmlr_api_key is None:
        notes.append("HMLR CCOD / OCOD skipped: no HMLR_API_KEY (company-owned titles only)")
    else:
        rows, ownership_notes = await _ownership_rows(inp, client)
        notes += ownership_notes
        seen = {n.title_number for n in numbers}
        for row in rows:
            if row.title_number in seen:
                continue
            seen.add(row.title_number)
            numbers.append(
                TitleNumber(
                    title_number=row.title_number,
                    source=row.dataset,
                    source_url=hmlr.DATASET_URL[row.dataset],
                    proprietor=row.proprietor,
                    tenure=row.tenure,
                    address=row.address,
                    postcode=row.postcode,
                    evidence=f"{hmlr.DATASET_NAME[row.dataset]} {row.file}: {row.address} ({row.postcode})",
                )
            )

    artifacts = [_number_artifact(inp.run_id, i, n, site_postcode=inp.location.postcode) for i, n in enumerate(numbers)]
    return numbers, notes, artifacts


async def _nearby_postcodes(pos: Position, client: httpx.AsyncClient) -> list[str]:
    req = postcodes_io.ReverseGeocodeRequest(lat=pos.lat, lon=pos.lon, radius=NEARBY_POSTCODES_M, limit=20)
    r = await client.get(req.URL, params=req.params())
    r.raise_for_status()
    return [p["postcode"] for p in (r.json().get("result") or [])]


async def _ownership_rows(inp: TitleInput, client: httpx.AsyncClient) -> tuple[list[hmlr.OwnershipRow], list[str]]:
    """CCOD / OCOD rows at the site's postcode or one near the pin, site postcode first, freeholds first."""
    notes: list[str] = []
    missing = [d for d in hmlr.DATASETS if hmlr.indexed_file(d) is None]
    if missing:
        notes.append(
            f"HMLR {', '.join(d.upper() for d in missing)} not downloaded: run `uv run python scripts/hmlr_ownership.py`"
        )
    try:
        nearby = await _nearby_postcodes(inp.location.position, client)
    except httpx.HTTPError:
        nearby = []
        notes.append("Postcodes near the pin unavailable: CCOD / OCOD matched on the site postcode only")
    site_pc = hmlr.norm_postcode(inp.location.postcode)
    postcodes = [inp.location.postcode, *nearby]
    rows = [r for d in hmlr.DATASETS for r in await asyncio.to_thread(hmlr.lookup, d, postcodes)]
    rows.sort(key=lambda r: (hmlr.norm_postcode(r.postcode) != site_pc, (r.tenure or "") != "Freehold", r.title_number))
    if len(rows) > MAX_OWNERSHIP_ROWS:
        notes.append(f"{len(rows) - MAX_OWNERSHIP_ROWS} more company-owned titles at these postcodes not listed")
    return rows[:MAX_OWNERSHIP_ROWS], notes


def _number_artifact(run_id: str, i: int, n: TitleNumber, *, site_postcode: str | None = None) -> Artifact:
    """One artifact per title number; the claim names its source and how it is linked."""
    if n.source == "listing":
        claim = f'Title number {n.title_number}, stated in the listing: "{n.evidence}". Linked to the site, not to a polygon'
        confidence = 0.8
        model = "listing text"
    elif n.source in {"ccod", "ocod"}:
        at_site = bool(site_postcode) and hmlr.norm_postcode(site_postcode) == hmlr.norm_postcode(n.postcode)
        claim = (
            f"Title number {n.title_number} ({n.tenure or 'tenure not stated'}) is owned by {n.proprietor or 'a company'} "
            f"at {n.address}, per {hmlr.DATASET_NAME[n.source]}. A postcode match: a candidate for the site, "
            "not tied to a polygon"
        )
        confidence = 0.45 if at_site else 0.3
        model = hmlr.DATASET_NAME[n.source]
    else:
        claim = f"Title number {n.title_number} for INSPIRE polygon {n.inspire_id}: entered by the user, not checked"
        confidence = 0.5
        model = "user"
    return Artifact(
        id=f"title-{run_id[:8]}-number-{i}-{n.title_number}",
        stage="title",
        claim=claim,
        source_url=_url(n.source_url),
        file_path=None if n.source_url else "site_parcels.geojson",
        confidence=confidence,
        model_used=model,
    )


# ------------------------------------------ after HITL ------------------------------------------- #


def _footprint(inp: TitleSiteInput) -> BaseGeometry | None:
    shape = parcels.footprint_shape(inp.footprint_geojson)
    if shape is None and inp.capacity_mw > 0:
        shape = parcels.footprint_shape(footprint_polygon(inp.position, footprint_acres(inp.capacity_mw)))
    return shape


async def _fetch_more(
    inp: TitleSiteInput, footprint: BaseGeometry | None, known: set[str], client: httpx.AsyncClient
) -> tuple[list[TitleParcel], list[str]]:
    """Polygons outside the candidates: under a footprint that left the search circle, and ids added by the user."""
    more: list[TitleParcel] = []
    notes: list[str] = []
    radius = inp.title.search_radius_m or parcels.MIN_RADIUS_M
    if footprint is not None and parcels.outside_search(footprint, inp.origin, radius):
        response, _ = await search.search_titles(str(footprint.buffer(0.0002).envelope.wkt), client=client)
        more += [p for p in parcels.parcels_from_features(response, inp.position) if p.inspire_id not in known]
    wanted = [i for i in inp.added_ids if i not in known and i not in {p.inspire_id for p in more}]
    if wanted:
        req = planning_data.EntitySearchRequest(dataset=["title-boundary"], reference=wanted, limit=len(wanted))
        r = await client.get(req.GEOJSON_URL, params=req.params())
        r.raise_for_status()
        got = parcels.parcels_from_features(planning_data.EntityGeoJsonResponse.model_validate(r.json()), inp.position)
        more += got
        lost = sorted(set(wanted) - {p.inspire_id for p in got})
        if lost:
            notes.append(f"Added polygons not found in the title-boundary dataset: {', '.join(lost)}")
    return more, notes


async def confirm_title_site(inp: TitleSiteInput, *, client: httpx.AsyncClient | None = None) -> TitleOutput:
    """The confirmed site: the polygons under the footprint (or the user's clicks), shares, union and artifacts."""
    if client is None:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "bessible"}, follow_redirects=True) as own:
            return await confirm_title_site(inp, client=own)
    before = inp.title
    footprint = _footprint(inp)
    known = {p.inspire_id for p in before.candidates}
    more, notes = await _fetch_more(inp, footprint, known, client)
    pool = [*before.candidates, *more]
    title_ids = None if inp.title_ids is None else [*inp.title_ids, *inp.added_ids]
    site = parcels.select_site(
        pool, footprint=footprint, origin=inp.position, title_ids=title_ids, pin=before.pin_parcel
    )
    site = [_with_user_number(p, inp.user_title_numbers.get(p.inspire_id)) for p in site]

    user_numbers = [
        TitleNumber(title_number=p.title_number, source="user", link="polygon", inspire_id=p.inspire_id)
        for p in site
        if p.title_number and p.title_source == "user"
    ]
    numbers = [*before.title_numbers, *user_numbers]
    pin = next((p for p in site if before.pin_parcel and p.inspire_id == before.pin_parcel.inspire_id), None)

    boundary, area = parcels.union_feature(site, inp.position)
    if footprint is not None:
        off = parcels.uncovered_pct(footprint, pool, inp.position)
        if off >= parcels.MIN_SHARE_PCT:
            notes.append(
                f"{off:.1f}% of the BESS footprint is on no INSPIRE polygon (road, river or unregistered land)"
            )
    if not site:
        notes.append("The confirmed site covers no INSPIRE polygon")

    await asyncio.to_thread(_write, inp.run_id, "site_parcels.geojson", parcels.feature_collection(site))
    await asyncio.to_thread(
        _write, inp.run_id, "boundary.geojson", boundary or {"type": "FeatureCollection", "features": []}
    )

    site_numbers = [n for n in before.title_numbers if n.link == "site"]
    artifacts = [_parcel_artifact(inp.run_id, p, site_numbers) for p in site]
    artifacts += [_number_artifact(inp.run_id, len(before.title_numbers) + i, n) for i, n in enumerate(user_numbers)]
    artifacts.append(_edit_artifact(inp, before, site, footprint, area))
    return TitleOutput(
        title_number=pin.title_number if pin else None,
        boundary_geojson=boundary,
        area_m2=area,
        pin_parcel=before.pin_parcel,
        candidates=[],  # kept in out/<run>/title_candidates.geojson; later stages need only the site
        site_parcels=site,
        inspire_ids=[p.inspire_id for p in site],
        title_numbers=numbers,
        search_radius_m=before.search_radius_m,
        notes=[*before.notes, *notes],
        artifacts=artifacts,
    )


def _with_user_number(p: TitleParcel, number: str | None) -> TitleParcel:
    if not number:
        return p
    return p.model_copy(
        update={"title_number": number, "title_source": "user", "title_source_url": None, "title_link": "polygon"}
    )


def _parcel_artifact(run_id: str, p: TitleParcel, site_numbers: Sequence[TitleNumber]) -> Artifact:
    share = f"{p.footprint_overlap_pct:.1f}% of the BESS footprint" if p.footprint_overlap_pct is not None else "chosen"
    if p.title_number:
        number = f"title number {p.title_number} ({p.title_source})"
    elif site_numbers:
        listed = ", ".join(f"{n.title_number} ({n.source})" for n in site_numbers[:4])
        number = f"title number not known for this polygon (site-level: {listed})"
    else:
        number = "title number not known"
    return Artifact(
        id=f"title-{run_id[:8]}-parcel-{p.inspire_id}",
        stage="title",
        claim=f"INSPIRE polygon {p.inspire_id}, {p.area_m2 / HA:.2f} ha, {share}; {number}. {INDICATIVE}",
        source_url=_url(p.source_url or INSPIRE_GUIDANCE),
        file_path="site_parcels.geojson",
        confidence=0.9 if (p.footprint_overlap_pct or 0) >= 10 else 0.7,  # a sliver may be an indicative-edge artefact
        model_used=search.SOURCE_NAME,
    )


def _edit_artifact(
    inp: TitleSiteInput, before: TitleOutput, site: Sequence[TitleParcel], footprint: BaseGeometry | None, area: float
) -> Artifact:
    proposed = set(before.inspire_ids)
    chosen = {p.inspire_id for p in site}
    added, removed = sorted(chosen - proposed), sorted(proposed - chosen)
    how = "clicked polygons" if inp.title_ids is not None else "the polygons under the footprint"
    foot = ""
    if footprint is not None:
        foot_m2 = parcels.shape_area_m2(footprint, inp.position)
        foot = f"BESS footprint {foot_m2 / M2_PER_ACRE:.1f} acres ({foot_m2 / HA:.2f} ha); "
    claim = (
        f"User confirmed the site as {how}: {len(site)} polygon(s), added {len(added)}"
        f"{' (' + ', '.join(added[:6]) + ')' if added else ''}, removed {len(removed)}"
        f"{' (' + ', '.join(removed[:6]) + ')' if removed else ''}; {foot}"
        f"site {area / HA:.2f} ha ({area / M2_PER_ACRE:.1f} acres)"
    )
    return Artifact(
        id=f"title-{inp.run_id[:8]}-edit",
        stage="title",
        claim=claim,
        file_path="site_parcels.geojson",
        confidence=1.0 if site else 0.5,
        model_used="user",
    )
