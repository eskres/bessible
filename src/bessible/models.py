"""Pydantic data models for Bessible assessment stages and pipeline."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, ValidationInfo, model_validator

Stage = Literal[
    "location",
    "capacity",
    "title",
    "grid",
    "site_land",
    "market",
    "financial",
    "planning",
    "synthesis",
    "sentiment",
]

Verdict = Literal["go", "maybe", "no_go"]


class EncryptedCredentials(BaseModel):
    """The run owner's keys as ciphertext (plain `str`, never `SecretStr`: Temporal would mask or store it).

    The Google key is required. The Tavily key is optional; without it, Tavily calls use the server's key.
    """

    uid: str
    key_id: str
    google_ct: str
    tavily_ct: str | None = None
    tavily_key_id: str | None = None  # the master secret may rotate between the two saves


class Position(BaseModel):
    """Geographic coordinates in WGS84."""

    lat: float
    lon: float

    @model_validator(mode="before")
    @classmethod
    def parse_coords(cls, data: Any) -> Any:  # ruff: ignore[any-type]
        """Accept [lon, lat] pairs or dicts with lng instead of lon."""
        coord_pair_len = 2
        if isinstance(data, (list, tuple)) and len(data) == coord_pair_len:
            return {"lon": float(data[0]), "lat": float(data[1])}
        if isinstance(data, dict) and "lng" in data and "lon" not in data:
            return {**data, "lon": data["lng"]}
        return data


class AssessmentRequest(BaseModel):
    """User request to assess a site for BESS development."""

    property_url: HttpUrl | None = None
    postcode: str | None = None
    position: Position | None = None  # a pin on the map: the exact site, no postcode needed
    battery_mw: float | None = None
    budget_gbp: float | None = None
    flexible_connection: bool = False
    link: str | HttpUrl | None = None
    target_mw: float | None = None
    # Set by the API or CLI from the key store; client values are ignored
    credentials: EncryptedCredentials | None = None
    # Set by the API: how long a completed run stays open for `retry_stages`. 0 = it ends with its report
    retry_window_s: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def populate_aliases(cls, data: Any) -> Any:  # ruff: ignore[any-type]
        """Map link -> property_url and target_mw -> battery_mw."""
        if isinstance(data, dict):
            data = dict(data)
            if "link" in data and not data.get("property_url"):
                data["property_url"] = data["link"]
            if "target_mw" in data and data.get("battery_mw") is None:
                data["battery_mw"] = data["target_mw"]
        return data

    @model_validator(mode="after")
    def validate_site_provided(self) -> AssessmentRequest:
        """Ensure a property URL, UK postcode or map position is supplied."""
        if self.property_url is None and not self.postcode and self.position is None:
            msg = "One of property_url, postcode or position must be provided"
            raise ValueError(msg)
        return self


class DataGap(BaseModel):
    """Evidence a stage could not get: what is missing, why, and whether trying again could fill it."""

    stage: Stage
    what: str = Field(description="The check or figure left without data, e.g. 'outside_flood_zone_3'.")
    reason: str
    sources: list[str] = Field(default_factory=list, description="Upstream sources that failed.")
    retryable: bool = Field(default=False, description="A source failed, so a retry may fill it (else no coverage).")
    could_block: bool = Field(default=False, description="The missing evidence could have ruled the site out.")


class Artifact(BaseModel):
    """Explainable AI artifact supporting a claim or decision."""

    id: str
    stage: Stage
    claim: str
    details: list[str] = Field(default_factory=list)  # items behind the claim, listed when it is opened
    source_url: HttpUrl | None = None
    file_path: str | None = None
    image_path: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    model_used: str

    @model_validator(mode="after")
    def validate_evidence(self) -> Artifact:
        """Ensure at least one piece of evidence is present."""
        if not (self.source_url or self.file_path or self.image_path):
            msg = "Artifact must include at least one evidence field: source_url, file_path, or image_path"
            raise ValueError(msg)
        return self


class StageInput(BaseModel):
    """Base input for all assessment stages."""

    run_id: str
    request: AssessmentRequest


class LocationInput(StageInput):
    """Input for location resolution."""


class LocationOutput(BaseModel):
    """Resolved location coordinates and postcode."""

    postcode: str
    position: Position
    artifacts: list[Artifact] = Field(default_factory=list)


class CapacityInput(StageInput):
    """Input for capacity proposal stage."""

    location: LocationOutput


class AlternateOption(BaseModel):
    """Another primary substation near the site, shown for comparison."""

    substation: str
    distance_km: float
    size_mw: float
    marginal: bool  # farther than 1 km: cable cost and losses make it a weak option
    position: Position | None = None  # so a moved pin can be matched to its nearest substation


class CableRoute(BaseModel):
    """Cable from the site to the serving substation: the straight line, priced with a detour factor."""

    distance_km: float  # priced length: straight_km x detour_factor
    straight_km: float
    detour_factor: float
    path: list[Position]  # site, substation


class CapacityOutput(BaseModel):
    """Grid capacity proposal and headroom assessment."""

    viable: bool
    message: str | None = None
    out_of_area: bool = False
    substation: str | None = None
    connection_voltage_kv: float | None = None
    firm_mw: float = 0.0
    ceiling_mw: float = 0.0
    recommended_mw: float = 0.0
    binding_direction: Literal["import", "export"] | None = None
    binding_season: Literal["winter", "summer"] | None = None
    distance_km: float | None = None  # straight line to the serving substation (ranking, marginal flag)
    substation_position: Position | None = None
    route: CableRoute | None = None  # set after the proposal; its distance prices the cable
    alternates: list[AlternateOption] = Field(default_factory=list)
    tia_threshold_mw: Literal[1, 5] | None = None
    snapshot_date: date | None = None
    competition: Any = None
    export_ceiling_mw: float | None = None
    gsp: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_capacity_consistency(self) -> CapacityOutput:
        """Validate consistency of viability, out_of_area, and capacity bounds."""
        if self.out_of_area and self.viable:
            msg = "An out of area site cannot be viable"
            raise ValueError(msg)
        if not self.viable and not self.message:
            msg = "A non-viable capacity output must include an explanatory message"
            raise ValueError(msg)
        if self.ceiling_mw < self.firm_mw:
            msg = f"ceiling_mw ({self.ceiling_mw}) must be >= firm_mw ({self.firm_mw})"
            raise ValueError(msg)
        return self


class TitleInput(StageInput):
    """Input for title boundary lookup stage."""

    location: LocationOutput
    capacity: CapacityOutput


class Geometry(BaseModel):
    """A GeoJSON geometry in WGS84 (the same shape as `bessible.location.models.Geometry`)."""

    type: Literal["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]
    coordinates: list[Any]


TitleSource = Literal["listing", "ccod", "ocod", "user"]
TITLE_NUMBER_RE = r"[A-Z]{1,3}\d{1,6}"  # HMLR title numbers: 1 to 3 letters (county prefix) then digits


class TitleParcel(BaseModel):
    """One HM Land Registry INSPIRE index polygon: the position and indicative extent of a registered property.

    The free index carries no title number: `title_number` is set only when a named source links one to this
    polygon (`title_link="polygon"`) or to the site it belongs to (`"site"`).
    """

    inspire_id: str
    geometry: Geometry
    area_m2: float
    source_url: str | None = None  # the planning.data entity page
    title_number: str | None = None
    title_source: TitleSource | None = None
    title_source_url: str | None = None  # the listing URL, the HMLR dataset, or None for the user
    title_link: Literal["polygon", "site"] | None = None
    footprint_overlap_pct: float | None = None  # share of the BESS footprint on this polygon, 0-100


class TitleNumber(BaseModel):
    """A title number from a named source. Never inferred from an INSPIRE id, a neighbour or a model."""

    title_number: str
    source: TitleSource
    source_url: str | None = None
    link: Literal["polygon", "site"] = "site"
    inspire_id: str | None = None  # the polygon it is tied to, when `link == "polygon"`
    proprietor: str | None = None  # CCOD / OCOD: the registered company
    tenure: str | None = None
    address: str | None = None
    postcode: str | None = None
    evidence: str | None = None  # the listing sentence, or the matching dataset row's address


class TitleOutput(BaseModel):
    """Land registry title boundaries and area.

    Before confirmation: `pin_parcel` and `candidates` from the polygon search, `site_parcels` the pin polygon.
    After: `site_parcels` are the polygons the confirmed site covers; `boundary_geojson` and `area_m2` their union.
    """

    title_number: str | None = None  # the pin polygon's title number, when a source gives one
    boundary_geojson: dict[str, Any] = Field(default_factory=dict)
    area_m2: float
    pin_parcel: TitleParcel | None = None
    candidates: list[TitleParcel] = Field(default_factory=list)
    site_parcels: list[TitleParcel] = Field(default_factory=list)
    inspire_ids: list[str] = Field(default_factory=list)
    title_numbers: list[TitleNumber] = Field(default_factory=list)
    search_radius_m: float | None = None
    notes: list[str] = Field(default_factory=list)  # what was not found or skipped, and why
    artifacts: list[Artifact] = Field(default_factory=list)


class SiteDecision(BaseModel):
    """Decision submitted by human-in-the-loop."""

    confirmed: bool
    position: Position | None = None
    capacity_mw: float | None = None
    footprint_acres: float | None = None
    flexible_connection: bool | None = None
    footprint_geojson: dict[str, Any] | None = None
    # The polygons the user clicked on (INSPIRE ids of candidates). None: the footprint decides.
    title_ids: list[str] | None = None
    # Polygons added from the `/inspire` search outside the candidates; the confirm activity fetches them again.
    added_ids: list[str] = Field(default_factory=list)
    # Title numbers typed by the user, per INSPIRE id (from the legal pack; not checked).
    user_title_numbers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalise_title_numbers(self) -> SiteDecision:
        """Title numbers upper case without spaces, in the registry's format (letters then digits)."""
        clean: dict[str, str] = {}
        for inspire_id, number in self.user_title_numbers.items():
            n = re.sub(r"\s+", "", number).upper()
            if not n:
                continue
            if not re.fullmatch(TITLE_NUMBER_RE, n):
                msg = f"{number!r} is not a title number (1 to 3 letters then digits, e.g. SY123456)"
                raise ValueError(msg)
            clean[inspire_id] = n
        self.user_title_numbers = clean
        return self

    def check_titles(self, candidate_ids: set[str]) -> None:
        """Raise ValueError unless every clicked polygon is a candidate and added ids look like INSPIRE ids."""
        unknown = sorted(set(self.title_ids or ()) - candidate_ids)
        if unknown:
            msg = f"Not among the candidate polygons: {', '.join(unknown[:10])}"
            raise ValueError(msg)
        bad = [i for i in self.added_ids if not i.isdigit()]
        if bad:
            msg = f"Not INSPIRE ids: {', '.join(bad[:10])}"
            raise ValueError(msg)
        stray = sorted(set(self.user_title_numbers) - candidate_ids - set(self.added_ids))
        if stray:
            msg = f"Title numbers given for polygons that are not candidates: {', '.join(stray[:10])}"
            raise ValueError(msg)


