"""NESO (National Energy System Operator) open data portal — CKAN action API.

Docs: https://www.neso.energy/data-portal/api-guidance
CKAN: https://docs.ckan.org/en/latest/api/ and https://docs.ckan.org/en/latest/maintaining/datastore.html

Rate limits (NESO guidance): CKAN actions (package_*) max 1 request/second;
datastore actions (datastore_search, datastore_search_sql) max 2 requests/MINUTE.
No API key needed. Errors come back with a non-200 status (404/409) and
``{"success": false, "error": {...}}`` with no ``result``.

Location filtering: neither register has coordinates, county or region columns. The only
location-ish columns are "Connection Site" (free-text substation/GSP name) and "HOST TO"
(NGET = England & Wales, SPT/SHET = Scotland, OFTO = offshore). To find projects near a
point, first resolve nearby substation names elsewhere, then match "Connection Site" with
``datastore_search_sql`` + ILIKE (enabled, verified) or the full-text ``q`` param. For
Darlington/Teesside, sites such as Norton, Hartmoor, Lackenby, Spennymoor, Saltholme match.

NB the Embedded Register covers SCOTLAND only (HOST TO is SHET or SPT), so it is of no use
for English sites; English embedded projects with TEC appear in the TEC register with
"Agreement Type" = "Embedded".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

from pydantic import Field

from . import ckan
from .base import ApiResponse

BASE_URL = "https://api.neso.energy/api/3/action/"

TEC_REGISTER_PACKAGE = "transmission-entry-capacity-tec-register"

TEC_REGISTER_RESOURCE_ID = "17becbab-e3e8-473f-b303-3806f43a6a10"  # ~2.2k rows, updated Tue/Fri

EMBEDDED_REGISTER_PACKAGE = "embedded-register"

EMBEDDED_REGISTER_RESOURCE_ID = "68b6f3a1-e1bf-403b-9062-0269fc758d77"  # ~560 rows, Scotland only

DNO_LICENCE_AREAS_PACKAGE = "gis-boundaries-for-gb-dno-license-areas"

_DNO_DL = "https://api.neso.energy/dataset/0e377f16-95e9-4c15-a1fc-49e06a39cfa0/resource/"

DNO_LICENCE_AREAS_RESOURCES: dict[str, str] = {
    "geojson_20240503": _DNO_DL
    + "1c6a7dc0-1b6c-443a-bc67-5f7125649434/download/gb-dno-license-areas-20240503-as-geojson.geojson",
    "shapefile_zip_20240503": _DNO_DL
    + "668df251-b8a5-4ac3-8427-20b93b2f5f67/download/gb-dno-license-areas-20240503-as-esri-shape-file.zip",
    "png_20240503": _DNO_DL + "26d1b3b3-2ea7-4681-bbd0-8fc7f574c6db/download/gb-dno-license-areas-20240503.png",
    "geojson_20200506": _DNO_DL + "e96db306-aaa8-45be-aecd-65b34d38923a/download/dno_license_areas_20200506.geojson",
    "shapefile_zip_20200506": _DNO_DL + "46a7b674-49f9-4ad6-b778-df48f6736222/download/dno_license_areas_20200506.zip",
    "pdf_20200506": _DNO_DL + "af79a476-33b0-42a3-8c6d-1c578d844cc3/download/dno_license_areas_20200506.pdf",
}

"""GIS boundaries of the 14 GB DNO licence areas: plain file downloads, NOT in the datastore
(datastore_active=false on every resource, so datastore_search does not work). Each URL
302-redirects to a short-lived signed Cloudflare R2 URL, so follow redirects. Geometry CRS
is EPSG:27700 (British National Grid), including the GeoJSON — reproject before testing a
WGS84 lat/lon. Boundaries are approximate. Resource list via
``PackageShowRequest(id=DNO_LICENCE_AREAS_PACKAGE)``."""

STORAGE_PLANT_TYPE = "Energy Storage System"  # also inside hybrids: "Energy Storage System;PV Array (...)"


# ------------------------------------------ 1. Request ------------------------------------------ #


class PackageShowRequest(ckan.PackageShowRequest):
    """GET package_show on NESO. Max 1 request/second."""

    URL: ClassVar[str] = BASE_URL + "package_show"


class PackageSearchRequest(ckan.PackageSearchRequest):
    """GET package_search on NESO. Max 1 request/second."""

    URL: ClassVar[str] = BASE_URL + "package_search"


class DatastoreSearchRequest(ckan.DatastoreSearchRequest):
    """GET datastore_search on NESO. Max 2 requests/minute."""

    URL: ClassVar[str] = BASE_URL + "datastore_search"


class DatastoreSearchSqlRequest(ckan.DatastoreSearchSqlRequest):
    """GET datastore_search_sql on NESO (enabled, verified). Max 2 requests/minute."""

    URL: ClassVar[str] = BASE_URL + "datastore_search_sql"


# ----------------------------------------- 2. Response ------------------------------------------ #

# The CKAN envelopes in `ckan`, parametrised with a row model from section 3, e.g.
# `ckan.DatastoreSearchSqlResponse[TecRegisterRecord]`, `ckan.PackageShowResponse`.


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class _RegisterRecord(ApiResponse):
    """Columns shared by the TEC and Embedded registers.

    Everything is optional because
    the ``fields`` request param (or a SQL column list) can project any subset.
    """

    id: int | None = Field(default=None, alias="_id")  # row number, not stable across republishes
    project_name: str | None = Field(default=None, alias="Project Name")
    customer_name: str | None = Field(default=None, alias="Customer Name")
    connection_site: str | None = Field(
        default=None, alias="Connection Site"
    )  # substation/GSP name; only location column
    stage: float | None = Field(default=None, alias="Stage")  # null unless staged
    mw_connected: float | None = Field(default=None, alias="MW Connected")  # MW, 0 until built
    mw_increase_decrease: float | None = Field(default=None, alias="MW Increase / Decrease")  # MW
    cumulative_total_capacity_mw: float | None = Field(default=None, alias="Cumulative Total Capacity (MW)")
    mw_effective_from: date | None = Field(default=None, alias="MW Effective From")  # null once built
    project_status: str | None = Field(
        default=None, alias="Project Status"
    )  # "Scoping", "Awaiting Consents", "Consents Approved", "Built", ...
    host_to: str | None = Field(default=None, alias="HOST TO")  # transmission owner
    plant_type: str | None = Field(
        default=None, alias="Plant Type"
    )  # ";"-separated when hybrid, e.g. "Energy Storage System;PV Array (Photo Voltaic/solar)"
    project_id: str | None = Field(default=None, alias="Project ID")  # case sensitive
    project_number: str | None = Field(
        default=None, alias="Project Number"
    )  # "PRO-001136" or "PRO-001136-1" (stage suffix)
    gate: float | None = Field(default=None, alias="Gate")  # 1 or 2; null until countersigned
    full_text: str | None = Field(default=None, alias="_full_text")  # only via SQL SELECT *
    rank: float | None = None  # only when `q` is used


class TecRegisterRecord(_RegisterRecord):
    """Row of the TEC register (resource TEC_REGISTER_RESOURCE_ID), GB-wide.

    HOST TO is NGET (England & Wales), SHET, SPT or OFTO.
    """

    agreement_type: str | None = Field(default=None, alias="Agreement Type")  # "Direct Connection" | "Embedded"


class EmbeddedRegisterRecord(_RegisterRecord):
    """Row of the Embedded register (resource EMBEDDED_REGISTER_RESOURCE_ID).

    Scotland only: HOST TO is SHET or SPT. No "Agreement Type" column.
    """


# -------------------- 4. Not from the API (helpers, registries, transforms) --------------------- #


_TEC_COLUMNS = (
    "Project Name",
    "Customer Name",
    "Connection Site",
    "Stage",
    "MW Connected",
    "MW Increase / Decrease",
    "Cumulative Total Capacity (MW)",
    "MW Effective From",
    "Project Status",
    "Agreement Type",
    "HOST TO",
    "Plant Type",
    "Project ID",
    "Project Number",
    "Gate",
)


def tec_at_sites(sites: list[str], *, storage_only: bool = False, limit: int = 200) -> DatastoreSearchSqlRequest:
    """TEC register rows whose "Connection Site" contains any of `sites`, largest first.

    `sites` are grid supply point / substation names from the DNO data (UKPN heatmap ``gsp``, e.g.
    "West Weybridge", which matches "West Weybridge 275kV Substation"). This is the only way to make the
    register location-based. Parse with ``DatastoreSearchSqlResponse[TecRegisterRecord]``.
    """
    if not sites:
        msg = "at least one site name is needed"
        raise ValueError(msg)
    likes = " OR ".join("\"Connection Site\" ILIKE '%{}%'".format(s.strip().replace("'", "''")) for s in sites)
    where = f"({likes})"
    if storage_only:
        where += f" AND \"Plant Type\" ILIKE '%{STORAGE_PLANT_TYPE}%'"
    columns = ", ".join(f'"{c}"' for c in _TEC_COLUMNS)
    return DatastoreSearchSqlRequest(
        sql=f'SELECT {columns} FROM "{TEC_REGISTER_RESOURCE_ID}" WHERE {where} '  # ruff: ignore[hardcoded-sql-expression] - values are quoted above
        f'ORDER BY "Cumulative Total Capacity (MW)" DESC LIMIT {int(limit)}'
    )


# ------------------------------- Response and reserve auctions (EAC) ------------------------------- #

RESPONSE_RESERVE_PACKAGE = "eac-auction-results"
"""Enduring Auction Capability results: Dynamic Containment / Moderation / Regulation (DC, DM, DR, each Low and
High frequency) and the Quick / Slow / Balancing Reserve products. Daily auctions; one row per product per
delivery window. The current financial year lives in the "NESO Response-Reserve Results Summary" resource;
each April the past year moves to "NESO Response-Reserve Results Summary FY<yyyy> (Archive)" (FY2025 =
April 2025 to March 2026, checked against its first and last ``deliveryStart``). Find them with
``PackageShowRequest(id=RESPONSE_RESERVE_PACKAGE)`` and ``results_summary_resources``."""

RESULTS_SUMMARY_NAME = "NESO Response-Reserve Results Summary"

RESPONSE_PRODUCTS = ("DCL", "DCH", "DML", "DMH", "DRL", "DRH")  # L = low-frequency side, H = high-frequency side


class ResponseProductSummary(ApiResponse):
    """One row of ``response_product_summary``: per-product totals over a delivery window.

    The datastore returns SQL aggregates as strings (e.g. ``"1080"``); pydantic parses them.
    """

    auction_product: str = Field(alias="auctionProduct")  # e.g. "DCL"
    windows: int  # number of delivery windows (rows)
    price_sum: float  # sum of clearingPrice, GBP/MW/h
    volume_sum: float  # sum of clearedVolume, MW
    first_start: datetime  # earliest deliveryStart
    last_end: datetime  # latest deliveryEnd
    shortest: str  # shortest window, postgres interval text, e.g. "4:00:00"
    longest: str  # longest window


def results_summary_resources(package: ckan.CkanPackage, since: date) -> list[str]:
    """Ids of the Results Summary resources that can hold windows from `since` on: the current one plus archives.

    Archive names end "FY<yyyy> (Archive)"; FY<yyyy> runs April yyyy to March yyyy+1.
    """
    first_fy = since.year if since.month >= 4 else since.year - 1  # ruff: ignore[magic-value-comparison]
    ids = []
    for r in package.resources:
        name = (r.name or "").strip()
        if name == RESULTS_SUMMARY_NAME:
            ids.append(r.id)
        elif name.startswith(RESULTS_SUMMARY_NAME + " FY") and name.endswith("(Archive)"):
            fy = int(name.removeprefix(RESULTS_SUMMARY_NAME + " FY")[:4])
            if fy >= first_fy:
                ids.append(r.id)
    return ids


def response_product_summary(resource_id: str, start: date, end: date) -> DatastoreSearchSqlRequest:
    """Per-product totals of DC/DM/DR windows starting in [start, end).

    Parse with ``DatastoreSearchSqlResponse[ResponseProductSummary]``.

    NESO blocks SQL functions (EXTRACT, ...) but allows aggregates and interval arithmetic, so the window
    lengths come back as ``shortest``/``longest`` for the caller to check.
    """
    products = ", ".join(f"'{p}'" for p in RESPONSE_PRODUCTS)
    return DatastoreSearchSqlRequest(
        sql=(
            'SELECT "auctionProduct", COUNT(*) AS windows, SUM("clearingPrice") AS price_sum, '  # ruff: ignore[hardcoded-sql-expression] - dates are typed
            'SUM("clearedVolume") AS volume_sum, MIN("deliveryStart") AS first_start, '
            'MAX("deliveryEnd") AS last_end, MIN("deliveryEnd" - "deliveryStart") AS shortest, '
            'MAX("deliveryEnd" - "deliveryStart") AS longest '
            f'FROM "{resource_id}" WHERE "auctionProduct" IN ({products}) '
            f"AND \"deliveryStart\" >= '{start.isoformat()}' AND \"deliveryStart\" < '{end.isoformat()}' "
            'GROUP BY "auctionProduct"'
        )
    )
