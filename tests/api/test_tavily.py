from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from bessible.api import tavily
from bessible.suitability.research import search_request

FIX = Path(__file__).parent / "fixtures"
DORKING = FIX / "tavily_search_dorking.json"  # recorded with scripts/news_research.py --fixture dorking


@pytest.mark.skipif(
    not DORKING.exists(), reason="record it: uv run python scripts/news_research.py 51.2329 -0.3315 --fixture dorking"
)
def test_search_response_parses_a_recorded_response():
    r = tavily.SearchResponse.model_validate(json.loads(DORKING.read_text()))
    assert r.query
    assert r.results
    assert all(res.url.startswith("http") for res in r.results)
    assert any(res.raw_content for res in r.results)  # include_raw_content="text"
    assert r.usage is not None
    assert r.usage.credits >= 1


def test_request_body_is_what_the_stage_sends():
    body = search_request("Dorking battery storage BESS planning application", date(2026, 9, 27)).params()
    assert body == {
        "query": "Dorking battery storage BESS planning application",
        "search_depth": "basic",
        "topic": "general",
        "country": "united kingdom",
        "start_date": "2023-01-01",
        "max_results": 5,
        "include_raw_content": "text",
        "include_published_date": True,
        "exclude_domains": [
            "facebook.com",
            "linkedin.com",
            "x.com",
            "twitter.com",
            "instagram.com",
            "tiktok.com",
            "youtube.com",
        ],
        "include_answer": False,
        "include_images": False,
        "auto_parameters": False,
        "include_usage": True,
    }


def test_unknown_request_field_is_our_bug():
    with pytest.raises(ValidationError):
        tavily.SearchRequest(query="x", days=3)  # type: ignore[call-arg]


def test_error_body():
    assert tavily.ErrorResponse.model_validate({"detail": {"error": "Unauthorized: missing or invalid API key."}})
    assert tavily.auth_headers("tvly-x") == {"Authorization": "Bearer tvly-x"}
