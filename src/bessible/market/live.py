"""Live revenue sources: wholesale arbitrage from Elexon prices, ancillary response from NESO auction results.

Each source computes a 12-month figure for 2, 4 and 8 hours from free public APIs, caches it per day under
`out/market_cache/` (so a demo run does not download a year of prices each time), and returns `StreamValue`s
with `cached=False`. Calls have a timeout and no retries: Temporal retries the activity, and `FallbackSource`
serves the committed fixture if a call fails.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, HttpUrl

from bessible.api import ckan, elexon, neso
from bessible.assumptions import DATA_DIR
from bessible.models import REQUIRED_DURATION_HOURS, StreamValue

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from bessible.assumptions import AssumptionSet

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[3] / "out" / "market_cache"
TIMEOUT_S = 30.0
HOURS_PER_YEAR = 8760
WINDOW_DAYS = 365
MIN_PERIODS_PER_DAY = 44  # a full day has 48 (46 / 50 on clock-change days); fewer means missing prices
REPD_DIR = DATA_DIR / "repd"


class LiveFigure(BaseModel):
    """A computed stream, as cached and as written into the committed fixture."""

    stream: str
    source: str
    source_url: HttpUrl
    as_of: date
    period: str
    method: str
    derivation: str
    gbp_per_mw_year_by_duration: dict[str, float]


def _cached(name: str, today: date) -> LiveFigure | None:
    """Today's cached figure for a stream, or None."""
    path = CACHE_DIR / f"{name}_{today.isoformat()}.json"
    if path.exists():
        return LiveFigure.model_validate_json(path.read_text(encoding="utf-8"))
    return None


def _store(name: str, today: date, figure: LiveFigure) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}_{today.isoformat()}.json").write_text(figure.model_dump_json(indent=2), encoding="utf-8")


async def _once(owner: object, compute: Callable[[], Awaitable[LiveFigure]]) -> LiveFigure:
    """Run `compute` once per `owner`; later calls get the same figure or re-raise the same error."""
    done: LiveFigure | Exception | None = getattr(owner, "_outcome", None)
    if done is None:
        try:
            done = await compute()
        except Exception as e:  # ruff: ignore[blind-except] - remembered, then re-raised below
            done = e
        owner._outcome = done  # type: ignore[attr-defined]  # ruff: ignore[private-member-access]
    if isinstance(done, Exception):
        raise done
    return done


def _to_stream(figure: LiveFigure, duration_h: int) -> StreamValue:
    return StreamValue(
        stream=figure.stream,
        gbp_per_mw_year=figure.gbp_per_mw_year_by_duration[str(duration_h)],
        source=figure.source,
        source_url=figure.source_url,
        as_of=figure.as_of,
        cached=False,
        method=figure.method,
        period=figure.period,
    )


# ------------------------------------------ Wholesale ------------------------------------------- #


def daily_spread_revenue(prices: list[float], duration_h: int, rte: float) -> float:
    """One day's arbitrage in GBP per MW: (mean of top `d` hours - mean of bottom `d` hours) x d MWh x RTE.

    `prices` are the day's half-hourly prices in GBP/MWh, so `d` hours are `2d` half hours.
    """
    n = 2 * duration_h
    ranked = sorted(prices)
    return (fmean(ranked[-n:]) - fmean(ranked[:n])) * duration_h * rte


def arbitrage_by_duration(
    days: dict[date, list[float]], rte: float, durations: Iterable[int] = REQUIRED_DURATION_HOURS
) -> dict[int, float]:
    """Sum of `daily_spread_revenue` over the days, per duration, in GBP per MW."""
    return {d: sum(daily_spread_revenue(p, d, rte) for p in days.values()) for d in durations}


def prices_by_day(records: list[elexon.MarketIndexRecord], start: date, end: date) -> dict[date, list[float]]:
    """APX half-hourly prices per settlement day in [start, end]; periods with no volume and short days dropped."""
    days: dict[date, list[float]] = defaultdict(list)
    for r in records:
        if r.data_provider == elexon.APX and r.volume > 0 and start <= r.settlement_date <= end:
            days[r.settlement_date].append(r.price)
    return {d: p for d, p in sorted(days.items()) if len(p) >= MIN_PERIODS_PER_DAY}


