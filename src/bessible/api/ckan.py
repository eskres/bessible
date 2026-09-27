"""CKAN action API: the envelope, package and datastore models shared by every CKAN portal.

Used by ``neso``, ``nged`` and ``ssen_distribution``, which subclass the requests to set their ``URL``.
Docs: https://docs.ckan.org/en/latest/api/ and https://docs.ckan.org/en/latest/maintaining/datastore.html

Notes:
- Errors come back with a non-200 status (404/409) and ``{"success": false, "error": {...}}`` with no ``result``.
- ``datastore_search_sql`` is optional per portal (NESO: on; NGED: off; SSEN Distribution: account only).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from .base import ApiRequest, ApiResponse

# ------------------------------------------ 1. Request ------------------------------------------ #


class PackageShowRequest(ApiRequest):
    """GET package_show request.

    https://docs.ckan.org/en/latest/api/#ckan.logic.action.get.package_show
    """

    ACTION: ClassVar[str] = "package_show"  # providers subclass this and set URL = BASE_URL + ACTION
    METHOD: ClassVar[str] = "GET"

    id: str  # package id or name
    use_default_schema: bool | None = None
    include_tracking: bool | None = None


class PackageSearchRequest(ApiRequest):
    """GET package_search (Solr) request.

    https://docs.ckan.org/en/latest/api/#ckan.logic.action.get.package_search
    """

    ACTION: ClassVar[str] = "package_search"  # providers subclass this and set URL = BASE_URL + ACTION
    METHOD: ClassVar[str] = "GET"

    q: str | None = None  # Solr query, default "*:*"
    fq: str | None = None  # Solr filter query, e.g. 'organization:connection-registers'
    rows: int | None = Field(default=None, ge=0, le=1000)  # default 10
    start: int | None = Field(default=None, ge=0)
    sort: str | None = None  # e.g. "metadata_modified desc"; default "score desc, metadata_modified desc"
    facet: bool | None = None
    facet_field: list[str] | None = Field(default=None, serialization_alias="facet.field")  # sent as JSON list
    facet_limit: int | None = Field(default=None, serialization_alias="facet.limit")  # default 50, negative = unlimited
    facet_mincount: int | None = Field(default=None, serialization_alias="facet.mincount")
    include_drafts: bool | None = None
    include_private: bool | None = None
    use_default_schema: bool | None = None

    def params(self) -> dict[str, Any]:
        """Return query params with ``facet.field`` JSON-encoded."""
        p = super().params()
        if "facet.field" in p:
            p["facet.field"] = json.dumps(p["facet.field"])
        return p


class DatastoreSearchRequest(ApiRequest):
    """GET datastore_search request.

    https://docs.ckan.org/en/latest/maintaining/datastore.html#ckanext.datastore.logic.action.datastore_search


    """

    ACTION: ClassVar[str] = "datastore_search"  # providers subclass this and set URL = BASE_URL + ACTION
    METHOD: ClassVar[str] = "GET"

    resource_id: str
    q: str | dict[str, str] | None = None  # full-text; dict = per-column, sent as JSON
    filters: dict[str, Any] | None = None  # exact match {column: value | [values]}, sent as JSON
    plain: bool | None = None  # default true; false lets q use postgres tsquery syntax
    language: str | None = None
    limit: int | None = Field(default=None, ge=0, le=32000)  # default 100
    offset: int | None = Field(default=None, ge=0)
    fields: list[str] | None = None  # sent comma-separated
    sort: str | None = None  # e.g. "Project Name asc, _id desc"
    distinct: bool | None = None
    include_total: bool | None = None  # default true
    total_estimation_threshold: int | None = None
    records_format: Literal["objects", "lists", "csv", "tsv"] | None = None

    def params(self) -> dict[str, Any]:
        """Return query params with ``q``/``filters`` JSON-encoded and ``fields`` comma-joined."""
        p = super().params()
        if isinstance(p.get("q"), dict):
            p["q"] = json.dumps(p["q"])
        if "filters" in p:
            p["filters"] = json.dumps(p["filters"])
        if "fields" in p:
            p["fields"] = ",".join(p["fields"])
        return p


class DatastoreSearchSqlRequest(ApiRequest):
    """GET datastore_search_sql.

    https://docs.ckan.org/en/latest/maintaining/datastore.html#ckanext.datastore.logic.action.datastore_search_sql
    Single SELECT only; double-quote the resource id (table) and column names, e.g.
    ``SELECT * FROM "<resource_id>" WHERE "Connection Site" ILIKE '%Norton%' LIMIT 10``.
    """

    ACTION: ClassVar[str] = "datastore_search_sql"  # providers subclass this and set URL = BASE_URL + ACTION
    METHOD: ClassVar[str] = "GET"

    sql: str


# ----------------------------------------- 2. Response ------------------------------------------ #


class CkanError(ApiResponse):
    """CKAN error body.

    Besides ``__type``/``message``, validation errors add one key per
    offending field, e.g. ``{"resource_id": ["Missing value"]}``. Those keys are our own request
    field names, so extras are allowed here, typed as message lists.
    """

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, list[str]] = Field(init=False)

    type: str | None = Field(default=None, alias="__type")  # e.g. "Not Found Error", "Validation Error"
    message: str | None = None


class CkanResponse(ApiResponse):
    """The CKAN action envelope. Each action's response adds ``result``, absent when ``success`` is false."""

    help: str  # URL of help_show for the action
    success: bool
    error: CkanError | None = None


