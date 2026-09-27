"""Tavily Search API — web search built for agents, with the page text of each result.

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search
Credits: https://docs.tavily.com/documentation/api-credits

Notes:
- ``POST /search`` with a JSON body; auth is the header ``Authorization: Bearer tvly-...`` (see ``auth_headers``).
- ``include_raw_content`` returns the cleaned page text per result (``"text"`` or ``"markdown"``); ``content``
  is only a short, query-dependent snippet.
- ``country`` boosts results from that country and works only with ``topic="general"``.
- ``search_depth="basic"`` costs 1 credit and ``"advanced"`` 2; ``include_usage`` reports the credits used.
- Errors are non-2xx with ``{"detail": {"error": "..."}}`` -> ``ErrorResponse``: 400 bad request, 401 bad key,
  429 rate limit, 432 / 433 plan or pay-as-you-go limit.
- The request fields match tavily-python 0.8.4 (``AsyncTavilyClient._search``) and @tavily/core 0.7.13. The
  response fields match the @tavily/core types.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .base import ApiRequest, ApiResponse

BASE_URL = "https://api.tavily.com"


def auth_headers(api_key: str) -> dict[str, str]:
    """Request headers carrying the API key."""
    return {"Authorization": f"Bearer {api_key}"}


# ------------------------------------------ 1. Request ------------------------------------------ #


class SearchRequest(ApiRequest):
    """POST /search — the JSON body. ``params()`` gives the body to send."""

    URL: ClassVar[str] = f"{BASE_URL}/search"
    METHOD: ClassVar[str] = "POST"

    query: str = Field(max_length=400)
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] | None = None  # server default "basic"
    topic: Literal["general", "news", "finance"] | None = None  # server default "general"
    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: str | None = None  # YYYY-MM-DD, results published on or after
    end_date: str | None = None
    max_results: int | None = Field(default=None, ge=0, le=20)  # server default 5
    chunks_per_source: int | None = Field(default=None, ge=1, le=3)  # advanced depth only
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    include_answer: bool | Literal["basic", "advanced"] | None = None
    include_raw_content: bool | Literal["markdown", "text"] | None = None  # True = "markdown"
    include_images: bool | None = None
    include_favicon: bool | None = None
    country: str | None = None  # full lowercase name, e.g. "united kingdom"; topic "general" only
    auto_parameters: bool | None = None  # let Tavily pick topic / depth / time range (can cost 2 credits)
    exact_match: bool | None = None  # only results containing the quoted phrases in `query`
    include_usage: bool | None = None


# ----------------------------------------- 2. Response ------------------------------------------ #


class SearchResponse(ApiResponse):
    """200 from POST /search."""

    query: str
    answer: str | None = None
    follow_up_questions: list[str] | None = None
    images: list[Image | str] = Field(default_factory=list)
    results: list[SearchResult] = Field(default_factory=list)
    auto_parameters: dict[str, object] | None = None
    response_time: float | None = None  # seconds
    usage: Usage | None = None  # with include_usage
    request_id: str | None = None


class ErrorResponse(ApiResponse):
    """Non-2xx body: ``{"detail": {"error": "..."}}``."""

    detail: ErrorDetail


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class SearchResult(ApiResponse):
    """One ranked web result."""

    title: str
    url: str
    content: str  # short snippet most relevant to the query
    score: float  # relevance to the query, 0-1
    raw_content: str | None = None  # cleaned page text, with include_raw_content; None when it could not be fetched
    published_date: str | None = None  # topic "news" only
    favicon: str | None = None
    images: list[Image | str] | None = None


class Image(ApiResponse):
    """An image; plain URLs unless include_image_descriptions."""

    url: str
    description: str | None = None


class Usage(ApiResponse):
    """Credits this request used."""

    credits: float


class ErrorDetail(ApiResponse):
    """The error message."""

    error: str


SearchResponse.model_rebuild()
ErrorResponse.model_rebuild()
