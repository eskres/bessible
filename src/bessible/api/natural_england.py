"""Natural England open-data ArcGIS feature services (environmental designations).

Base: https://services.arcgis.com/JJzESW51TqeY9uat/ArcGIS/rest/services  (``?f=json`` lists services)
Query docs: https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/

Notes (verified live 2026-09):
- Errors come back as HTTP 200 with body ``{"error": {...}}`` -> check with ``ArcGisErrorResponse``.
- With ``f=geojson``: ``exceededTransferLimit`` and ``count`` live under top-level ``properties``;
  ``crs`` is present on normal results; ``geometry`` is null when ``returnGeometry=false``.
- Esri date fields (esriFieldTypeDate) arrive as epoch-milliseconds ints in geojson
  (only ``NationalParkProps.desig_date`` among these layers); use ``esri_date()`` to convert.
- Native CRS of all layers is EPSG:27700; AREA/Shape__* values are in metres / m2 / ha as commented.
- No Green Belt service exists on this server (it is an MHCLG dataset, see planning.data.gov.uk).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, NamedTuple
from urllib.parse import quote

from pydantic import Field

from .base import ApiRequest, ApiResponse

BASE_URL = "https://services.arcgis.com/JJzESW51TqeY9uat/ArcGIS/rest/services"

GeometryType = Literal[
    "esriGeometryPoint",
    "esriGeometryMultipoint",
    "esriGeometryPolyline",
    "esriGeometryPolygon",
    "esriGeometryEnvelope",
]

SpatialRel = Literal[
    "esriSpatialRelIntersects",
    "esriSpatialRelContains",
    "esriSpatialRelCrosses",
    "esriSpatialRelEnvelopeIntersects",
    "esriSpatialRelIndexIntersects",
    "esriSpatialRelOverlaps",
    "esriSpatialRelTouches",
    "esriSpatialRelWithin",
]

DistanceUnits = Literal[
    "esriSRUnit_Meter",
    "esriSRUnit_StatuteMile",
    "esriSRUnit_Foot",
    "esriSRUnit_Kilometer",
    "esriSRUnit_NauticalMile",
    "esriSRUnit_USNauticalMile",
]

ResponseFormat = Literal["geojson", "json", "pbf", "html"]

SERVICE_ALC_PROVISIONAL = "Provisional Agricultural Land Classification (ALC) (England)"  # url() percent-encodes

SERVICE_ALC_POST_1988 = "Agricultural_Land_Classification_Post_1988"

SERVICE_ANCIENT_WOODLAND = "Ancient_Woodland_England"

SERVICE_ANCIENT_WOODLAND_REVISED = "Ancient_Woodland_Revised_England"

SERVICE_SSSI = "SSSI_England"

SERVICE_SSSI_IRZ = "SSSI_Impact_Risk_Zones_England"

SERVICE_SAC = "Special_Areas_of_Conservation_England"

SERVICE_SPA = "Special_Protection_Areas_England"

SERVICE_RAMSAR = "Ramsar_England"

SERVICE_NNR = "National_Nature_Reserves_England"

SERVICE_LNR = "Local_Nature_Reserves_England"

SERVICE_AONB = "Areas_of_Outstanding_Natural_Beauty_England"

SERVICE_NATIONAL_PARKS = "National_Parks_England"

SERVICE_PRIORITY_HABITATS = "Priority_Habitats_Inventory_England"


# ------------------------------------------ 1. Request ------------------------------------------ #


class ArcGisQueryRequest(ApiRequest):
    """Feature query against one layer: GET {BASE}/{service}/FeatureServer/{layer}/query.

    https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/
    """

    URL: ClassVar[str] = BASE_URL + "/{service}/FeatureServer/{layer}/query"
    METHOD: ClassVar[str] = "GET"

    # path params (excluded from params())
    service: str
    layer: int = 0

    where: str | None = None  # SQL-92 where clause, e.g. "1=1"
    geometry: str | None = None  # simple syntax "x,y" (point) or "xmin,ymin,xmax,ymax" (envelope), or Esri JSON
    geometry_type: GeometryType | None = Field(default=None, serialization_alias="geometryType")
    in_sr: int | None = Field(default=None, serialization_alias="inSR")  # WKID of input geometry, e.g. 4326
    spatial_rel: SpatialRel | None = Field(default=None, serialization_alias="spatialRel")
    distance: float | None = None  # buffer around input geometry, in `units`
    units: DistanceUnits | None = None
    out_fields: str | None = Field(default=None, serialization_alias="outFields")  # comma-separated or "*"
    return_geometry: bool | None = Field(default=None, serialization_alias="returnGeometry")
    out_sr: int | None = Field(default=None, serialization_alias="outSR")  # f=geojson defaults to 4326
    result_record_count: int | None = Field(default=None, serialization_alias="resultRecordCount")
    result_offset: int | None = Field(default=None, serialization_alias="resultOffset")
    return_count_only: bool | None = Field(default=None, serialization_alias="returnCountOnly")
    order_by_fields: str | None = Field(default=None, serialization_alias="orderByFields")  # e.g. "NAME ASC"
    geometry_precision: int | None = Field(default=None, serialization_alias="geometryPrecision")  # decimal places
    max_allowable_offset: float | None = Field(
        default=None, serialization_alias="maxAllowableOffset"
    )  # simplification tolerance, outSR units
    f: ResponseFormat = "geojson"

    def url(self) -> str:
        """Return the layer's query endpoint URL."""
        return _layer_url(self.service, self.layer) + "/query"

    def params(self) -> dict[str, Any]:
        """Return the query-string parameters (aliased, nulls dropped, path params excluded)."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"service", "layer"})


class LayerMetadataRequest(ApiRequest):
    """Layer metadata request: GET {BASE}/{service}/FeatureServer/{layer}?f=json.

    https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/
    """

    URL: ClassVar[str] = BASE_URL + "/{service}/FeatureServer/{layer}"
    METHOD: ClassVar[str] = "GET"

    service: str
    layer: int = 0
    f: Literal["json"] = "json"

    def url(self) -> str:
        """Return the layer's metadata endpoint URL."""
        return _layer_url(self.service, self.layer)

    def params(self) -> dict[str, Any]:
        """Return the query-string parameters (just the response format)."""
        return {"f": self.f}


