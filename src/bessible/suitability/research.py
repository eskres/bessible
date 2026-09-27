"""News research agent: searches local UK planning and energy news with Gemini."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.capabilities import WebSearch
from pydantic_ai.models import Model

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
NEWS_FIXTURES_DIR = DATA_DIR / "fixtures" / "news"
# Bounds how many searches one news-research call can run, so nothing in a search result can drive
# the agent into an unbounded, billed sequence of further searches (the pydantic-ai default is 50
# requests; a local-news lookup needs a handful).
RESEARCH_USAGE_LIMITS = UsageLimits(request_limit=8, tool_calls_limit=6)


class Source(BaseModel):
    """One news or planning article source."""

    url: HttpUrl
    title: str
    published: date | None = None
    paragraphs: list[str] = Field(default_factory=list)


class Research(BaseModel):
    """Collected local energy infrastructure news for a site."""

    place: str
    lpa: str | None = None
    sources: list[Source] = Field(default_factory=list)
    cached: bool = False
    unavailable: str | None = None  # why no live search ran or answered; None = it did (or the cache did)
    retryable: bool = False  # the search failed, so trying again may work


def _site_key(lat: float, lon: float, postcode: str | None = None) -> str:
    """Generate deterministic file-friendly site key."""
    if postcode:
        clean_pc = re.sub(r"[^A-Za-z0-9]", "", postcode).upper()
        if clean_pc:
            return clean_pc
    return f"{lat:.4f}_{lon:.4f}".replace("-", "m").replace(".", "_")


def _create_research_agent() -> Agent[None, Research]:
    return Agent(
        name="news_researcher",
        defer_model_check=True,  # no key at import time: the run's model is supplied per call
        capabilities=[WebSearch()],
        output_type=Research,
        system_prompt=(
            "You are a local planning and energy infrastructure research assistant for the UK.\n"
            "Your task is to search for recent local news, planning applications, and community reactions "
            "regarding battery storage (BESS), solar farms, substations, or energy infrastructure near the specified place.\n"
            "CRITICAL REQUIREMENTS:\n"
            "1. Only include UK sources relevant to the specific local authority / area.\n"
            "2. For each source, provide the full URL, article title, publication date if available, and 1 to 4 key paragraphs.\n"
            "3. Each paragraph must be concise (under 200 words).\n"
            "4. If no relevant local coverage is found, return an empty sources list."
        ),
    )


research_agent = _create_research_agent()


def load_cached_research(key: str) -> Research | None:
    """Load research from fixture cache if present."""
    NEWS_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = NEWS_FIXTURES_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            res = Research.model_validate(data)
            res.cached = True
            return res
        except Exception:
            return None
    return None


def save_cached_research(key: str, research: Research) -> None:
    """Save research to fixture cache."""
    NEWS_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = NEWS_FIXTURES_DIR / f"{key}.json"
    cache_file.write_text(research.model_dump_json(indent=2), encoding="utf-8")


async def research_local_news(
    place: str,
    lat: float,
    lon: float,
    postcode: str | None = None,
    lpa: str | None = None,
    model: Model | None = None,
) -> Research:
    """Find local energy and planning news, checking cache first. Without a `model` there is no live search."""
    key = _site_key(lat, lon, postcode)

    # 1. Check primary key
    cached = load_cached_research(key)
    if cached is not None:
        return cached

    # 2. Check coordinate key if postcode key wasn't found
    coord_key = f"{lat:.4f}_{lon:.4f}".replace("-", "m").replace(".", "_")
    if coord_key != key:
        cached_coord = load_cached_research(coord_key)
        if cached_coord is not None:
            return cached_coord

    # 3. Live search via Gemini + WebSearch
    lpa_str = f" in {lpa}" if lpa else ""
    prompt = (
        f"Search for local news and planning coverage of battery storage, solar farms, or substations "
        f"near {place}{lpa_str} (coordinates: {lat:.4f}, {lon:.4f}{f', postcode: {postcode}' if postcode else ''})."
    )

    if model is None:
        why, retryable = "No model on this run for the live news search.", False
    else:
        try:
            res = await research_agent.run(prompt, model=model, usage_limits=RESEARCH_USAGE_LIMITS)
            output = res.output
            if isinstance(output, Research):
                save_cached_research(key, output)
                return output
            why = "The news search returned no usable result."
        except Exception as e:  # reported as a retryable gap, not raised
            why = f"The news search failed ({type(e).__name__}: {e})"[:200]
        retryable = True

    # 4. Fallback if search fails / offline: say so, rather than "no coverage"
    return Research(place=place, lpa=lpa, sources=[], cached=False, unavailable=why, retryable=retryable)
