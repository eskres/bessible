"""Location resolution stage: map pin, postcode or link extraction to coordinates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.api.postcodes_io import ReverseGeocodeRequest
from bessible.geocode import format_postcode, geocode_postcode, nearest_postcode
from bessible.location.extract import LocationNotFound, resolve_from_link
from bessible.models import Artifact, LocationInput, LocationOutput, Position

POSTCODES_IO_URL = "https://api.postcodes.io/postcodes/"
if TYPE_CHECKING:
    from pydantic_ai.models import Model

QUALITY_CONFIDENCE_LIMIT = 4  # postcodes.io positional quality 1-4 is a unit-postcode centroid or better


async def resolve_location(
    inp: LocationInput, *, model: Model | None = None, tavily_key: str | None = None
) -> LocationOutput:
    """Resolve a map pin, postcode or property link to coordinates and canonical postcode.

    A map pin is the exact site: it wins, and the nearest postcode only labels it.
    Else if a postcode is provided in the request, it wins and no page is fetched.
    Otherwise, the property link is fetched (through Tavily on `tavily_key` if the portal blocks us) and the run's
    `model` extracts location details.

    Raises:
        LocationNotFound: if extraction fails, no address exists, non-UK, or postcode invalid.
    """
    if inp.request.position:
        return await _resolve_pin(inp.request.position, inp.run_id)

    if inp.request.postcode:
        result = await geocode_postcode(inp.request.postcode)
        postcode = format_postcode(result.postcode)
        assert result.latitude is not None and result.longitude is not None
        pos = Position(lat=result.latitude, lon=result.longitude)

        area = result.admin_district or result.country
        claim = f"Postcode {postcode} geocodes to ({pos.lat:.4f}, {pos.lon:.4f}) in {area}"
        art = Artifact(
            id=f"location-{inp.run_id[:8]}",
            stage="location",
            claim=claim,
            source_url=HttpUrl(f"{POSTCODES_IO_URL}{result.postcode.replace(' ', '')}"),
            confidence=0.95 if result.quality <= QUALITY_CONFIDENCE_LIMIT else 0.6,
            model_used="postcodes.io",
        )
        return LocationOutput(postcode=postcode, position=pos, artifacts=[art])

    target_url = inp.request.property_url or inp.request.link
    if target_url is None:
        msg = "No property link or postcode was provided. Please pass --postcode."
        raise LocationNotFound(msg)

    return await resolve_from_link(str(target_url), inp.run_id, model=model, tavily_key=tavily_key)


async def _resolve_pin(pos: Position, run_id: str) -> LocationOutput:
    """Keep the pin where the user put it; label it with the nearest postcode, which also proves it is in the UK."""
    nearest = await nearest_postcode(pos.lat, pos.lon)
    if nearest is None:
        msg = f"The pin at ({pos.lat:.4f}, {pos.lon:.4f}) is not near a UK postcode. Only UK sites are supported."
        raise LocationNotFound(msg)

    postcode = format_postcode(nearest.postcode)
    area = nearest.admin_district or nearest.country
    away = f", {nearest.distance:,.0f} m away" if nearest.distance is not None else ""
    req = ReverseGeocodeRequest(lat=pos.lat, lon=pos.lon, limit=1, widesearch=True)
    art = Artifact(
        id=f"location-{run_id[:8]}",
        stage="location",
        claim=f"Map pin at ({pos.lat:.4f}, {pos.lon:.4f}) in {area}; nearest postcode {postcode}{away}",
        source_url=HttpUrl(f"{req.URL}?lon={pos.lon}&lat={pos.lat}&limit=1&widesearch=true"),
        confidence=0.95,
        model_used="postcodes.io",
    )
    return LocationOutput(postcode=postcode, position=pos, artifacts=[art])