# ----------------------------------------- 2. Response ------------------------------------------ #


class ArcGisGeoJsonResponse[PropsT](ApiResponse):
    """f=geojson query result: a GeoJSON FeatureCollection."""

    type: Literal["FeatureCollection"]
    crs: GeoJsonCrs | None = None
    properties: ArcGisCollectionProperties | None = None
    # Documented at top level for f=json; in geojson it is seen under `properties`. Kept for safety.
    exceeded_transfer_limit: bool | None = Field(default=None, alias="exceededTransferLimit")
    features: list[ArcGisFeature[PropsT]]


class ArcGisCountResponse(ApiResponse):
    """returnCountOnly=true with f=geojson: {"type":"FeatureCollection","properties":{"count":N},"features":[]}.

    (With f=json the body is just {"count": N}; both shapes are accepted.)
    """

    type: Literal["FeatureCollection"] | None = None
    properties: ArcGisCountProperties | None = None
    features: list[Any] | None = None  # always [] for count-only queries
    count: int | None = None  # f=json shape


class ArcGisError(ApiResponse):
    """Payload of the `error` key in an ArcGIS error body."""

    code: int
    message: str  # often ""
    details: list[str] = Field(default_factory=list)


class ArcGisErrorResponse(ApiResponse):
    """Error body — returned with HTTP 200, so check for the `error` key before parsing results."""

    error: ArcGisError


