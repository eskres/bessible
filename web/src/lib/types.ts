export interface PositionCoords {
  lat: number;
  lon: number;
}

export interface AssessmentRequest {
  postcode?: string;
  /** A map pin: the exact site, no postcode needed. */
  position?: PositionCoords;
  property_url?: string;
  link?: string;
  battery_mw?: number;
  target_mw?: number;
  budget_gbp?: number;
  flexible_connection?: boolean;
}

export interface SubstationOption {
  name: string;
  distance_km: number;
  import_headroom_mw: number;
  export_headroom_mw: number;
  effective_headroom_mw: number;
  voltage_kv: number;
  is_marginal: boolean; // true if distance > 1 km
}

/** Backend spelling of a capacity alternate, before `normalizeCapacity` maps it to a SubstationOption. */
export interface RawSubstationOption extends Partial<SubstationOption> {
  substation?: string;
  size_mw?: number;
  marginal?: boolean;
}

/** The part of LocationData (GET /site-data) the map draws. */
export interface SiteData {
  title?: {
    geometry: GeoJSON.Geometry;
    area_ha: number;
    bbox: [number, number, number, number]; // min_lon, min_lat, max_lon, max_lat
  } | null;
  deterministic?: { grid?: SiteGrid };
}

export interface SiteGrid {
  substations: GridSubstation[];
  lines: GridLine[];
  projects: GridProject[];
}

export interface GridHeadroom {
  generation_mw?: number | null;
  generation_constraint?: string | null;
  demand?: number | null;
  demand_unit: 'MW' | 'MVA';
  demand_constraint?: string | null;
}

export interface GridSubstation {
  name: string;
  operator: string;
  kind: string;
  voltage_kv?: number | null;
  voltages?: string | null;
  coords: PositionCoords;
  distance_km: number;
  bsp?: string | null;
  gsp?: string | null;
  headroom?: GridHeadroom | null;
}

export interface GridLine {
  crosses_site: boolean;
  geometry: GeoJSON.Geometry;
}

export interface GridProject {
  name?: string | null;
  operator: string;
  coords: PositionCoords;
  distance_km: number;
  technology?: string | null;
  is_storage: boolean;
  is_solar: boolean;
  capacity_mw?: number | null;
  storage_mwh?: number | null;
  status?: string | null;
}

export interface Artifact {
  id: string;
  stage: string;
  claim: string;
  confidence: number;
  source_name?: string;
  model_used?: string;
  source_url?: string;
  file_path?: string;
  image_path?: string;
  snapshot_date?: string;
}

/** Cable from the site to the serving substation: the straight line, priced with a detour factor. */
export interface CableRoute {
  distance_km: number; // priced length: straight_km x detour_factor
  straight_km: number;
  detour_factor: number;
  path: PositionCoords[]; // site, substation
}

export interface CapacityOutput {
  viable: boolean;
  out_of_area: boolean;
  message?: string;
  substation?: string;
  serving_substation?: string;
  connection_voltage_kv?: number;
  voltage_kv?: number;
  firm_mw?: number;
  ceiling_mw?: number;
  recommended_mw?: number;
  binding_direction?: 'import' | 'export';
  binding_season?: 'winter' | 'summer';
  distance_km?: number; // straight line to the serving substation
  substation_position?: PositionCoords | null;
  route?: CableRoute | null;
  alternates?: SubstationOption[];
  artifacts?: Artifact[];
}

/** Mirrors `TitleSource` in `src/bessible/models.py`: where a title number came from. */
export type TitleSource = 'listing' | 'ccod' | 'ocod' | 'user';

/** Mirrors `TitleParcel`: one HM Land Registry INSPIRE index polygon (indicative extent; no title number of its own). */
export interface TitleParcel {
  inspire_id: string;
  geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  area_m2: number;
  source_url?: string | null; // the planning.data entity page
  title_number?: string | null; // only when a named source links one
  title_source?: TitleSource | null;
  title_source_url?: string | null;
  title_link?: 'polygon' | 'site' | null;
  footprint_overlap_pct?: number | null; // share of the BESS footprint on this polygon, 0-100
}

/** Mirrors `TitleNumber`: a title number from a named source, never inferred. */
export interface TitleNumber {
  title_number: string;
  source: TitleSource;
  source_url?: string | null;
  link: 'polygon' | 'site';
  inspire_id?: string | null;
  proprietor?: string | null;
  tenure?: string | null;
  address?: string | null;
  postcode?: string | null;
  evidence?: string | null;
}

