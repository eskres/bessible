"""HM Land Registry CCOD / OCOD: title numbers of land owned by UK (CCOD) and overseas (OCOD) companies.

API: https://use-land-property-data.service.gov.uk/api-documentation (free; needs an account, `HMLR_API_KEY`,
and the dataset licence accepted once in the web service). Only `ccod` and `ocod` may be requested: the same API
serves paid datasets (the National Polygon Service), which this module refuses to touch.

The monthly full file (a zipped CSV) is downloaded to `out/cache/hmlr/` and indexed by postcode in SQLite, so a
lookup is a local query. The rows name companies, never private individuals, and cover company-owned land only.
There are no polygons: a row matches a site by postcode (and address), never a polygon.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel

from bessible.config import settings

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

API = "https://use-land-property-data.service.gov.uk/api/v1"
Dataset = Literal["ccod", "ocod"]
DATASETS: tuple[Dataset, ...] = ("ccod", "ocod")  # free; anything else is refused
DATASET_URL = {
    "ccod": "https://use-land-property-data.service.gov.uk/datasets/ccod",
    "ocod": "https://use-land-property-data.service.gov.uk/datasets/ocod",
}
DATASET_NAME = {"ccod": "HMLR CCOD", "ocod": "HMLR OCOD"}
BATCH = 50_000  # rows per SQLite insert while indexing
_COLUMNS = {
    "title_number": "Title Number",
    "tenure": "Tenure",
    "address": "Property Address",
    "postcode": "Postcode",
    "proprietor": "Proprietor Name (1)",
    "company_no": "Company Registration No. (1)",
    "country": "Country Incorporated (1)",  # OCOD only
}


_SELECT = "title_number, tenure, address, postcode, proprietor, company_no, country"


class HmlrError(Exception):
    """The API refused a request (no key, licence not accepted, unknown file)."""


class OwnershipRow(BaseModel):
    """One CCOD / OCOD row: a title owned by a company."""

    dataset: Dataset
    file: str  # the monthly file it came from
    title_number: str
    tenure: str | None = None
    address: str | None = None
    postcode: str | None = None
    proprietor: str | None = None
    company_no: str | None = None
    country: str | None = None


def _check(dataset: str) -> Dataset:
    if dataset not in DATASETS:
        msg = f"Refusing HMLR dataset {dataset!r}: only the free {DATASETS} are allowed"
        raise HmlrError(msg)
    return dataset


def _headers() -> dict[str, str]:
    if settings.hmlr_api_key is None:
        msg = "HMLR_API_KEY is not set"
        raise HmlrError(msg)
    return {"Authorization": settings.hmlr_api_key.get_secret_value(), "Accept": "application/json"}


def cache_dir() -> Path:
    """Where the monthly files and their indexes live (gitignored)."""
    return settings.cache_dir / "hmlr"


def norm_postcode(postcode: str | None) -> str:
    """Upper case, no spaces: 'ab1 2cd' -> 'AB12CD'."""
    return re.sub(r"\s+", "", postcode or "").upper()


async def latest_full_file(dataset: str, client: httpx.AsyncClient) -> str:
    """The current monthly full file's name, e.g. CCOD_FULL_2026_09.zip."""
    name = _check(dataset)
    r = await client.get(f"{API}/datasets/{name}", headers=_headers())
    body = r.json()
    if r.is_error or not body.get("success"):
        raise HmlrError(str(body.get("error") or r.status_code))
    for res in body["result"]["resources"]:
        if res.get("name") == "Full File":
            return str(res["file_name"])
    msg = f"No full file listed for {name}"
    raise HmlrError(msg)


async def download(dataset: str, *, client: httpx.AsyncClient | None = None) -> Path:
    """Download the current full file into `out/cache/hmlr/` (skipped when already there). Returns its path."""
    if client is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=600), follow_redirects=True) as own:
            return await download(dataset, client=own)
    name = _check(dataset)
    file_name = await latest_full_file(name, client)
    target = cache_dir() / file_name
    if target.exists():
        return target
    r = await client.get(f"{API}/datasets/{name}/{file_name}", headers=_headers())
    body = r.json()
    if r.is_error or not body.get("success"):
        raise HmlrError(str(body.get("error") or r.status_code))  # e.g. the licence is not accepted yet
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    async with client.stream("GET", body["result"]["download_url"]) as stream:  # a signed URL: no key header
        stream.raise_for_status()
        with partial.open("wb") as out:
            async for chunk in stream.aiter_bytes(1 << 20):
                out.write(chunk)
    partial.rename(target)
    return target


def index_path(dataset: str) -> Path:
    """The SQLite index of a dataset."""
    return cache_dir() / f"{_check(dataset)}.sqlite"


def build_index(dataset: str, zip_path: Path) -> Path:
    """Index the zipped CSV by postcode into `out/cache/hmlr/<dataset>.sqlite` (replacing an older index)."""
    name = _check(dataset)
    target = index_path(name)
    tmp = target.with_suffix(".building")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute(
        "CREATE TABLE rows (title_number TEXT, tenure TEXT, address TEXT, postcode TEXT, pc TEXT,"
        " proprietor TEXT, company_no TEXT, country TEXT)"
    )
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('file', ?)", (zip_path.name,))
    with zipfile.ZipFile(zip_path) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(member) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            batch: list[tuple[str | None, ...]] = []
            for row in reader:
                pc = norm_postcode(row.get(_COLUMNS["postcode"]))
                if not pc:
                    continue
                batch.append((
                    row.get(_COLUMNS["title_number"]),
                    row.get(_COLUMNS["tenure"]) or None,
                    row.get(_COLUMNS["address"]) or None,
                    row.get(_COLUMNS["postcode"]) or None,
                    pc,
                    row.get(_COLUMNS["proprietor"]) or None,
                    row.get(_COLUMNS["company_no"]) or None,
                    row.get(_COLUMNS["country"]) or None,
                ))
                if len(batch) >= BATCH:
                    db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?)", batch)
                    batch = []
            db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?)", batch)
    db.execute("CREATE INDEX rows_pc ON rows (pc)")
    db.commit()
    db.close()
    tmp.replace(target)
    return target


def indexed_file(dataset: str) -> str | None:
    """The monthly file the local index was built from, or None when there is no index."""
    path = index_path(dataset)
    if not path.exists():
        return None
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'file'").fetchone()
    return str(row[0]) if row else None


def lookup(dataset: str, postcodes: Iterable[str], *, limit: int = 200) -> list[OwnershipRow]:
    """Rows of the local index at any of `postcodes`. Empty when there is no index."""
    name = _check(dataset)
    pcs = sorted({norm_postcode(p) for p in postcodes if norm_postcode(p)})
    path = index_path(name)
    if not pcs or not path.exists():
        return []
    marks = ",".join("?" * len(pcs))
    query = f"SELECT {_SELECT} FROM rows WHERE pc IN ({marks}) LIMIT ?"  # ruff: ignore[hardcoded-sql-expression] - only "?" marks
    with sqlite3.connect(path) as db:
        file = indexed_file(name) or ""
        rows = db.execute(
            query,
            (*pcs, limit),
        ).fetchall()
    return [
        OwnershipRow(
            dataset=name,
            file=file,
            title_number=r[0],
            tenure=r[1],
            address=r[2],
            postcode=r[3],
            proprietor=r[4],
            company_no=r[5],
            country=r[6],
        )
        for r in rows
    ]