class LayerMetadataResponse(ApiResponse):
    """Layer description (large; the useful keys come first)."""

    id: int
    name: str
    type: str  # "Feature Layer"
    current_version: float | None = Field(default=None, alias="currentVersion")
    service_item_id: str | None = Field(default=None, alias="serviceItemId")
    description: str | None = None
    copyright_text: str | None = Field(default=None, alias="copyrightText")
    display_field: str | None = Field(default=None, alias="displayField")
    geometry_type: str | None = Field(default=None, alias="geometryType")
    object_id_field: str | None = Field(default=None, alias="objectIdField")
    global_id_field: str | None = Field(default=None, alias="globalIdField")
    extent: Extent | None = None
    fields: list[LayerField]
    max_record_count: int | None = Field(default=None, alias="maxRecordCount")
    standard_max_record_count: int | None = Field(default=None, alias="standardMaxRecordCount")
    supported_query_formats: str | None = Field(default=None, alias="supportedQueryFormats")
    capabilities: str | None = None
    has_z: bool | None = Field(default=None, alias="hasZ")
    has_m: bool | None = Field(default=None, alias="hasM")
    # Everything below is unused by us but modelled so that extra="forbid" holds; nested blobs stay raw.
    cache_max_age: int | None = Field(default=None, alias="cacheMaxAge")
    default_visibility: bool | None = Field(default=None, alias="defaultVisibility")
    editing_info: dict[str, Any] | None = Field(default=None, alias="editingInfo")
    relationships: list[dict[str, Any]] | None = None
    is_data_versioned: bool | None = Field(default=None, alias="isDataVersioned")
    has_contingent_values_definition: bool | None = Field(default=None, alias="hasContingentValuesDefinition")
    supports_append: bool | None = Field(default=None, alias="supportsAppend")
    supports_calculate: bool | None = Field(default=None, alias="supportsCalculate")
    supports_async_calculate: bool | None = Field(default=None, alias="supportsASyncCalculate")
    supports_truncate: bool | None = Field(default=None, alias="supportsTruncate")
    supports_attachments_by_upload_id: bool | None = Field(default=None, alias="supportsAttachmentsByUploadId")
    supports_attachments_resizing: bool | None = Field(default=None, alias="supportsAttachmentsResizing")
    supports_rollback_on_failure_parameter: bool | None = Field(
        default=None, alias="supportsRollbackOnFailureParameter"
    )
    supports_statistics: bool | None = Field(default=None, alias="supportsStatistics")
    supports_exceeds_limit_statistics: bool | None = Field(default=None, alias="supportsExceedsLimitStatistics")
    supports_advanced_queries: bool | None = Field(default=None, alias="supportsAdvancedQueries")
    supports_validate_sql: bool | None = Field(default=None, alias="supportsValidateSql")
    supports_coordinates_quantization: bool | None = Field(default=None, alias="supportsCoordinatesQuantization")
    supports_layer_overrides: bool | None = Field(default=None, alias="supportsLayerOverrides")
    supports_tiles_and_basic_queries_mode: bool | None = Field(default=None, alias="supportsTilesAndBasicQueriesMode")
    supports_field_description_property: bool | None = Field(default=None, alias="supportsFieldDescriptionProperty")
    supports_quantization_edit_mode: bool | None = Field(default=None, alias="supportsQuantizationEditMode")
    supports_column_store_index: bool | None = Field(default=None, alias="supportsColumnStoreIndex")
    supports_apply_edits_with_global_ids: bool | None = Field(default=None, alias="supportsApplyEditsWithGlobalIds")
    supports_multi_scale_geometry: bool | None = Field(default=None, alias="supportsMultiScaleGeometry")
    supports_returning_query_geometry: bool | None = Field(default=None, alias="supportsReturningQueryGeometry")
    enable_null_geometry: bool | None = Field(default=None, alias="enableNullGeometry")
    has_geometry_properties: bool | None = Field(default=None, alias="hasGeometryProperties")
    geometry_properties: dict[str, Any] | None = Field(default=None, alias="geometryProperties")
    advanced_query_capabilities: dict[str, Any] | None = Field(default=None, alias="advancedQueryCapabilities")
    advanced_query_analytic_capabilities: dict[str, Any] | None = Field(
        default=None, alias="advancedQueryAnalyticCapabilities"
    )
    query_bins_capabilities: dict[str, Any] | None = Field(default=None, alias="queryBinsCapabilities")
    supported_operations_with_collation: str | None = Field(default=None, alias="supportedOperationsWithCollation")
    advanced_editing_capabilities: dict[str, Any] | None = Field(default=None, alias="advancedEditingCapabilities")
    info_in_estimates: list[str] | None = Field(default=None, alias="infoInEstimates")
    use_standardized_queries: bool | None = Field(default=None, alias="useStandardizedQueries")
    min_scale: float | None = Field(default=None, alias="minScale")
    max_scale: float | None = Field(default=None, alias="maxScale")
    spatial_reference: SpatialReference | None = Field(default=None, alias="spatialReference")
    drawing_info: dict[str, Any] | None = Field(default=None, alias="drawingInfo")
    allow_geometry_updates: bool | None = Field(default=None, alias="allowGeometryUpdates")
    true_curve_support_mode: str | None = Field(default=None, alias="trueCurveSupportMode")
    supported_curve_types: list[str] | None = Field(default=None, alias="supportedCurveTypes")
    supported_true_curve_pbf_feature_encodings: list[str] | None = Field(
        default=None, alias="supportedTrueCurvePbfFeatureEncodings"
    )
    allow_true_curves_updates: bool | None = Field(default=None, alias="allowTrueCurvesUpdates")
    only_allow_true_curve_updates_by_true_curve_clients: bool | None = Field(
        default=None, alias="onlyAllowTrueCurveUpdatesByTrueCurveClients"
    )
    has_attachments: bool | None = Field(default=None, alias="hasAttachments")
    html_popup_type: str | None = Field(default=None, alias="htmlPopupType")
    unique_id_field: dict[str, Any] | None = Field(default=None, alias="uniqueIdField")
    type_id_field: str | None = Field(default=None, alias="typeIdField")
    collation: dict[str, Any] | None = None
    indexes: list[dict[str, Any]] | None = None
    date_fields_time_reference: dict[str, Any] | None = Field(default=None, alias="dateFieldsTimeReference")
    preferred_time_reference: dict[str, Any] | None = Field(default=None, alias="preferredTimeReference")
    types: list[dict[str, Any]] | None = None
    templates: list[dict[str, Any]] | None = None
    supported_append_formats: str | None = Field(default=None, alias="supportedAppendFormats")
    supported_append_source_filter_formats: str | None = Field(default=None, alias="supportedAppendSourceFilterFormats")
    supported_export_formats: str | None = Field(default=None, alias="supportedExportFormats")
    supported_convert_file_formats: str | None = Field(default=None, alias="supportedConvertFileFormats")
    supported_convert_content_formats: str | None = Field(default=None, alias="supportedConvertContentFormats")
    supported_spatial_relationships: list[str] | None = Field(default=None, alias="supportedSpatialRelationships")
    guid_format: str | None = Field(default=None, alias="guidFormat")
    supports_contingent_values: bool | None = Field(default=None, alias="supportsContingentValues")
    supports_editing_contingent_values: bool | None = Field(default=None, alias="supportsEditingContingentValues")
    supported_contingent_values_formats: str | None = Field(default=None, alias="supportedContingentValuesFormats")
    supports_field_groups: bool | None = Field(default=None, alias="supportsFieldGroups")
    supported_sync_data_options: int | None = Field(default=None, alias="supportedSyncDataOptions")
    has_static_data: bool | None = Field(default=None, alias="hasStaticData")
    max_ids_count: int | None = Field(default=None, alias="maxIdsCount")
    standard_max_record_count_no_geometry: int | None = Field(default=None, alias="standardMaxRecordCountNoGeometry")
    tile_max_record_count: int | None = Field(default=None, alias="tileMaxRecordCount")
    max_record_count_factor: float | None = Field(default=None, alias="maxRecordCountFactor")


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class GeoJsonGeometry(ApiResponse):
    """GeoJSON geometry. Polygon layers return both Polygon and MultiPolygon."""

    type: Literal["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]
    coordinates: list[Any]  # nesting depth depends on `type`; [lon, lat] when outSR=4326


class ArcGisFeature[PropsT](ApiResponse):
    """GeoJSON Feature whose `properties` are typed by the layer's props model."""

    type: Literal["Feature"]
    id: int | str | None = None  # the layer's object id
    geometry: GeoJsonGeometry | None = None  # null when returnGeometry=false
    properties: PropsT


class GeoJsonCrsProperties(ApiResponse):
    """Properties of a GeoJSON named CRS."""

    name: str  # e.g. "EPSG:4326"


class GeoJsonCrs(ApiResponse):
    """GeoJSON `crs` member naming the output coordinate reference system."""

    type: str  # "name"
    properties: GeoJsonCrsProperties


class ArcGisCollectionProperties(ApiResponse):
    """Top-level FeatureCollection `properties` (only present when there is something to say)."""

    exceeded_transfer_limit: bool | None = Field(default=None, alias="exceededTransferLimit")
    count: int | None = None  # returnCountOnly=true


class ArcGisCountProperties(ApiResponse):
    """FeatureCollection `properties` of a count-only geojson result."""

    count: int


class SpatialReference(ApiResponse):
    """Esri spatial reference (WKID pair)."""

    wkid: int | None = None
    latest_wkid: int | None = Field(default=None, alias="latestWkid")


class Extent(ApiResponse):
    """Bounding box of a layer in its spatial reference."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    spatial_reference: SpatialReference | None = Field(default=None, alias="spatialReference")


class CodedValue(ApiResponse):
    """One code/label pair of a coded-value field domain."""

    name: str
    code: str | int | float


class FieldDomain(ApiResponse):
    """Allowed values of a layer field (coded values or a numeric range)."""

    type: str  # "codedValue" | "range"
    name: str | None = None
    coded_values: list[CodedValue] | None = Field(default=None, alias="codedValues")
    range: list[float] | None = None


class LayerField(ApiResponse):
    """Field definition from a layer's metadata."""

    name: str
    type: str  # esriFieldTypeOID/String/Integer/SmallInteger/Double/Date/GlobalID/...
    alias: str | None = None
    sql_type: str | None = Field(default=None, alias="sqlType")
    length: int | None = None  # strings, dates, GlobalID only
    precision: int | None = None  # numeric fields only
    nullable: bool | None = None
    editable: bool | None = None
    domain: FieldDomain | None = None
    default_value: Any | None = Field(default=None, alias="defaultValue")
    description: str | None = None


# Field lists from each layer's ?f=json metadata, confirmed against a live feature.
# All non-OID/GlobalID fields are nullable per metadata.


class _ShapeProps(ApiResponse):
    shape_area: float | None = Field(default=None, alias="Shape__Area")  # m2 (EPSG:27700)
    shape_length: float | None = Field(default=None, alias="Shape__Length")  # m


class AlcProvisionalProps(_ShapeProps):
    """Provisional ALC (pre-1988, 1:250k). Does not split Grade 3 into 3a/3b."""

    objectid: int = Field(alias="OBJECTID")
    geogext: str | None = Field(default=None, alias="GEOGEXT")
    area: float | None = Field(default=None, alias="AREA")  # ha
    # seen: "Grade 1".."Grade 5", "Non Agricultural", "Urban", "Exclusion"
    alc_grade: str | None = Field(default=None, alias="ALC_GRADE")
    perimeter: float | None = Field(default=None, alias="PERIMETER")  # m


class AlcPost1988Props(_ShapeProps):
    """Post-1988 detailed ALC surveys (patchy coverage). Splits 3a/3b."""

    objectid_1: int = Field(alias="OBJECTID_1")  # the real OID
    objectid: int | None = Field(default=None, alias="OBJECTID")
    geogext: str | None = Field(default=None, alias="GEOGEXT")
    job_number: str | None = Field(default=None, alias="JOB_NUMBER")
    rpt: str | None = Field(default=None, alias="RPT")  # reporting office
    alc_grade: str | None = Field(default=None, alias="ALC_GRADE")  # e.g. "Grade 3b"
    hectares: float | None = Field(default=None, alias="HECTARES")
    rpt_jobnum: str | None = Field(default=None, alias="RPT_JOBNUM")
    published: str | None = Field(default=None, alias="Published_")  # report URL


class AncientWoodlandProps(_ShapeProps):
    """Ancient Woodland Inventory polygon attributes."""

    objectid: int = Field(alias="OBJECTID")
    name: str | None = Field(default=None, alias="NAME")  # often " "
    theme: str | None = Field(default=None, alias="THEME")
    themname: str | None = Field(default=None, alias="THEMNAME")
    themid: float | None = Field(default=None, alias="THEMID")
    status: str | None = Field(default=None, alias="STATUS")  # "ASNW" | "PAWS" (others possible)
    perimeter: float | None = Field(default=None, alias="PERIMETER")  # m
    area: float | None = Field(default=None, alias="AREA")  # ha
    x_coord: int | None = Field(default=None, alias="X_COORD")  # BNG easting
    y_coord: int | None = Field(default=None, alias="Y_COORD")  # BNG northing
    global_id: str = Field(alias="GlobalID")


class AncientWoodlandRevisedProps(_ShapeProps):
    """Revised inventory (rolling county updates). Note THEMENAME/THEMEID spelling + str id."""

    objectid: int = Field(alias="OBJECTID")
    name: str | None = Field(default=None, alias="NAME")
    theme: str | None = Field(default=None, alias="THEME")
    themename: str | None = Field(default=None, alias="THEMENAME")
    status: str | None = Field(default=None, alias="STATUS")
    x_coord: int | None = Field(default=None, alias="X_COORD")
    y_coord: int | None = Field(default=None, alias="Y_COORD")
    themeid: str | None = Field(default=None, alias="THEMEID")  # e.g. "ESS-2501"
    area: float | None = Field(default=None, alias="AREA")  # ha
    perimeter: float | None = Field(default=None, alias="PERIMETER")  # km in sample (unlike unrevised layer)
    global_id: str = Field(alias="GlobalID")


class SssiProps(_ShapeProps):
    """Site of Special Scientific Interest polygon attributes."""

    objectid: int = Field(alias="OBJECTID")
    ref_code: str | None = Field(default=None, alias="REF_CODE")
    name: str | None = Field(default=None, alias="NAME")
    measure: float | None = Field(default=None, alias="MEASURE")  # ha
    label: str | None = Field(default=None, alias="LABEL")
    hyperlink: str | None = Field(default=None, alias="HYPERLINK")  # designated-sites site code, not a URL
    contact_no: str | None = Field(default=None, alias="CONTACT_NO")
    global_id: str = Field(alias="GlobalID")


class SssiImpactRiskZoneProps(_ShapeProps):
    """IRZ polygons carry only a URL; development-type rules are at that URL (irzcode/notes query args)."""

    objectid: int = Field(alias="OBJECTID")
    irzurl: str | None = Field(default=None, alias="IRZURL")  # contains a raw space; URL-encode before fetching
    global_id: str = Field(alias="GlobalID")


class _EuropeanSiteProps(_ShapeProps):
    objectid: int = Field(alias="OBJECTID")
    grid_ref: str | None = Field(default=None, alias="GRID_REF")
    easting: float | None = Field(default=None, alias="EASTING")
    northing: float | None = Field(default=None, alias="NORTHING")
    latitude: str | None = Field(default=None, alias="LATITUDE")  # DMS string e.g. "52:55:27N"
    longitude: str | None = Field(default=None, alias="LONGITUDE")
    status: str | None = Field(default=None, alias="STATUS")  # "Designated" / "Classified" / "Listed"
    id: float | None = Field(default=None, alias="ID")
    file: str | None = Field(default=None, alias="FILE_")
    easting0: float | None = Field(default=None, alias="EASTING0")
    northing0: float | None = Field(default=None, alias="NORTHING0")
    gis_date: str | None = Field(default=None, alias="GIS_DATE")  # string "YYYYMMDD", not an Esri date
    version: int | None = Field(default=None, alias="VERSION")
    global_id: str = Field(alias="GlobalID")


class SacProps(_EuropeanSiteProps):
    """Special Area of Conservation polygon attributes."""

    sac_name: str | None = Field(default=None, alias="SAC_NAME")
    sac_code: str | None = Field(default=None, alias="SAC_CODE")
    sac_area: float | None = Field(default=None, alias="SAC_AREA")  # ha
    name: str | None = Field(default=None, alias="NAME")  # legacy, usually ""
    area: float | None = Field(default=None, alias="AREA")


class SpaProps(_EuropeanSiteProps):
    """Special Protection Area polygon attributes."""

    spa_name: str | None = Field(default=None, alias="SPA_NAME")
    spa_code: str | None = Field(default=None, alias="SPA_CODE")
    spa_area: float | None = Field(default=None, alias="SPA_AREA")  # ha
    name: str | None = Field(default=None, alias="NAME")  # legacy, usually ""
    area: float | None = Field(default=None, alias="AREA")


class RamsarProps(_EuropeanSiteProps):
    """Ramsar wetland site polygon attributes."""

    name: str | None = Field(default=None, alias="NAME")
    code: str | None = Field(default=None, alias="CODE")
    area: float | None = Field(default=None, alias="AREA")  # ha
    name0: str | None = Field(default=None, alias="NAME0")  # legacy, usually ""
    area0: float | None = Field(default=None, alias="AREA0")


class NatureReserveProps(_ShapeProps):
    """National and Local Nature Reserves share a schema."""

    objectid: int = Field(alias="OBJECTID")
    hyperlink: str | None = Field(default=None, alias="HYPERLINK")  # null seen on LNR
    ref_code: str | None = Field(default=None, alias="REF_CODE")
    name: str | None = Field(default=None, alias="NAME")
    measure: float | None = Field(default=None, alias="MEASURE")  # ha
    label: str | None = Field(default=None, alias="LABEL")
    global_id: str = Field(alias="GlobalID")


class AonbProps(_ShapeProps):
    """AONB (now branded National Landscapes)."""

    objectid: int = Field(alias="OBJECTID")
    code: str | None = Field(default=None, alias="CODE")
    name: str | None = Field(default=None, alias="NAME")
    desig_date: str | None = Field(default=None, alias="DESIG_DATE")  # free string e.g. "Sep-63"
    hotlink: str | None = Field(default=None, alias="HOTLINK")
    stat_area: float | None = Field(default=None, alias="STAT_AREA")  # km2
    global_id: str = Field(alias="GlobalID")


class NationalParkProps(_ShapeProps):
    """National Park polygon attributes."""

    objectid: int = Field(alias="OBJECTID")
    code: int | None = Field(default=None, alias="CODE")
    name: str | None = Field(default=None, alias="NAME")
    measure: float | None = Field(default=None, alias="MEASURE")  # km2
    desig_date: int | None = Field(default=None, alias="DESIG_DATE")  # Esri date: epoch ms (UTC); see esri_date()
    hotlink: str | None = Field(default=None, alias="HOTLINK")
    status: str | None = Field(default=None, alias="STATUS")


class PriorityHabitatProps(_ShapeProps):
    """Priority Habitats Inventory (single England-wide service; millions of small polygons)."""

    objectid: int = Field(alias="OBJECTID")
    main_habs: str | None = Field(default=None, alias="MainHabs")
    hab_codes: str | None = Field(default=None, alias="HabCodes")
    feat_desc: str | None = Field(default=None, alias="FeatDesc")
    feat_codes: str | None = Field(default=None, alias="FeatCodes")
    other_class: str | None = Field(default=None, alias="OtherClass")
    add_habs: str | None = Field(default=None, alias="AddHabs")
    prim_source: str | None = Field(default=None, alias="PrimSource")
    area_ha: float | None = Field(default=None, alias="AreaHa")
    version: str | None = Field(default=None, alias="Version")  # e.g. "Sep_25"
    uid: str | None = Field(default=None, alias="UID")
    global_id: str = Field(alias="GlobalID")


# -------------------- 4. Not from the API (helpers, registries, transforms) --------------------- #


def esri_date(ms: int | None) -> datetime | None:
    """Convert an Esri epoch-milliseconds date value to an aware UTC datetime."""
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC)