/** Mirrors `TitleOutput`: the pin polygon and candidates before confirmation; the site's polygons after. */
export interface TitleOutput {
  title_number?: string | null;
  boundary_geojson?: Record<string, unknown>;
  area_m2: number;
  pin_parcel?: TitleParcel | null;
  candidates?: TitleParcel[];
  site_parcels?: TitleParcel[];
  inspire_ids?: string[];
  title_numbers?: TitleNumber[];
  search_radius_m?: number | null;
  notes?: string[];
  artifacts?: Artifact[];
}

export interface ConfirmedSite {
  position: [number, number] | PositionCoords; // [lng, lat] or { lat, lon }
  capacity_mw: number;
  reserved_acres?: number;
  footprint_geojson?: Record<string, unknown> | null;
  boundary?: TitleOutput | null;
}

/** Mirrors `SiteDecision`. */
export interface SiteDecision {
  confirmed: boolean;
  position?: [number, number] | PositionCoords;
  capacity_mw?: number;
  footprint_acres?: number;
  flexible_connection?: boolean;
  footprint_geojson?: GeoJSON.Feature<GeoJSON.Polygon> | null;
  /** INSPIRE ids of candidates the user clicked; omitted = the polygons under the footprint. */
  title_ids?: string[] | null;
  /** Polygons from `/inspire` outside the candidates; the backend fetches them again. */
  added_ids?: string[];
  /** Title numbers per INSPIRE id from the CLI / API (from the legal pack; not checked). The web UI does not send it. */
  user_title_numbers?: Record<string, string>;
}

export type RunStatusType =
  | 'running'
  | 'awaiting_confirmation'
  | 'completed'
  | 'rejected'
  | 'out_of_area'
  | 'not_viable'
  | 'failed'
  | 'pending';

export interface RunStatus {
  run_id: string;
  status: RunStatusType;
  stages?: string[];
  stage?: string;
  message?: string;
  capacity?: CapacityOutput;
  position?: [number, number] | PositionCoords;
  boundary?: TitleOutput | null;
}

/** Mirrors `DurationCase` in `src/bessible/models.py`. `irr` is a fraction; null when equity never pays back. */
export interface FinancialCase {
  duration_h: 2 | 4 | 8;
  capex_gbp: number;
  npv_gbp: number;
  irr?: number | null;
  over_budget?: boolean;
  curtailment_pct?: number | null;
  payback_years?: number | null;
}

/** Mirrors `FinancialOutput` in `src/bessible/models.py`. */
export interface FinancialOutput {
  cases: FinancialCase[];
  recommended_h?: 2 | 4 | 8 | null;
  rationale?: string | null;
  discount_rate_pct?: number | null;
  project_life_years?: number | null;
  artifacts?: Artifact[];
}

export interface SentimentOutput {
  opposition_index?: number | null; // 0-1, None = unknown
  top_concerns?: string[];
  sources?: number;
  paragraphs?: number;
  artifacts?: Artifact[];
}

export interface ReportOutput {
  verdict: 'go' | 'maybe' | 'no_go';
  findings: Array<{ text: string; artifact_ids: string[] }>;
  report_path?: string;
  artifacts?: Artifact[];
}

export interface AssessmentResult {
  run_id?: string;
  postcode?: string;
  status?: RunStatusType;
  message?: string;
  site?: ConfirmedSite;
  capacity?: CapacityOutput;
  grid_connection?: {
    serving_substation: string;
    distance_km: number;
    voltage_kv: number;
    rag_status?: string;
    gsp_status?: string;
    tia_threshold_mw?: number;
  };
  land_planning?: {
    reserved_acres: number;
    green_belt: boolean;
    consenting_route: string;
    planning_risk: string;
  };
  sentiment?: SentimentOutput;
  financial?: FinancialOutput | null;
  report?: ReportOutput;
  artifacts: Artifact[];
  /** Evidence the run could not get; the retryable ones can be fetched again while `retries_left` > 0. */
  gaps?: DataGap[];
  retries_left?: number;
  run_dir?: string;
}

/** Mirrors `bessible.models.DataGap`. */
export interface DataGap {
  stage: string;
  what: string;
  reason: string;
  sources: string[];
  retryable: boolean;
  could_block: boolean;
}

export interface TraceEvent {
  id: number;
  t: string;
  stage: string;
  msg: string;
}
