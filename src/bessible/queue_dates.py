"""Indicative connection timescale at a grid supply point (GSP), from the contracted dates of projects queued there.

Two free registers carry a contracted connection date per project (checked live 2026-09-28):

- UKPN "Appendix G Detail" (``ukpn-appendix-g``, CC BY 4.0, monthly): distribution-connected projects in each UKPN
  GSP's transmission queue, with their Gate 2 outcome. Few rows carry a date (54 of the 421 not yet connected).
- NESO TEC register (NESO Open Data Licence, updated Tue/Fri): transmission-connected and large embedded projects,
  matched on "Connection Site"; "MW Effective From" is the contracted date.

`indicative_timescale` is pure: months from today to each future date, with quartiles and range. A date already
passed ("lapsed") is counted but left out, because the project is late and its date says nothing about the future.
`gsp_queue_dates` reads both registers (cached for a day under ``out/cache/queue_dates/``); a source that fails is
listed in ``failed`` instead of raising, so the grid stage can still report the gap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, HttpUrl

from bessible.api import ckan, neso, opendatasoft, ukpn
from bessible.config import settings
from bessible.models import Artifact

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path

log = logging.getLogger(__name__)

Register = Literal["UKPN Appendix G", "NESO TEC"]

APPENDIX_G = ukpn.DATASETS["appendix_g"]
APPENDIX_G_URL = "https://ukpowernetworks.opendatasoft.com/explore/dataset/ukpn-appendix-g/"
TEC_URL = f"https://www.neso.energy/data-portal/{neso.TEC_REGISTER_PACKAGE}"
SOURCE_URLS: dict[Register, str] = {"UKPN Appendix G": APPENDIX_G_URL, "NESO TEC": TEC_URL}

EXCEL_EPOCH = date(1899, 12, 30)  # Appendix G stores some dates as Excel day serials
DAYS_PER_MONTH = 30.4375
CACHE_TTL_S = 24 * 3600
MAX_PAGES = 10  # Appendix G has ~420 rows not yet connected, 100 per page
MAX_CONFIDENCE = 0.8
MODEL_USED = "deterministic"


class QueuedConnection(BaseModel):
    """One project queued at the GSP with a contracted connection date."""

    listed_on: Register
    project: str
    mw: float | None = None
    status: str | None = None
    connection_date: date


class QueueDates(BaseModel):
    """Every dated project found at one GSP, and which registers were read."""

    gsp: str
    connections: list[QueuedConnection]
    rows: dict[Register, int]  # rows at the GSP per register that was read, dated or not
    failed: list[Register]  # registers that could not be read


class QueueTimescale(BaseModel):
    """Months from `today` to the contracted connection dates still ahead at the GSP."""

    gsp: str
    today: date
    records: int  # future dates used
    lapsed: int  # dates already passed, left out
    median_months: float
    p25_months: float
    p75_months: float
    min_months: float
    max_months: float
    by_register: dict[Register, int]  # future dates per register
    confidence: float  # from `records` only


# ------------------------------------------ pure parts ------------------------------------------ #


def gsp_key(name: str) -> str:
    """Comparable GSP name: "Hackney 132kV" -> "HACKNEY 132", "BARKING C (EPN)" -> "BARKING C"."""
    s = re.sub(r"\([A-Z]{3}\)", " ", name.upper())
    s = re.sub(r"\b(\d+)\s*KV\b", r"\1", s)
    s = re.sub(r"\b(GSP|SUBSTATION)\b", " ", s)
    return " ".join(s.split())


def same_gsp(a: str, b: str) -> bool:
    """Whether two GSP names mean the same site: equal, or one extends the other ("TILBURY" ~ "TILBURY 1&7")."""
    x, y = gsp_key(a), gsp_key(b)
    return bool(x and y) and (x == y or x.startswith(y + " ") or y.startswith(x + " "))


def tec_site(gsp: str) -> str:
    """The name to match in the TEC "Connection Site" column: the GSP name without voltage or circuit numbers."""
    words = [w for w in gsp_key(gsp).split() if not any(c.isdigit() for c in w)]
    return " ".join(words).title()


def appendix_g_date(text: str | None) -> date | None:
    """Parse an Appendix G date cell: "dd/mm/yyyy" or an Excel day serial; None for "Connected" or blank."""
    t = (text or "").strip()
    if t.isdigit():
        return EXCEL_EPOCH + timedelta(days=int(t))
    try:
        return datetime.strptime(t, "%d/%m/%Y").date()  # ruff: ignore[call-datetime-strptime-without-zone] - a date
    except ValueError:
        return None


def from_appendix_g(records: Iterable[ukpn.AppendixGRecord], gsp: str) -> tuple[list[QueuedConnection], int]:
    """Dated, not yet connected Appendix G projects at `gsp`, and the count of all its not-connected rows."""
    at_gsp = [r for r in records if r.gsp and same_gsp(r.gsp, gsp) and r.connection_status != "Connected"]
    out = [
        QueuedConnection(
            listed_on="UKPN Appendix G",
            project=r.site_name or r.unique_nodd_id or "unnamed",
            mw=r.developer_capacity_mw,
            status=r.connection_status,
            connection_date=when,
        )
        for r in at_gsp
        if (when := appendix_g_date(r.date_of_connection))
    ]
    return out, len(at_gsp)


def from_tec(records: Iterable[neso.TecRegisterRecord]) -> tuple[list[QueuedConnection], int]:
    """Dated TEC rows not yet built, and the count of all rows not yet built."""
    queued = [r for r in records if r.project_status != "Built"]
    out = [
        QueuedConnection(
            listed_on="NESO TEC",
            project=r.project_name or r.project_number or "unnamed",
            mw=r.mw_increase_decrease,
            status=r.project_status,
            connection_date=r.mw_effective_from,
        )
        for r in queued
        if r.mw_effective_from
    ]
    return out, len(queued)


def confidence_for(records: int) -> float:
    """Confidence that grows with the number of dates, capped at MAX_CONFIDENCE: 1 -> 0.2, 3 -> 0.4, 12 -> 0.64."""
    return round(MAX_CONFIDENCE * records / (records + 3), 2)


def indicative_timescale(queue: QueueDates, today: date) -> QueueTimescale | None:
    """Median and spread of months from `today` to each future contracted date at the GSP; None when there is none."""
    ahead = [c for c in queue.connections if c.connection_date >= today]
    if not ahead:
        return None
    months = sorted(round((c.connection_date - today).days / DAYS_PER_MONTH, 1) for c in ahead)
    p25, p75 = (months[0], months[0]) if len(months) == 1 else statistics.quantiles(months, n=4)[::2]
    by_register: dict[Register, int] = {}
    for c in ahead:
        by_register[c.listed_on] = by_register.get(c.listed_on, 0) + 1
    return QueueTimescale(
        gsp=queue.gsp,
        today=today,
        records=len(months),
        lapsed=len(queue.connections) - len(ahead),
        median_months=round(statistics.median(months), 1),
        p25_months=round(p25, 1),
        p75_months=round(p75, 1),
        min_months=months[0],
        max_months=months[-1],
        by_register=by_register,
        confidence=confidence_for(len(months)),
    )


def timescale_artifact(t: QueueTimescale, queue: QueueDates, artifact_id: str) -> Artifact:
    """The grid-stage artifact: the figure, the registers and datasets it came from, and the record counts."""
    parts = [
        f"{n} from {reg} ({queue.rows.get(reg, 0)} queued rows at the GSP)" for reg, n in sorted(t.by_register.items())
    ]
    lapsed = f"; {t.lapsed} contracted dates already passed and are left out" if t.lapsed else ""
    failed = f"; not read: {', '.join(queue.failed)}" if queue.failed else ""
    claim = (
        f"Indicative connection timescale at {t.gsp}: median {t.median_months:g} months from {t.today.isoformat()} "
        f"(interquartile {t.p25_months:g}-{t.p75_months:g}, range {t.min_months:g}-{t.max_months:g}) to the contracted "
        f"connection dates of {t.records} projects queued there: {'; '.join(parts)}{lapsed}{failed}. "
        f"These are other projects' dates, not an offer for this site "
        f"[UKPN ukpn-appendix-g {APPENDIX_G_URL}; NESO TEC register {TEC_URL}]"
    )
    main: Register = max(t.by_register, key=lambda r: t.by_register[r])
    return Artifact(
        id=artifact_id,
        stage="grid",
        claim=claim,
        source_url=HttpUrl(SOURCE_URLS[main]),
        confidence=t.confidence,
        model_used=MODEL_USED,
    )


# ------------------------------------------ fetching ------------------------------------------- #


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
    return settings.cache_dir / "queue_dates" / f"{digest}.json"


async def _cached_json(key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:  # ruff: ignore[any-type] - raw JSON
    """The raw body for `key` from the day-old cache, or from `fetch()` (then cached)."""
    cache = _cache_path(key)
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        return json.loads(cache.read_text(encoding="utf-8"))
    body = await fetch()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(body), encoding="utf-8")
    return body


async def fetch_appendix_g(client: httpx.AsyncClient) -> list[ukpn.AppendixGRecord]:
    """Every Appendix G row not yet connected, all GSPs (one cache entry for every site). Needs UKPN_API_KEY."""
    if settings.ukpn_api_key is None:
        msg = "UKPN_API_KEY is not set"
        raise ValueError(msg)
    headers = opendatasoft.auth_headers(settings.ukpn_api_key.get_secret_value())

    async def fetch() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            req = opendatasoft.RecordsRequest(
                base_url=APPENDIX_G.base_url,
                dataset=APPENDIX_G.dataset,
                where='connection_status != "Connected"',
                order_by="position",
                limit=100,
                offset=page * 100 or None,
            )
            r = await client.get(req.url(), params=req.params(), headers=headers)
            r.raise_for_status()
            got = r.json()
            rows += got["results"]
            if len(rows) >= got["total_count"]:
                break
        return rows

    body = await _cached_json("appendix_g|not-connected", fetch)
    return [ukpn.AppendixGRecord.model_validate(row) for row in body]


async def fetch_tec(client: httpx.AsyncClient, site: str) -> list[neso.TecRegisterRecord]:
    """TEC register rows whose "Connection Site" contains `site`."""
    req = neso.tec_at_sites([site])

    async def fetch() -> dict[str, Any]:
        r = await client.get(req.URL, params=req.params())
        r.raise_for_status()
        body: dict[str, Any] = r.json()
        return body

    body = await _cached_json(f"tec|{req.sql}", fetch)
    parsed = ckan.DatastoreSearchSqlResponse[neso.TecRegisterRecord].model_validate(body)
    return parsed.result.records if parsed.result else []


async def gsp_queue_dates(gsp: str, *, client: httpx.AsyncClient | None = None) -> QueueDates:
    """Dated projects queued at `gsp` in UKPN Appendix G and the NESO TEC register."""
    if client is None:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "bessible"}) as own:
            return await gsp_queue_dates(gsp, client=own)

    async def appendix_g() -> tuple[list[QueuedConnection], int]:
        return from_appendix_g(await fetch_appendix_g(client), gsp)

    async def tec() -> tuple[list[QueuedConnection], int]:
        return from_tec(await fetch_tec(client, tec_site(gsp)))

    readers: dict[Register, Callable[[], Awaitable[tuple[list[QueuedConnection], int]]]] = {
        "UKPN Appendix G": appendix_g,
        "NESO TEC": tec,
    }
    queue = QueueDates(gsp=gsp, connections=[], rows={}, failed=[])
    for register, read in readers.items():
        try:
            got, queue.rows[register] = await read()
        except (httpx.HTTPError, ValueError) as e:  # ValueError covers a missing key and a stale model
            log.warning("%s not read for %s: %s", register, gsp, e)
            queue.failed.append(register)
        else:
            queue.connections += got
    return queue


async def queue_timescale(gsp: str, today: date, artifact_id: str) -> tuple[int, Artifact] | None:
    """Grid-stage entry point: (median months, artifact) from the queue dates at `gsp`, or None when there are none."""
    queue = await gsp_queue_dates(gsp)
    t = indicative_timescale(queue, today)
    if t is None:
        return None
    return round(t.median_months), timescale_artifact(t, queue, artifact_id)
