from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from bessible.api.elexon import APX, ElexonError, MarketIndexStreamRequest, MarketIndexStreamResponse

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_mid_stream_request_params():
    req = MarketIndexStreamRequest(
        from_=date(2026, 9, 20),
        to=date(2026, 9, 20),
        settlement_period_from=1,
        settlement_period_to=50,
        data_providers=[APX],
    )
    assert req.URL == "https://data.elexon.co.uk/bmrs/api/v1/datasets/MID/stream"
    assert req.params() == {
        "from": "2026-09-20",
        "to": "2026-09-20",
        "settlementPeriodFrom": 1,
        "settlementPeriodTo": 50,
        "dataProviders": ["APXMIDP"],
    }


def test_mid_stream_one_settlement_day():
    rows = MarketIndexStreamResponse.model_validate(load("elexon_mid_stream_apx_20260920.json")).root
    assert len(rows) == 48
    assert {r.settlement_date for r in rows} == {date(2026, 9, 20)}
    assert {r.data_provider for r in rows} == {APX}
    assert sorted(r.settlement_period for r in rows) == list(range(1, 49))
    sp48 = next(r for r in rows if r.settlement_period == 48)
    assert sp48.price == 151.09
    assert sp48.volume == 1740.6
    assert sp48.start_time == datetime.fromisoformat("2026-09-20T22:30:00+00:00")


def test_error_body():
    err = ElexonError.model_validate(load("elexon_error_range.json"))
    assert err.status == 400
    assert "The From field must not be later than the To field" in err.errors[""]