class ElexonArbitrageSource:
    """Wholesale arbitrage upper bound from the last 365 days of GB market index prices (Elexon MID, APX)."""

    name = "elexon_mid_arbitrage"

    def __init__(self, a: AssumptionSet, today: date | None = None) -> None:
        """`a` is the market assumptions (round-trip efficiency); `today` is for tests."""
        self.rte = a.number("round_trip_efficiency")
        self.today = today

    async def figure(self) -> LiveFigure:
        """Today's figure, from the cache or from one Elexon call."""
        today = self.today or datetime.now(UTC).date()
        if hit := _cached("wholesale", today):
            return hit
        end = today - timedelta(days=1)
        start = end - timedelta(days=WINDOW_DAYS - 1)
        req = elexon.MarketIndexStreamRequest(
            from_=start, to=end, settlement_period_from=1, settlement_period_to=50, data_providers=[elexon.APX]
        )
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": "bessible"}) as client:
            r = await client.get(req.URL, params=req.params())
            r.raise_for_status()
        records = elexon.MarketIndexStreamResponse.model_validate(r.json()).root
        days = prices_by_day(records, start, end)
        if len(days) < WINDOW_DAYS - 30:
            msg = f"Elexon MID returned only {len(days)} complete days for {start}..{end}"
            raise ValueError(msg)
        by_d = arbitrage_by_duration(days, self.rte)
        url = str(httpx.URL(req.URL, params=req.params()))
        figure = LiveFigure(
            stream="wholesale",
            source="Elexon Insights, Market Index Data (APX), half-hourly GB prices",
            source_url=HttpUrl(url),
            as_of=today,
            period=f"{min(days)} to {max(days)} ({len(days)} complete settlement days)",
            method=(
                "upper bound: perfect foresight, one full cycle per day; per day (mean of the top d hours' price - "
                f"mean of the bottom d hours' price) x d MWh per MW x round-trip efficiency {self.rte:g}, summed "
                "over the days; charge and discharge hours are not forced into order"
            ),
            derivation=(
                f"{len(records)} APX rows from {url}; {len(days)} days with >= {MIN_PERIODS_PER_DAY} priced "
                "half hours kept; " + ", ".join(f"{d}h: GBP {v:,.0f}/MW/yr" for d, v in by_d.items())
            ),
            gbp_per_mw_year_by_duration={str(d): round(v) for d, v in by_d.items()},
        )
        _store("wholesale", today, figure)
        return figure

    async def fetch(self, duration_h: int) -> StreamValue:
        """Wholesale arbitrage for one duration. One live attempt per instance, so a failure is not retried."""
        return _to_stream(await _once(self, self.figure), duration_h)


# ------------------------------------ Balancing and ancillary ----------------------------------- #


def operational_battery_mw() -> tuple[float, int, int, str]:
    """GB operational battery capacity from the committed REPD snapshot: (MW, records with MW, records, CSV URL)."""
    manifest = json.loads((REPD_DIR / "manifest.json").read_text(encoding="utf-8"))
    rows = json.loads((REPD_DIR / manifest["file"]).read_text(encoding="utf-8"))["results"]
    operational = [r for r in rows if r.get("status") == "Operational"]
    with_mw = [r["mw"] for r in operational if r.get("mw")]
    return float(sum(with_mw)), len(with_mw), len(operational), str(manifest["csv_url"])


class ResponseValue(BaseModel):
    """The pieces of the ancillary figure, kept for the derivation text and tests."""

    pair_price: dict[str, float]  # family (DC/DM/DR) -> mean Low + mean High clearing price, GBP/MW/h
    pair_volume: dict[str, float]  # family -> mean cleared MW, averaged over the Low and High sides
    value_gbp_per_mw_h: float  # volume-weighted mean of pair_price
    share: float  # cleared MW / fleet MW, capped at 1


def response_value(rows: list[neso.ResponseProductSummary], fleet_mw: float) -> ResponseValue:
    """Combine per-product totals (possibly from several resources) into the ancillary value and share."""
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # windows, price sum, volume sum
    for r in rows:
        t = totals[r.auction_product]
        t[0] += r.windows
        t[1] += r.price_sum
        t[2] += r.volume_sum
    missing = [p for p in neso.RESPONSE_PRODUCTS if p not in totals]
    if missing:
        msg = f"NESO results have no rows for {missing}"
        raise ValueError(msg)
    mean_price = {p: t[1] / t[0] for p, t in totals.items()}
    mean_volume = {p: t[2] / t[0] for p, t in totals.items()}
    families = ("DC", "DM", "DR")
    pair_price = {f: mean_price[f + "L"] + mean_price[f + "H"] for f in families}
    pair_volume = {f: (mean_volume[f + "L"] + mean_volume[f + "H"]) / 2 for f in families}
    cleared = sum(pair_volume.values())
    value = sum(pair_price[f] * pair_volume[f] for f in families) / cleared
    return ResponseValue(
        pair_price=pair_price, pair_volume=pair_volume, value_gbp_per_mw_h=value, share=min(1.0, cleared / fleet_mw)
    )