def _layer_url(service: str, layer: int) -> str:
    return f"{BASE_URL}/{quote(service)}/FeatureServer/{layer}"


class LayerSpec[PropsT: ApiResponse](NamedTuple):
    """Registry entry tying a feature service layer to its typed response model."""

    service: str
    layer: int
    response: type[ArcGisGeoJsonResponse[PropsT]]

    @property
    def props(self) -> type[PropsT]:
        """The properties model of this layer's features."""
        props: type[PropsT] = self.response.__pydantic_generic_metadata__["args"][0]
        return props

    def at_point(self, lat: float, lon: float, distance_m: float | None = None) -> ArcGisQueryRequest:
        """Build a query for this layer's features at (or within `distance_m` of) a WGS84 point."""
        return query_at_point(self.service, lat, lon, distance_m, layer=self.layer)

    def in_bbox(
        self, min_lat: float, min_lon: float, max_lat: float, max_lon: float, distance_m: float | None = None
    ) -> ArcGisQueryRequest:
        """Build a query for this layer's features touching (or within `distance_m` of) a WGS84 box."""
        return query_in_bbox(self.service, min_lat, min_lon, max_lat, max_lon, distance_m, layer=self.layer)

    def parse(self, body: dict[str, Any]) -> ArcGisGeoJsonResponse[PropsT]:
        """Validate a geojson query body with features typed by this layer's props model."""
        return self.response.model_validate(body)


