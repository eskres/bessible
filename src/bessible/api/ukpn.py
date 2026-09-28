"""UK Power Networks open data portal (Opendatasoft Explore API v2.1).

Portal: https://ukpowernetworks.opendatasoft.com  (138 datasets; the BESS-relevant ones are in DATASETS)
API docs: https://help.opendatasoft.com/apis/ods-explore-v2/

UKPN is the DNO for London (LPN), the South East (SPN) and the East of England (EPN) only; points
elsewhere in GB simply return no records.

Notes (verified live 2026-09):
- Auth: header ``Authorization: Apikey <key>`` (see ``auth_headers``). A bad key is HTTP 401; the
  catalogue is also readable anonymously but with lower quotas.
- ``limit`` is capped at 100 per call; page with ``offset`` (offset + limit <= 10000).
- Errors are HTTP 400 with ``{"error_code": ..., "message": ...}`` -> ``OdsError``.
- Record fields follow each dataset's schema exactly; "not applicable" in text columns is "-".
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from pydantic import Field

if TYPE_CHECKING:
    from typing import Any

from .base import ApiResponse
from .opendatasoft import DatasetSpec, GeoPoint, GeoShape, RecordsResponse

BASE_URL = "https://ukpowernetworks.opendatasoft.com/api/explore/v2.1"


# ------------------------------------------ 1. Request ------------------------------------------ #

# `opendatasoft.RecordsRequest` with `base_url=BASE_URL`; build one with `DATASETS[name].near(...)` (section 4).


# ----------------------------------------- 2. Response ------------------------------------------ #

# `opendatasoft.RecordsResponse[<row model>]`; each dataset's row model is in section 3.


# ------------------------------------ 3. Response sub-models ------------------------------------ #


class GridPrimarySite(ApiResponse):
    """Row of ``grid-and-primary-sites``: every grid (132/66 kV) and primary (33-11 kV) substation."""

    sitefunctionallocation: str | None = None  # site id shared across UKPN datasets, e.g. "EPN-S0000000C1068"
    licencearea: str | None = None  # "Eastern Power Networks (EPN)" | "... (SPN)" | "... (LPN)"
    sitename: str | None = None
    sitetype: str | None = None  # "Grid Substation" | "Primary Substation"
    sitevoltage: int | None = None  # kV, highest on site
    gridref: str | None = None  # OS grid reference
    street: str | None = None
    suburb: str | None = None
    towncity: str | None = None
    postcode: str | None = None
    county: str | None = None
    datecommissioned: str | None = None  # typed as a date upstream but holds junk like "01-01-15"
    powertransformercount: int | None = None
    maxdemandsummer: float | None = None  # MVA, LTDS table 3a
    maxdemandwinter: float | None = None
    transratingsummer: str | None = None  # firm capacity of the transformers (text upstream)
    transratingwinter: str | None = None
    reversepower: str | None = None
    # earthing study
    assessmentdate: date | None = None
    measuredresistance_ohm: float | None = None
    calculatedresistance: str | None = None
    last_report: str | None = None
    siteclassification: str | None = None  # "HOT" | "COLD" (earth potential rise)
    spatial_coordinates: GeoPoint | None = None
    local_authority: str | None = None
    local_authority_code: str | None = None  # ONS code
    what3words: str | None = None


class CapacityHeatmapSite(ApiResponse):
    """Row of ``ukpn-capacity-heatmap``: LTDS headroom per substation. Capacities in MW (MVA for firm).

    The BESS-relevant numbers are ``generationavailablecapacity`` (export headroom),
    ``demandavailablecapacity`` (import headroom) and the two RED/AMBER/GREEN constraint flags.
    """

    name: str | None = None  # substation name as in the LTDS
    mrid: str | None = None  # persistent record id
    description: str | None = None
    type: str | None = None  # "Primary" | "Grid" ...
    area: str | None = None  # EPN | SPN | LPN
    bsp: str | None = None  # bulk supply point feeding it
    gsp: str | None = None  # grid supply point (transmission interface)
    latitude: float | None = None
    longitude: float | None = None
    voltages: float | None = None  # kV
    voltage: float | None = None  # proposed connection voltage
    demandavailablecapacity: float | None = None
    demandconstraint: str | None = None  # RED | AMBER | GREEN
    demandconstraintlimitingfactor: str | None = None  # e.g. "Thermal"
    demandfirmcapacity: float | None = None
    demandmaximum: float | None = None
    demandminimum: float | None = None
    generationavailablecapacity: float | None = None
    generationconstraint: str | None = None  # RED | AMBER | GREEN
    generationconstraintlimitingfactor: str | None = None
    generationfirmcapacity: float | None = None
    # connection pipeline at this substation
    generationbudgetestimatesprovidedcapacity: float | None = None
    generationbudgetestimatesprovidedcount: int | None = None
    generationconnectionoffersacceptedcapacity: float | None = None
    generationconnectionoffersacceptedcount: int | None = None
    generationconnectionoffersmadecapacity: float | None = None
    generationconnectionoffersmadecount: int | None = None
    loadbudgetestimatesprovidedcapacity: float | None = None
    loadbudgetestimatesprovidedcount: int | None = None
    loadconnectionoffersacceptedcapacity: float | None = None
    loadconnectionoffersacceptedcount: int | None = None
    loadconnectionoffersmadecapacity: float | None = None
    loadconnectionoffersmadecount: int | None = None
    reversepowerflowavailablecapacity: float | None = None
    reversepowerflowtotalcapacity: float | None = None
    geometry_wkt_epsg4326: GeoShape | None = None
    geo_point_2d: GeoPoint | None = None


class EmbeddedCapacityRecord(ApiResponse):
    """Row of ``ukpn-embedded-capacity-register``: connected/accepted generation and storage >= 1 MW.

    Ofgem-mandated register (same columns at every DNO). Filter storage with
    ``energy_source_1 = "Stored Energy"``.
    """

    export_mpan_msid: str | None = None
    import_mpan_msid: str | None = None
    customer_name: str | None = None
    customer_site: str | None = None  # project name
    address_line_1: str | None = None
    address_line_2: str | None = None
    town_city: str | None = None
    county: str | None = None
    postcode: str | None = None
    country: str | None = None
    eastings: str | None = Field(default=None, alias="location_x_coordinate_eastings_where_data_is_held")  # EPSG:27700
    northings: str | None = Field(default=None, alias="location_y_coordinate_northings_where_data_is_held")
    grid_supply_point: str | None = None
    bulk_supply_point: str | None = None
    primary: str | None = None  # primary substation it connects through
    poc_voltage_kv: float | None = Field(default=None, alias="point_of_connection_poc_voltage_kv")
    licence_area: str | None = None
    # up to three technologies per site; capacities 2 and 3 are text upstream ("-" when unused)
    energy_source_1: str | None = None  # e.g. "Solar", "Stored Energy"
    energy_conversion_technology_1: str | None = None  # e.g. "Photovoltaic", "Storage (Battery)"
    chp_cogeneration_yes_no: str | None = None
    storage_capacity_1_mwh: str | None = None
    storage_duration_1_hours: str | None = None
    registered_capacity_1_mw: float | None = Field(
        default=None, alias="energy_source_energy_conversion_technology_1_registered_capacity_mw"
    )
    energy_source_2: str | None = None
    energy_conversion_technology_2: str | None = None
    chp_cogeneration_2_yes_no: str | None = None
    storage_capacity_2_mwh: str | None = None
    storage_duration_2_hours: str | None = None
    registered_capacity_2_mw: str | None = Field(
        default=None, alias="energy_source_energy_conversion_technology_2_registered_capacity_mw"
    )
    energy_source_3: str | None = None
    energy_conversion_technology_3: str | None = None
    chp_cogeneration_3_yes_no: str | None = None
    storage_capacity_3_mwh: str | None = None
    storage_duration_3_hours: str | None = None
    registered_capacity_3_mw: str | None = Field(
        default=None, alias="energy_source_energy_conversion_technology_3_registered_capacity_mw"
    )
    flexible_connection_yes_no: str | None = None
    connection_status: str | None = None  # "Connected" | "Accepted to Connect"
    already_connected_registered_capacity_mw: float | None = None
    maximum_export_capacity_mw: float | None = None
    maximum_export_capacity_mva: float | None = None
    maximum_import_capacity_mw: float | None = None
    maximum_import_capacity_mva: float | None = None
    date_connected: date | None = None
    accepted_to_connect_registered_capacity_mw: float | None = None
    change_to_maximum_export_capacity_mw: float | None = None
    change_to_maximum_export_capacity_mva: float | None = None
    change_to_maximum_import_capacity_mw: float | None = None
    change_to_maximum_import_capacity_mva: float | None = None
    date_accepted: date | None = None
    target_energisation_date: date | None = None
    distribution_service_provider_y_n: str | None = None
    transmission_service_provider_y_n: str | None = None
    reference: str | None = None
    in_a_connection_queue_y_n: str | None = None
    distribution_reinforcement_reference: str | None = None
    transmission_reinforcement_reference: str | None = None
    last_updated: date | None = None
    longitude: float | None = None
    latitude: float | None = None
    spatialcoordinates_customer: GeoPoint | None = None
    sitefunctionallocation: str | None = None  # joins to GridPrimarySite
    primary_resource_type_group: str | None = None  # e.g. "Solar PV", "Battery Storage"
    local_authority: str | None = None
    local_authority_county: str | None = None


class OverheadLine(ApiResponse):
    """Row of ``ukpn-132kv-overhead-lines`` / ``ukpn-33kv-overhead-lines``: one line segment."""

    id: str | None = None  # 33 kV dataset only
    voltage: str | None = None  # "132kV" | "33kV"
    dno: str | None = None  # EPN | SPN | LPN
    local_authority: str | None = None
    geo_shape: GeoShape | None = None  # LineString / MultiLineString
    geo_point_2d: GeoPoint | None = None  # segment midpoint


class Table6InterestRecord(ApiResponse):
    """Row of ``ukpn-ltds-table-6-interest-connections``: new connection enquiries and offers."""

    gridsupplypoint: str | None = None
    substation: str | None = None
    proposed_connection_voltage_kv: str | None = None
    status_of_connection: str | None = None
    demand_numbers_received_total_number: float | None = None
    demand_numbers_received_total_capacity: float | None = None
    generation_numbers_received_total_number: float | None = None
    generation_numbers_received_total_capacity: float | None = None
    spatial_coordinates: GeoPoint | None = None
    sitefunctionallocation: str | None = None
    licencearea: str | None = None
    id: int | None = None


class Table2aTransformerRecord(ApiResponse):
    """Row of ``ukpn-ltds-table-2a-transformer-2w``: transformer ratings and reverse power capability."""

    gridsupplypoint: str | None = None
    hv_node: str | None = None
    hv_substation: str | None = None
    voltage_hv: float | None = None
    lv_node: str | None = None
    lv_substation: str | None = None
    voltage_lv: float | None = None
    vector_group: str | None = None
    positive_sequence_impedance_r_percent: float | None = None
    positive_sequence_impedance_x_percent: float | None = None
    zero_sequence_impedance_x_percent: float | None = None
    tap_range_max_percent: float | None = None
    tap_range_min_percent: float | None = None
    transformer_rating_mva_winter: float | None = None
    transformer_rating_mva_summer: float | None = None
    reverse_power_capability_percent: str | float | None = None
    method_of_earthing_hv: str | None = None
    method_of_earthing_lv: str | None = None
    sitefunctionallocation: str | None = None
    licencearea: str | None = None
    id: int | None = None


class GspProjectStatusApiRecord(ApiResponse):
    """Row of ``ukpn-gsp-project-status``: GSP queue breakdown by gate status."""

    gsp: str | None = None
    technology_type: str | None = None
    measure: str | None = None  # "MW" or "#"
    sortby: str | None = None
    gate_2_protected_26_27: float | None = None
    gate_2_phase_1: float | None = None
    gate_2_phase_2: float | None = None
    gate_1: float | None = None
    has_not_undergone_gated_process: float | None = None


class AppendixGRecord(ApiResponse):
    """Row of ``ukpn-appendix-g`` ("Appendix G Detail"): one project in the transmission queue of its GSP.

    Distribution-connected generation and storage whose connection depends on the GSP's Appendix G (the DNO's
    transmission capacity allowance), with its Gate 2 connections-reform outcome. Monthly, CC BY 4.0.
    """

    gsp: str | None = None  # upper case, e.g. "WEST WEYBRIDGE", "BARKING C (EPN)", "HACKNEY 132"
    unique_nodd_id: str | None = None  # e.g. "WWYB-SPN-1257"
    site_name: str | None = None  # free text, often with a postcode
    connection_status: str | None = None  # "Connected" | "Gate 2 - Protected 26-27 able to connect enduring" | ...
    technology: str | None = None  # e.g. "Electricity Storage", "Solar PV", "Unabated Gas"
    developer_capacity_mw: float | None = None
    # text upstream: "Connected", "dd/mm/yyyy", or an Excel day serial such as "46296" (= 2026-10-01); mostly null
    date_of_connection: str | None = None
    date_of_non_firm_accelerated_connection: str | None = None  # same formats
    contract_status: str | None = None  # e.g. "Included in a signed NESO Gated contract", "Awaiting NESO Gated offer"
    type_of_contract: str | None = None  # "App G" | ...
    previously_offered_new_gsp: str | None = None  # "Y" | "N"
    position: int | None = None  # running number across all GSPs; connected rows are numbered too


# -------------------- 4. Not from the API (helpers, registries, transforms) --------------------- #


DATASETS: dict[str, DatasetSpec[Any]] = {
    "substations": DatasetSpec(
        BASE_URL, "grid-and-primary-sites", "spatial_coordinates", RecordsResponse[GridPrimarySite]
    ),
    "capacity_heatmap": DatasetSpec(
        BASE_URL, "ukpn-capacity-heatmap", "geo_point_2d", RecordsResponse[CapacityHeatmapSite]
    ),
    "embedded_capacity_register": DatasetSpec(
        BASE_URL,
        "ukpn-embedded-capacity-register",
        "spatialcoordinates_customer",
        RecordsResponse[EmbeddedCapacityRecord],
    ),
    "overhead_lines_132kv": DatasetSpec(
        BASE_URL, "ukpn-132kv-overhead-lines", "geo_shape", RecordsResponse[OverheadLine], "geo_point_2d"
    ),
    "overhead_lines_33kv": DatasetSpec(
        BASE_URL, "ukpn-33kv-overhead-lines", "geo_shape", RecordsResponse[OverheadLine], "geo_point_2d"
    ),
    "table6": DatasetSpec(
        BASE_URL, "ukpn-ltds-table-6-interest-connections", "spatial_coordinates", RecordsResponse[Table6InterestRecord]
    ),
    "table2a": DatasetSpec(
        BASE_URL, "ukpn-ltds-table-2a-transformer-2w", None, RecordsResponse[Table2aTransformerRecord]
    ),
    "gsp_project_status": DatasetSpec(
        BASE_URL, "ukpn-gsp-project-status", None, RecordsResponse[GspProjectStatusApiRecord]
    ),
    "appendix_g": DatasetSpec(BASE_URL, "ukpn-appendix-g", None, RecordsResponse[AppendixGRecord]),
}
