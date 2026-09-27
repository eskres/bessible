"""Elexon Insights Solution (BMRS) API — Market Index Data (MID).

Docs: https://bmrs.elexon.co.uk/api-documentation (OpenAPI: https://data.elexon.co.uk/swagger/v1/swagger.json)

No API key. The ``/datasets/MID/stream`` endpoint has no 7-day range limit (the plain ``/datasets/MID`` and
``/balancing/pricing/market-index`` endpoints reject ranges over 7 days), so one call returns a year of
half-hourly prices (~2.8 MB for one provider). Errors come back as RFC 9110 problem JSON with HTTP 400.

Two providers: APX ("APXMIDP", the traded day-ahead/within-day price) and N2EX ("N2EXMIDP", which reports
price 0 and volume 0 in almost every period, so it is not a price series).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import Field, RootModel

from .base import ApiRequest, ApiResponse

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1/"

APX = "APXMIDP"
N2EX = "N2EXMIDP"


# ------------------------------------------ 1. Request ------------------------------------------ #


class MarketIndexStreamRequest(ApiRequest):
    """GET datasets/MID/stream: Market Index Data, one row per provider per settlement period.

    With ``settlement_period_from``/``_to`` set, ``from``/``to`` filter on settlement DATE (inclusive, time
    ignored); without them they filter on start time. Setting 1..50 therefore means "whole settlement days".
    """

    URL: ClassVar[str] = BASE_URL + "datasets/MID/stream"
    METHOD: ClassVar[str] = "GET"

    from_: date | datetime = Field(alias="from")
    to: date | datetime
    settlement_period_from: int | None = Field(default=None, alias="settlementPeriodFrom", ge=1, le=50)
    settlement_period_to: int | None = Field(default=None, alias="settlementPeriodTo", ge=1, le=50)
    # Sent as a list, so httpx repeats the param per value, which the API expects
    data_providers: list[Literal["APXMIDP", "N2EXMIDP"]] | None = Field(default=None, alias="dataProviders")


# ----------------------------------------- 2. Response ------------------------------------------ #


class MarketIndexStreamResponse(RootModel[list["MarketIndexRecord"]]):
    """Body of datasets/MID/stream: a bare JSON array, newest first."""


class ElexonError(ApiResponse):
    """RFC 9110 problem body, e.g. for a bad or too-long date range (HTTP 400)."""

    type: str
    title: str
    status: int
    errors: dict[str, list[str]]  # field name ("" for the whole request) -> messages
    trace_id: str = Field(alias="traceId")


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class MarketIndexRecord(ApiResponse):
    """One provider's market index for one settlement period."""

    dataset: Literal["MID"]
    start_time: datetime = Field(alias="startTime")  # UTC start of the half hour
    data_provider: str = Field(alias="dataProvider")  # "APXMIDP" | "N2EXMIDP"
    settlement_date: date = Field(alias="settlementDate")  # UK local trading day
    settlement_period: int = Field(alias="settlementPeriod")  # 1..48 (46 / 50 on clock-change days)
    price: float  # GBP/MWh
    volume: float  # MWh traded; 0 means no price was formed for that period


MarketIndexStreamResponse.model_rebuild()