class PackageShowResponse(CkanResponse):
    """Response of package_show. Unknown id -> HTTP 404, error.__type "Not Found Error"."""

    result: CkanPackage | None = None


class PackageSearchResponse(CkanResponse):
    """Response of package_search."""

    result: PackageSearchResult | None = None


class DatastoreSearchResponse[R](CkanResponse):
    """Response of datastore_search.

    Parametrise with the record model, e.g.
    ``DatastoreSearchResponse[TecRegisterRecord]`` or ``[dict[str, Any]]``.
    """

    result: DatastoreSearchResult[R] | None = None


class DatastoreSearchSqlResponse[R](CkanResponse):
    """Response of datastore_search_sql. Parametrise with the record model."""

    result: DatastoreSearchSqlResult[R] | None = None


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class CkanOrganization(ApiResponse):
    """Organization that publishes a CKAN package."""

    id: str
    name: str
    title: str
    type: str | None = None
    description: str | None = None
    image_url: str | None = None
    created: datetime | None = None
    is_organization: bool | None = None
    approval_status: str | None = None
    state: str | None = None
    revision_id: str | None = None


class CkanTag(ApiResponse):
    """Tag attached to a CKAN package."""

    id: str
    name: str
    display_name: str | None = None
    state: str | None = None
    vocabulary_id: str | None = None


class CkanExtra(ApiResponse):
    """Free key/value metadata; NESO uses key "Update Frequency"."""

    key: str
    value: str | None = None


class CkanResource(ApiResponse):
    """A file within a package.

    ``datastore_active`` tells whether datastore_search works
    for ``id``; otherwise only ``url`` (file download) is available.
    """

    id: str
    package_id: str
    name: str | None = None
    description: str | None = None
    format: str | None = None  # "CSV", "ZIP", "GeoJSON", "PDF", "PNG", ...
    url: str  # download URL; filename changes on each republish, the resource id does not
    url_type: str | None = None
    mimetype: str | None = None
    mimetype_inner: str | None = None
    size: int | None = None
    hash: str | None = None
    state: str | None = None
    position: int | None = None
    resource_type: str | None = None
    datastore_active: bool | None = None
    datastore_append_or_update: bool | None = None
    datastore_unique_keys: list[str] | None = None  # NESO EAC order resources, e.g. ["orderID"]
    created: datetime | None = None  # naive UTC
    last_modified: datetime | None = None
    metadata_modified: datetime | None = None
    cache_url: str | None = None
    cache_last_updated: datetime | None = None
    revision_id: str | None = None