class TitleSiteInput(BaseModel):
    """Everything the confirmation step needs: the pre-HITL title result and the human's decision."""

    run_id: str
    title: TitleOutput
    origin: Position  # the pin the candidates were searched around
    position: Position  # the confirmed pin
    capacity_mw: float
    footprint_geojson: dict[str, Any] | None = None
    title_ids: list[str] | None = None
    added_ids: list[str] = Field(default_factory=list)
    user_title_numbers: dict[str, str] = Field(default_factory=dict)


class ConfirmedSite(BaseModel):
    """Confirmed site parameters approved by the human operator."""

    position: Position
    capacity_mw: float
    boundary: TitleOutput
    footprint_geojson: dict[str, Any] | None = None
    capacity: CapacityOutput | None = Field(default=None, repr=False)
    flexible_connection: bool = Field(default=False, repr=False)

    @model_validator(mode="after")
    def validate_limits(self, info: ValidationInfo) -> ConfirmedSite:
        """Validate capacity against ceiling and firm limits."""
        cap = self.capacity
        flex = self.flexible_connection
        if info.context:
            if "capacity" in info.context:
                cap = info.context["capacity"]
            if "flexible_connection" in info.context:
                flex = info.context["flexible_connection"]
        if cap is not None:
            if self.capacity_mw > cap.ceiling_mw:
                msg = f"Capacity {self.capacity_mw} MW exceeds ceiling {cap.ceiling_mw} MW"
                raise ValueError(msg)
            if not flex and self.capacity_mw > cap.firm_mw:
                msg = (
                    f"Capacity {self.capacity_mw} MW exceeds firm capacity {cap.firm_mw} MW without flexible connection"
                )
                raise ValueError(msg)
            if self.capacity_mw <= 0:
                msg = "Capacity must be positive"
                raise ValueError(msg)
        return self

    @classmethod
    def build(cls, *, capacity: CapacityOutput, flexible: bool = False, **fields: Any) -> ConfirmedSite:  # ruff: ignore[any-type]
        """Construct and validate against the capacity proposal."""
        return cls.model_validate(fields, context={"capacity": capacity, "flexible_connection": flexible})


