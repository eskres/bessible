"""Revenue sources: a protocol, a fixture loader, and a live-with-fallback wrapper."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, HttpUrl

from bessible.assumptions import FIXTURES_DIR
from bessible.market import load_market_assumptions
from bessible.market.live import ElexonArbitrageSource, NesoResponseSource
from bessible.models import StreamValue

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

MARKET_FIXTURES = FIXTURES_DIR / "market"


class RevenueSource(Protocol):
    """Any source, public or paid, implements one method."""

    name: str

    async def fetch(self, duration_h: int) -> StreamValue:
        """Return this stream's revenue for one duration."""
        ...


class Fixture(BaseModel):
    """A committed fallback for one stream."""

    stream: str
    source: str
    source_url: HttpUrl
    as_of: date
    status: str = "agreed"
    note: str = ""
    period: str | None = None  # data period the figure covers
    method: str | None = None
    quote: str | None = None  # verbatim line or table reference from the source
    derivation: str | None = None  # the arithmetic, if any
    cross_check: str | None = None  # an outside figure to compare against, never used in the stack
    gbp_per_mw_year_by_duration: dict[str, float]
    upper_bound_by_duration: dict[str, float] | None = None
    mw_share: float | None = None


class FixtureSource:
    """Serves a committed fixture. Always flagged cached."""

    def __init__(self, name: str, path: Path | None = None) -> None:
        """Initialize the fixture source with a stream name and optional path."""
        self.name = name
        self.path = path or MARKET_FIXTURES / f"{name}.json"

    async def fetch(self, duration_h: int) -> StreamValue:
        """Return the fixture value for one duration, flagged cached."""
        fixture = Fixture.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        return StreamValue(
            stream=fixture.stream,
            gbp_per_mw_year=fixture.gbp_per_mw_year_by_duration[str(duration_h)],
            source=fixture.source,
            source_url=fixture.source_url,
            as_of=fixture.as_of,
            cached=True,
            placeholder=fixture.status == "placeholder",
            method=fixture.method,
            period=fixture.period,
            upper_bound_gbp_per_mw_year=(fixture.upper_bound_by_duration or {}).get(str(duration_h)),
            mw_share=fixture.mw_share,
        )


class FallbackSource:
    """Try a live source. On any error, use the fixture. The run never fails on one source."""

    def __init__(self, live: RevenueSource, fixture: FixtureSource) -> None:
        """Wrap a live source with a fixture fallback."""
        self.name = fixture.name
        self.live = live
        self.fixture = fixture

    async def fetch(self, duration_h: int) -> StreamValue:
        """Return the live value, or the cached fixture if the live source fails."""
        try:
            return await self.live.fetch(duration_h)
        except Exception:
            logger.warning("Live source %s failed; using cached fixture", self.live.name, exc_info=True)
            return await self.fixture.fetch(duration_h)


STREAM_NAMES = ("capacity_market", "balancing_ancillary", "wholesale")
LiveFetch = Callable[[int], Awaitable[StreamValue]]


def default_sources() -> list[RevenueSource]:
    """The demo sources: Capacity Market from its committed auction results; wholesale and ancillary live.

    The live sources fall back to their fixtures, which `python -m bessible.market.refresh` rewrites from a live run.
    """
    a = load_market_assumptions()
    return [
        FixtureSource("capacity_market"),
        FallbackSource(NesoResponseSource(), FixtureSource("balancing_ancillary")),
        FallbackSource(ElexonArbitrageSource(a), FixtureSource("wholesale")),
    ]