LAYERS: dict[str, LayerSpec[Any]] = {
    "alc_provisional": LayerSpec(SERVICE_ALC_PROVISIONAL, 0, ArcGisGeoJsonResponse[AlcProvisionalProps]),
    "alc_post_1988": LayerSpec(SERVICE_ALC_POST_1988, 0, ArcGisGeoJsonResponse[AlcPost1988Props]),
    "ancient_woodland": LayerSpec(SERVICE_ANCIENT_WOODLAND, 0, ArcGisGeoJsonResponse[AncientWoodlandProps]),
    "ancient_woodland_revised": LayerSpec(
        SERVICE_ANCIENT_WOODLAND_REVISED, 0, ArcGisGeoJsonResponse[AncientWoodlandRevisedProps]
    ),
    "sssi": LayerSpec(SERVICE_SSSI, 0, ArcGisGeoJsonResponse[SssiProps]),
    "sssi_irz": LayerSpec(SERVICE_SSSI_IRZ, 0, ArcGisGeoJsonResponse[SssiImpactRiskZoneProps]),
    "sac": LayerSpec(SERVICE_SAC, 0, ArcGisGeoJsonResponse[SacProps]),
    "spa": LayerSpec(SERVICE_SPA, 0, ArcGisGeoJsonResponse[SpaProps]),
    "ramsar": LayerSpec(SERVICE_RAMSAR, 0, ArcGisGeoJsonResponse[RamsarProps]),
    "nnr": LayerSpec(SERVICE_NNR, 0, ArcGisGeoJsonResponse[NatureReserveProps]),
    "lnr": LayerSpec(SERVICE_LNR, 0, ArcGisGeoJsonResponse[NatureReserveProps]),
    "aonb": LayerSpec(SERVICE_AONB, 0, ArcGisGeoJsonResponse[AonbProps]),
    "national_parks": LayerSpec(SERVICE_NATIONAL_PARKS, 0, ArcGisGeoJsonResponse[NationalParkProps]),
    # Republished Sep 2026 with its only layer at id 1 (id 0 now answers 400)
    "priority_habitats": LayerSpec(SERVICE_PRIORITY_HABITATS, 1, ArcGisGeoJsonResponse[PriorityHabitatProps]),
}