class NodeInput(StageInput):
    """Input for downstream analysis stages following site confirmation."""

    site: ConfirmedSite
    capacity: CapacityOutput


class GridOutput(BaseModel):
    """Grid connection feasibility and queue position."""

    gate2_queue_position: int | None = None
    indicative_connection_months: int | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    gaps: list[DataGap] = Field(default_factory=list)


class SiteLandOutput(BaseModel):
    """Site land use and planning constraints assessment."""

    land_use: str
    constraints: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list, description="Hard-check failures: cannot be built here.")
    caveats: list[str] = Field(default_factory=list, description="Hard-check warnings: possible with a caveat.")
    gaps: list[DataGap] = Field(default_factory=list, description="Checks with no data, and why.")
    artifacts: list[Artifact] = Field(default_factory=list)


class StreamValue(BaseModel):
    """Revenue of one stream for one duration, with its source."""

    stream: str
    gbp_per_mw_year: float
    source: str
    source_url: HttpUrl
    as_of: date
    cached: bool
    placeholder: bool = False
    scheme: str | None = None
    method: str | None = None  # how the figure was computed, for the artifact claim
    period: str | None = None  # the data period it covers


class MarketOutput(BaseModel):
    """Market revenue projections and value streams."""

    revenue_gbp_per_mw_year: float
    streams: dict[str, float] = Field(default_factory=dict)
    by_duration: dict[int, list[StreamValue]] | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    gaps: list[DataGap] = Field(default_factory=list)


