from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from bessible.api import neso
from bessible.api.ckan import (
    DatastoreSearchResponse,
    DatastoreSearchSqlResponse,
    PackageSearchResponse,
    PackageShowResponse,
)
from bessible.api.neso import (
    DNO_LICENCE_AREAS_RESOURCES,
    EMBEDDED_REGISTER_RESOURCE_ID,
    TEC_REGISTER_RESOURCE_ID,
    DatastoreSearchRequest,
    DatastoreSearchSqlRequest,
    EmbeddedRegisterRecord,
    PackageSearchRequest,
    PackageShowRequest,
    TecRegisterRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_package_show_tec():
    r = PackageShowResponse.model_validate(load("neso_package_show_tec.json"))
    assert r.success
    assert r.result is not None
    assert r.result.name == "transmission-entry-capacity-tec-register"
    assert r.result.organization.name == "connection-registers"
    res = r.result.resources[0]
    assert res.id == TEC_REGISTER_RESOURCE_ID
    assert res.datastore_active is True
    assert res.format == "CSV"
    assert r.result.extras[0].key == "Update Frequency"


def test_package_show_gis_is_file_download_only():
    r = PackageShowResponse.model_validate(load("neso_package_show_gis.json"))
    assert len(r.result.resources) == 6
    assert all(x.datastore_active is False for x in r.result.resources)
    assert {x.url for x in r.result.resources} == set(DNO_LICENCE_AREAS_RESOURCES.values())


def test_package_show_error():
    r = PackageShowResponse.model_validate(load("neso_error_not_found.json"))
    assert r.success is False
    assert r.result is None
    assert r.error.type == "Not Found Error"
    assert r.error.message == "Not found"


def test_package_search():
    r = PackageSearchResponse.model_validate(load("neso_package_search.json"))
    assert r.result.count == 3
    assert len(r.result.results) == 2
    assert r.result.results[1].name == "embedded-register"
    assert r.result.results[1].resources[0].id == EMBEDDED_REGISTER_RESOURCE_ID
    assert r.result.search_facets["organization"].items[0].count == 3
    assert r.result.facets["tags"]["Register"] == 3


def test_datastore_search_tec():
    r = DatastoreSearchResponse[TecRegisterRecord].model_validate(load("neso_datastore_search_tec.json"))
    assert r.result.total == 2198
    assert r.result.limit == 5
    assert r.result.links.next.startswith("/api/3/action/datastore_search?")
    assert r.result.fields[0].info is None
    assert r.result.fields[3].id == "Connection Site"
    assert r.result.fields[3].info.unit == "n/a"
    rec = r.result.records[0]
    assert isinstance(rec, TecRegisterRecord)
    assert rec.connection_site == "Berkswell GSP"
    assert rec.host_to == "NGET"
    assert rec.cumulative_total_capacity_mw == pytest.approx(92.4)
    assert rec.mw_effective_from == date(2034, 10, 31)
    assert rec.agreement_type == "Embedded"
    assert rec.gate is None
    assert r.result.records[2].gate == 1
    assert r.result.records[4].mw_effective_from is None
    assert not rec.model_extra


def test_datastore_search_tec_filtered():
    r = DatastoreSearchResponse[TecRegisterRecord].model_validate(load("neso_datastore_search_tec_filtered.json"))
    assert r.result.filters == {"HOST TO": "NGET", "Plant Type": "Energy Storage System"}
    assert r.result.q == "Norton"
    assert r.result.offset == 1
    assert r.result.total == 3
    rec = r.result.records[0]
    assert rec.connection_site == "Norton 275kV Substation"
    assert rec.rank is not None
    assert rec.customer_name is None


def test_datastore_search_embedded():
    r = DatastoreSearchResponse[EmbeddedRegisterRecord].model_validate(load("neso_datastore_search_embedded.json"))
    assert r.result.resource_id == EMBEDDED_REGISTER_RESOURCE_ID
    assert r.result.total == 561
    assert {x.host_to for x in r.result.records} == {"SHET", "SPT"}
    assert r.result.records[0].plant_type == "Energy Storage System"
    assert not r.result.records[0].model_extra


def test_datastore_search_sql():
    r = DatastoreSearchSqlResponse[TecRegisterRecord].model_validate(load("neso_datastore_search_sql_tec.json"))
    assert r.result.sql.startswith("SELECT * FROM")
    assert r.result.fields[1].type == "tsvector"
    sites = {x.connection_site for x in r.result.records}
    assert "Norton 275kV Substation" in sites
    assert "Lackenby 400kV Substation" in sites
    assert r.result.records[0].full_text


def test_request_params():
    assert PackageShowRequest(id="embedded-register").params() == {"id": "embedded-register"}
    assert PackageSearchRequest(
        q="tec register", rows=2, sort="metadata_modified desc", facet_field=["tags", "organization"], facet_limit=3
    ).params() == {
        "q": "tec register",
        "rows": 2,
        "sort": "metadata_modified desc",
        "facet.field": '["tags", "organization"]',
        "facet.limit": 3,
    }
    assert DatastoreSearchRequest(
        resource_id=TEC_REGISTER_RESOURCE_ID,
        q="Norton",
        filters={"HOST TO": "NGET"},
        fields=["_id", "Connection Site"],
        sort="Project Name asc",
        limit=3,
        offset=1,
    ).params() == {
        "resource_id": TEC_REGISTER_RESOURCE_ID,
        "q": "Norton",
        "filters": '{"HOST TO": "NGET"}',
        "fields": "_id,Connection Site",
        "sort": "Project Name asc",
        "limit": 3,
        "offset": 1,
    }
    sql = f'SELECT * FROM "{TEC_REGISTER_RESOURCE_ID}" LIMIT 1'
    assert DatastoreSearchSqlRequest(sql=sql).params() == {"sql": sql}


def test_tec_at_sites():
    req = neso.tec_at_sites(["West Weybridge", "St John's Wood"], storage_only=True, limit=50)
    assert "\"Connection Site\" ILIKE '%West Weybridge%' OR \"Connection Site\" ILIKE '%St John''s Wood%'" in req.sql
    assert "\"Plant Type\" ILIKE '%Energy Storage System%'" in req.sql
    assert req.sql.endswith("LIMIT 50")
    with pytest.raises(ValueError, match="at least one"):
        neso.tec_at_sites([])
    body = json.loads((FIXTURES / "neso_tec_at_sites_dorking.json").read_text())
    rows = DatastoreSearchSqlResponse[neso.TecRegisterRecord].model_validate(body).result.records
    assert {r.connection_site.split()[0] for r in rows} == {"Bolney", "West"}
    assert rows[0].cumulative_total_capacity_mw == max(r.cumulative_total_capacity_mw for r in rows)


def test_package_show_eac_results_summary_resources():
    r = PackageShowResponse.model_validate(load("neso_package_show_eac.json"))
    assert r.result.name == neso.RESPONSE_RESERVE_PACKAGE
    # Window from 27 Sep 2025: the current summary plus the FY2025 archive (Apr 2025 - Mar 2026), not FY2024
    assert neso.results_summary_resources(r.result, date(2025, 9, 27)) == [
        "596f29ac-0387-4ba4-a6d3-95c243140707",
        "be55ee51-b79e-47da-b71e-a0f8865d9d66",
    ]
    assert len(neso.results_summary_resources(r.result, date(2024, 9, 27))) == 3  # FY2024 archive too


def test_response_product_summary_sql():
    req = neso.response_product_summary("abc", date(2025, 9, 26), date(2026, 9, 26))
    assert 'FROM "abc"' in req.sql
    assert "\"deliveryStart\" >= '2025-09-26'" in req.sql
    assert "'DCL', 'DCH', 'DML', 'DMH', 'DRL', 'DRH'" in req.sql


def test_response_product_summary_response():
    r = DatastoreSearchSqlResponse[neso.ResponseProductSummary].model_validate(
        load("neso_response_summary_fy2025.json")
    )
    rows = {x.auction_product: x for x in r.result.records}
    assert set(rows) == set(neso.RESPONSE_PRODUCTS)
    dml = rows["DML"]
    assert dml.windows == 1121
    assert dml.price_sum == 6649.73
    assert dml.volume_sum == 569138
    assert dml.first_start.isoformat() == "2025-09-26T02:00:00"  # naive, as the datastore returns it
    assert dml.last_end.isoformat() == "2026-03-31T22:00:00"
    # EFA blocks are 4 h; clock-change days make one 3 h and one 5 h block
    assert (dml.shortest, dml.longest) == ("3:00:00", "5:00:00")
    assert rows["DRH"].price_sum < 0  # regulation high cleared below zero on average
