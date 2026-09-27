"""Tavily Search API — web search built for agents, with the page text of each result.

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search
Credits: https://docs.tavily.com/documentation/api-credits

Notes:
- ``POST /search`` with a JSON body; auth is the header ``Authorization: Bearer tvly-...`` (see ``auth_headers``).
- ``include_raw_content`` returns the cleaned page text per result (``"text"`` or ``"markdown"``); ``content``
  is only a short, query-dependent snippet.
- ``country`` boosts results from that country and works only with ``topic="general"``.
- ``search_depth="basic"`` costs 1 credit and ``"advanced"`` 2; ``include_usage`` reports the credits used.
- ``POST /extract`` fetches the text of given URLs (up to 20): 1 credit per 5 successful URLs at basic depth,
  billed in blocks (a single failed URL was billed 1 credit). Unreachable URLs come back in ``failed_results``.
- Errors are non-2xx with ``{"detail": {"error": "..."}}`` -> ``ErrorResponse``: 400 bad request, 401 bad key,
  429 rate limit, 432 / 433 plan or pay-as-you-go limit.
- ``published_date`` comes back only with ``include_published_date``, as an RFC 2822 string
  (``"Sat, 31 May 2025 00:00:00 GMT"``): Tavily's estimate of publication or last update.
- ``start_date`` / ``end_date`` only rank by date; ``filter_by_published_date`` removes results outside the window
  (and results with no detectable date).
- The fields match docs.tavily.com (checked 2026-09-27) and a live response (``tests/api/fixtures``).
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
    time_range: Literal["day", "week", "month", "year", "d", "w", "m", "y"] | None = None
    start_date: str | None = None  # YYYY-MM-DD, results published on or after
    end_date: str | None = None
    include_published_date: bool | None = None  # adds `published_date` to each result
    filter_by_published_date: bool | None = None  # drop results outside the date window, or with no date
    max_results: int | None = Field(default=None, ge=0, le=20)  # server default 5
    chunks_per_source: int | None = Field(default=None, ge=1, le=3)  # advanced depth only
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    include_domains_mode: Literal["restrict", "prefer"] | None = None
    include_answer: bool | Literal["basic", "advanced"] | None = None
    include_raw_content: bool | Literal["markdown", "text"] | None = None  # True = "markdown"
    include_images: bool | None = None
    include_image_descriptions: bool | None = None
    include_favicon: bool | None = None
    country: str | None = None  # full lowercase name, e.g. "united kingdom"; boosts only; topic "general" only
    language: str | None = None  # ISO 639-1 code or English name; boosts unless filter_by_language
    filter_by_language: bool | None = None
    safe_search: bool | None = None
    auto_parameters: bool | None = None  # let Tavily pick topic / depth / time range (can cost 2 credits)
    exact_match: bool | None = None  # only results containing the quoted phrases in `query`
    include_usage: bool | None = None


class ExtractRequest(ApiRequest):
    """POST /extract — the JSON body. ``params()`` gives the body to send."""

    URL: ClassVar[str] = f"{BASE_URL}/extract"
    METHOD: ClassVar[str] = "POST"

    urls: list[str] = Field(min_length=1, max_length=20)
    query: str | None = None  # reranks chunks by relevance
    chunks_per_source: int | None = Field(default=None, ge=1, le=5)  # with `query`
    extract_depth: Literal["basic", "advanced"] | None = None  # server default "basic"
    format: Literal["markdown", "text"] | None = None  # server default "markdown"
    include_images: bool | None = None
    include_favicon: bool | None = None
    timeout: float | None = Field(default=None, ge=1, le=60)  # seconds
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


class ExtractResponse(ApiResponse):
    """200 from POST /extract (also when every URL failed)."""

    results: list[ExtractResult] = Field(default_factory=list)
    failed_results: list[FailedResult] = Field(default_factory=list)
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
    published_date: str | None = None  # RFC 2822, with include_published_date
    id: str | None = None  # unique result identifier, e.g. "1486a5-00"
    favicon: str | None = None
    images: list[Image | str] | None = None


class ExtractResult(ApiResponse):
    """One fetched page."""

    url: str
    title: str | None = None  # returned, though not in the docs
    raw_content: str
    images: list[Image | str] = Field(default_factory=list)
    favicon: str | None = None


class FailedResult(ApiResponse):
    """A URL that could not be fetched, e.g. ``"Failed to fetch url"``, ``"Request timed out"``."""

    url: str
    error: str


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
ExtractResponse.model_rebuild()
ErrorResponse.model_rebuild()