class FinancialInput(NodeInput):
    """Input for financial model stage."""

    grid: GridOutput
    market: MarketOutput
    site_land: SiteLandOutput | None = None


class CaseBound(BaseModel):
    """Lower or upper bound for financial metrics in a duration case."""

    capex_gbp: float
    npv_gbp: float
    irr: float | None = None
    payback_years: float | None = None


class DurationCase(BaseModel):
    """Financial returns for a specific storage duration case."""

    duration_h: Literal[2, 4, 8]
    capex_gbp: float
    npv_gbp: float
    irr: float | None = None
    over_budget: bool = False
    curtailment_pct: float | None = None
    low: CaseBound | None = None
    high: CaseBound | None = None
    payback_years: float | None = None


REQUIRED_DURATION_HOURS = (2, 4, 8)


class FinancialOutput(BaseModel):
    """Financial modeling outputs across storage durations."""

    cases: list[DurationCase]
    recommended_h: Literal[2, 4, 8] | None = None
    rationale: str | None = None
    discount_rate_pct: float | None = None
    project_life_years: int | None = None
    artifacts: list[Artifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_duration_cases(self) -> FinancialOutput:
        """Ensure cases cover exactly durations 2, 4, and 8."""
        durations = tuple(sorted(c.duration_h for c in self.cases))
        if durations != REQUIRED_DURATION_HOURS or len(self.cases) != len(REQUIRED_DURATION_HOURS):
            msg = "FinancialOutput must contain exactly three duration cases: 2h, 4h, and 8h"
            raise ValueError(msg)
        return self


class PlanningInput(NodeInput):
    """Input for regulatory and planning stage."""

    grid: GridOutput
    site_land: SiteLandOutput


class TiaStatement(BaseModel):
    """Transmission Impact Assessment requirement statement and threshold."""

    threshold_mw: Literal[1, 5] | None = None
    triggered: bool | None = None
    statement: str
    source_url: HttpUrl = HttpUrl("https://ukpowernetworks.opendatasoft.com/explore/dataset/ukpn-capacity-heatmap/")
    snapshot_date: date | None = None


class NearbyProject(BaseModel):
    """Battery storage project from REPD within search radius."""

    id: str
    name: str
    mw: float | None = None  # REPD leaves capacity blank for some projects
    status: str
    status_date: date
    distance_km: float
    ref_id: str | None = None  # REPD "Ref ID"
    csv_row: int | None = None  # row in the published REPD CSV (header = row 1)
    planning_authority: str | None = None
    planning_ref: str | None = None  # the council's planning application reference
    source_url: HttpUrl | None = None  # the CSV, with an RFC 7111 `#row=` fragment

    @property
    def capacity(self) -> str:
        """`49.5 MW`, or a plain note when REPD has no capacity."""
        return f"{self.mw:g} MW" if self.mw is not None else "capacity not stated"


class PlanningOutput(BaseModel):
    """Consenting pathway and regulatory risk assessment."""

    consenting_route: str
    risks: list[str] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    tia: TiaStatement | None = None
    nearby: list[NearbyProject] = Field(default_factory=list)


class Finding(BaseModel):
    """Synthesised finding citing evidence artifacts."""

    text: str
    artifact_ids: list[str] = Field(min_length=1)


class SentimentOutput(BaseModel):
    """Local community sentiment analysis output."""

    opposition_index: float | None = None  # 0-1, None = unknown
    top_concerns: list[str] = Field(default_factory=list)  # up to 3
    sources: int = 0
    paragraphs: int = 0
    artifacts: list[Artifact] = Field(default_factory=list)
    gaps: list[DataGap] = Field(default_factory=list)


class SynthesisInput(NodeInput):
    """Input for final report synthesis stage."""

    grid: GridOutput
    site_land: SiteLandOutput
    market: MarketOutput
    financial: FinancialOutput
    planning: PlanningOutput
    sentiment: SentimentOutput | None = None
    artifacts: list[Artifact] = Field(default_factory=list)


class ReportOutput(BaseModel):
    """Assessment report output and overall verdict."""

    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    report_path: str
    artifacts: list[Artifact] = Field(default_factory=list)


class RunStatus(BaseModel):
    """Current state of a workflow run queried by clients."""

    run_id: str | None = None
    status: Literal[
        "running",
        "awaiting_confirmation",
        "completed",
        "rejected",
        "out_of_area",
        "not_viable",
        "failed",
    ]
    stages: list[Stage] = Field(default_factory=list)
    capacity: CapacityOutput | None = None
    boundary: TitleOutput | None = None
    position: Position | None = None
    message: str | None = None


class AssessmentResult(BaseModel):
    """Final output of an assessment run."""

    status: Literal["completed", "rejected", "out_of_area", "not_viable"]
    message: str | None = None
    run_id: str | None = None  # the workflow id; None in recordings made before it was added
    postcode: str | None = None  # the resolved location's postcode, for the report heading
    report: ReportOutput | None = None
    financial: FinancialOutput | None = None
    sentiment: SentimentOutput | None = None  # so a keyless demo can re-assess a moved site
    site: ConfirmedSite | None = None  # the site the user confirmed; the report's MW and MWh come from it
    capacity: CapacityOutput | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    gaps: list[DataGap] = Field(default_factory=list, description="Every stage's missing evidence.")
    retries_left: int = 0
    run_dir: str