class CkanPackage(ApiResponse):
    """A CKAN dataset ("package") as returned by package_show / package_search."""

    id: str
    name: str  # slug, usable as package_show id
    title: str
    notes: str | None = None  # description, markdown/HTML
    type: str | None = None
    state: str | None = None
    private: bool | None = None
    isopen: bool | None = None
    url: str | None = None
    version: str | None = None
    author: str | None = None
    author_email: str | None = None
    maintainer: str | None = None
    maintainer_email: str | None = None
    license_id: str | None = None
    license_title: str | None = None
    license_url: str | None = None
    owner_org: str | None = None
    organization: CkanOrganization | None = None
    creator_user_id: str | None = None
    revision_id: str | None = None
    metadata_created: datetime | None = None  # naive UTC
    metadata_modified: datetime | None = None
    num_resources: int | None = None
    num_tags: int | None = None
    resources: list[CkanResource] = Field(default_factory=list)
    tags: list[CkanTag] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)  # always empty on NESO
    extras: list[CkanExtra] = Field(default_factory=list)
    relationships_as_subject: list[dict[str, Any]] = Field(default_factory=list)
    relationships_as_object: list[dict[str, Any]] = Field(default_factory=list)


class CkanFacetItem(ApiResponse):
    """One value of a search facet with its match count."""

    name: str
    display_name: str
    count: int


class CkanSearchFacet(ApiResponse):
    """A package_search facet: its title and value counts."""

    title: str
    items: list[CkanFacetItem]


class PackageSearchResult(ApiResponse):
    """The ``result`` object of package_search: matching packages and facets."""

    count: int  # total matches, not len(results)
    results: list[CkanPackage]
    sort: str | None = None
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)  # deprecated form: {field: {value: count}}
    search_facets: dict[str, CkanSearchFacet] = Field(default_factory=dict)


class DatastoreFieldInfo(ApiResponse):
    """Data-dictionary entry for a column (NESO fills these in; all free text)."""

    title: str | None = None
    description: str | None = None
    comment: str | None = None
    example: str | None = None
    unit: str | None = None
    type: str | None = None  # NESO's own label: "string", "number", "date"
    label: str | None = None  # stock CKAN data-dictionary keys, not seen on NESO
    notes: str | None = None
    type_override: str | None = None


class DatastoreField(ApiResponse):
    """A datastore column: name, type and optional data-dictionary info."""

    id: str  # exact column name
    # datastore_search: "int", "text", "numeric", "date", "timestamp", ...
    # datastore_search_sql: raw postgres names, e.g. "int4", "tsvector"
    type: str
    info: DatastoreFieldInfo | None = None  # absent for _id and in SQL results


class DatastoreLinks(ApiResponse):
    """Relative URLs (start with /api/3/action/...)."""

    start: str
    next: str | None = None
    prev: str | None = None


class DatastoreSearchResult[R](ApiResponse):
    """The ``result`` object of datastore_search: records of type ``R`` plus paging info."""

    resource_id: str
    fields: list[DatastoreField]  # NESO returns ALL columns here even when `fields` projects records
    records: list[R] | str  # str when records_format is csv/tsv
    records_format: Literal["objects", "lists", "csv", "tsv"] | None = None
    include_total: bool | None = None
    total: int | None = None  # absent if include_total=false
    total_was_estimated: bool | None = None
    total_estimation_threshold: int | None = None  # echoed by newer CKAN (NGED)
    limit: int | None = None
    offset: int | None = None  # echoed only when sent
    q: str | dict[str, str] | None = None  # echoed only when sent
    filters: dict[str, Any] | None = None  # echoed only when sent
    sort: str | None = None  # echoed only when sent
    distinct: bool | None = None
    links: DatastoreLinks | None = Field(default=None, alias="_links")


class DatastoreSearchSqlResult[R](ApiResponse):
    """The ``result`` object of datastore_search_sql: records of type ``R``."""

    sql: str  # echo of the query
    fields: list[DatastoreField]  # postgres type names, no `info`
    records: list[R]  # SELECT * also returns `_full_text` (tsvector string)
    records_truncated: bool | None = None  # present+true only if the 32000-row cap was hit
