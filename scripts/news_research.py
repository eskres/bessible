"""Run the local news research on a live location, and optionally record its Tavily responses.

    uv run python scripts/news_research.py <lat> <lon> [--model] [--record] [--fixture NAME]

--model     select paragraphs and label them with your Gemini key (GOOGLE_API_KEY), as a run would
--record    copy this run's Tavily responses (searches and extract) to data/recorded/tavily/ (serves the offline demo)
--fixture   save the first query's raw response as tests/api/fixtures/tavily_search_<NAME>.json, and the extract
            response (if any) as tavily_extract_<NAME>.json

Needs TAVILY_API_KEY in .env. Prints the queries, credits, and every source URL with its quotes and labels.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bessible.config import settings
from bessible.location import Coordinates, locality
from bessible.suitability import research as news
from bessible.suitability.sentiment import process_sentiment

FIXTURES = settings.data_dir.parent / "tests" / "api" / "fixtures"


async def main() -> None:
    """Search, print what was kept, and copy the dated responses where asked."""
    p = argparse.ArgumentParser()
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--model", action="store_true")
    p.add_argument("--record", action="store_true")
    p.add_argument("--fixture")
    args = p.parse_args()

    model = None
    if args.model:
        from bessible.llm import developer_model  # ruff: ignore[import-outside-top-level] - only with --model

        model = developer_model()
    location = await locality(Coordinates(lat=args.lat, lon=args.lon))
    research = await news.research_local_news(location, model=model)
    out = await process_sentiment("script", research, model=model)

    print(f"queries ({len(research.queries)}):", *research.queries, sep="\n  ")
    print(f"status={research.status} results={research.results} pages_read={research.pages_read}")
    print(f"credits={research.credits:g} cached={research.cached} selected_by={research.selected_by}")
    print(
        f"dropped (not verbatim)={research.dropped} extracted={research.extracted} unavailable={research.unavailable}"
    )
    for a in out.artifacts:
        print(f"\n[{a.id}] {a.source_url}\n  {a.claim}\n  model={a.model_used} confidence={a.confidence}")
    for s in research.sources:
        print(f"\n{s.url}  ({s.title}, {s.published})")
        for q in s.paragraphs:
            print(f"  > {q}")

    _save(research, record=args.record, fixture=args.fixture)


def _save(research: news.Research, *, record: bool, fixture: str | None) -> None:
    """Copy this run's cached responses to the recordings and the test fixtures."""
    cached = [settings.cache_dir / "tavily" / f"{key}.json" for key in research.responses]  # searches, then extract
    if record:
        news.RECORDED_DIR.mkdir(parents=True, exist_ok=True)
        for path in cached:
            if path.exists():
                entry = json.loads(path.read_text(encoding="utf-8"))
                entry["recorded"] = True
                (news.RECORDED_DIR / path.name).write_text(json.dumps(entry, indent=1, ensure_ascii=False))
                print(f"recorded {path.name} (fetched {entry['fetched_on']})")
    if not fixture:
        return
    if not cached or not cached[0].exists():
        sys.exit("no cached response to save as a fixture")
    fixtures = [("search", cached[0])]
    if len(cached) > len(research.queries):  # the extract answer comes last
        fixtures.append(("extract", cached[-1]))
    for kind, path in fixtures:
        target = FIXTURES / f"tavily_{kind}_{fixture}.json"
        entry = json.loads(path.read_text(encoding="utf-8"))
        target.write_text(json.dumps(entry["response"], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"fixture {target} (fetched {entry['fetched_on']})")


if __name__ == "__main__":
    asyncio.run(main())