class NesoResponseSource:
    """Dynamic Containment / Moderation / Regulation revenue per MW from 12 months of NESO auction results."""

    name = "neso_response_auctions"

    def __init__(self, today: date | None = None) -> None:
        """`today` is for tests."""
        self.today = today

    async def figure(self) -> LiveFigure:  # ruff: ignore[too-many-locals] - each local is one derivation fact
        """Today's figure, from the cache or from NESO (1 package_show + 1 SQL call per resource, max 2/minute)."""
        today = self.today or datetime.now(UTC).date()
        if hit := _cached("balancing_ancillary", today):
            return hit
        end = today
        start = end - timedelta(days=WINDOW_DAYS)
        rows: list[neso.ResponseProductSummary] = []
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": "bessible"}) as client:
            show = neso.PackageShowRequest(id=neso.RESPONSE_RESERVE_PACKAGE)
            r = await client.get(show.URL, params=show.params())
            r.raise_for_status()
            package = ckan.PackageShowResponse.model_validate(r.json()).result
            if package is None:
                msg = f"NESO package {neso.RESPONSE_RESERVE_PACKAGE} not found"
                raise ValueError(msg)
            resource_ids = neso.results_summary_resources(package, start)
            for rid in resource_ids:
                req = neso.response_product_summary(rid, start, end)
                r = await client.get(req.URL, params=req.params())
                r.raise_for_status()
                result = ckan.DatastoreSearchSqlResponse[neso.ResponseProductSummary].model_validate(r.json()).result
                rows.extend(result.records if result else [])
        fleet_mw, with_mw, operational, repd_url = operational_battery_mw()
        v = response_value(rows, fleet_mw)
        gbp = v.value_gbp_per_mw_h * HOURS_PER_YEAR * v.share
        first = min(r.first_start for r in rows).date()
        last = max(r.last_end for r in rows).date()
        prices = ", ".join(f"{f} {p:.2f}" for f, p in v.pair_price.items())
        volumes = ", ".join(f"{f} {m:,.0f}" for f, m in v.pair_volume.items())
        figure = LiveFigure(
            stream="balancing_ancillary",
            source="NESO EAC auction results (Dynamic Containment, Moderation, Regulation)",
            source_url=HttpUrl(f"https://www.neso.energy/data-portal/{neso.RESPONSE_RESERVE_PACKAGE}"),
            as_of=today,
            period=f"delivery {first} to {last}",
            method=(
                "mean clearing price of each service over the period, Low + High frequency sides stacked for one "
                "symmetric MW, weighted by cleared volume across DC/DM/DR, x 8,760 h, x participation share = "
                "mean cleared MW / GB operational battery MW (REPD); same for every duration"
            ),
            derivation=(
                f"GBP/MW/h (L+H): {prices}; mean cleared MW: {volumes}; volume-weighted "
                f"{v.value_gbp_per_mw_h:.2f} GBP/MW/h; share {sum(v.pair_volume.values()):,.0f} MW / "
                f"{fleet_mw:,.0f} MW = {v.share:.3f} (REPD {repd_url}: {with_mw} of {operational} Operational "
                f"Battery records carry MW, so the fleet is undercounted and the share is high); "
                f"{v.value_gbp_per_mw_h:.2f} x 8760 x {v.share:.3f} = GBP {gbp:,.0f}/MW/yr; "
                f"resources {', '.join(resource_ids)}"
            ),
            gbp_per_mw_year_by_duration={str(d): round(gbp) for d in REQUIRED_DURATION_HOURS},
        )
        _store("balancing_ancillary", today, figure)
        return figure

    async def fetch(self, duration_h: int) -> StreamValue:
        """Ancillary response revenue for one duration. One live attempt per instance, so a failure is not retried."""
        return _to_stream(await _once(self, self.figure), duration_h)