def query_at_point(
    service: str, lat: float, lon: float, distance_m: float | None = None, layer: int = 0
) -> ArcGisQueryRequest:
    """Request the features intersecting a WGS84 point (or within `distance_m` metres of it)."""
    return ArcGisQueryRequest(
        service=service,
        layer=layer,
        geometry=f"{lon},{lat}",
        geometry_type="esriGeometryPoint",
        in_sr=4326,
        spatial_rel="esriSpatialRelIntersects",
        distance=distance_m,
        units="esriSRUnit_Meter" if distance_m is not None else None,
        out_fields="*",
        return_geometry=True,
        out_sr=4326,
    )


def query_in_bbox(  # ruff: ignore[too-many-arguments, too-many-positional-arguments] - a box is four numbers
    service: str,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    distance_m: float | None = None,
    layer: int = 0,
) -> ArcGisQueryRequest:
    """Request the features touching a WGS84 box (or within `distance_m` metres of it), e.g. a site's extent."""
    return ArcGisQueryRequest(
        service=service,
        layer=layer,
        geometry=f"{min_lon},{min_lat},{max_lon},{max_lat}",
        geometry_type="esriGeometryEnvelope",
        in_sr=4326,
        spatial_rel="esriSpatialRelIntersects",
        distance=distance_m,
        units="esriSRUnit_Meter" if distance_m is not None else None,
        out_fields="*",
        return_geometry=True,
        out_sr=4326,
    )


def exceeded_transfer_limit(response: ArcGisGeoJsonResponse[Any]) -> bool:
    """Whether the server truncated the result (the flag can sit at the top level or under `properties`)."""
    nested = response.properties.exceeded_transfer_limit if response.properties else None
    return bool(nested or response.exceeded_transfer_limit)


def feature_count(response: ArcGisCountResponse) -> int:
    """Feature count from whichever count-only response shape was returned."""
    if response.properties is not None:
        return response.properties.count
    if response.count is None:
        msg = "no count in response"
        raise ValueError(msg)
    return response.count
