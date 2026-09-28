"""Local sentiment analysis stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bessible import events
from bessible.location import Coordinates, locality
from bessible.models import NodeInput, SentimentOutput
from bessible.suitability.research import research_local_news
from bessible.suitability.sentiment import process_sentiment

if TYPE_CHECKING:
    from pydantic_ai.models import Model


async def local_sentiment(
    inp: NodeInput, *, model: Model | None = None, tavily_key: str | None = None
) -> SentimentOutput:
    """Assess local community sentiment from local news and planning coverage.

    Runs beside `site_land`, so it looks up only the site's place names and council (`location.locality`),
    not the whole `LocationData`.
    """
    location = await locality(Coordinates(lat=inp.site.position.lat, lon=inp.site.position.lon))
    where = location.deterministic.locality
    place = where.place or where.district or "the site area"
    council = f" ({where.planning_authority})" if where.planning_authority else ""
    events.emit(inp.run_id, "sentiment", f"Searching local news and planning coverage for {place}{council}")

    research = await research_local_news(location, model=model, tavily_key=tavily_key)

    events.emit(inp.run_id, "sentiment", f"Kept {len(research.sources)} sources; analyzing planning sentiment")
    return await process_sentiment(inp.run_id, research, model=model)
