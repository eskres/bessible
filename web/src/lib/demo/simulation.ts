// Demo-only data: presets and a local simulation of the screening pipeline. The live workspace never imports this.
import { AssessmentResult, CapacityOutput, SubstationOption, TraceEvent } from '../types';
import { distanceKm } from '../footprint';

export interface DemoPreset {
  label: string;
  postcode: string;
  coords: [number, number];
  desc: string;
  /** The recorded backend run in `data/demo/<slug>` that the demo replays instead of simulating in the browser. */
  slug?: string;
}

export const DEMO_PRESETS: DemoPreset[] = [
  {
    label: 'Histon Viable (CB24 9ZR)',
    postcode: 'CB24 9ZR',
    coords: [0.1082, 52.245],
    desc: 'Viable firm capacity (37.7 MW firm at Histon Grid 33kV)',
    slug: 'histon',
  },
  {
    label: 'Dorking (RH4 1AD)',
    postcode: 'RH4 1AD',
    coords: [-0.3302, 51.2329],
    desc: '8 MW firm at Dorking Town 11kV',
    slug: 'dorking',
  },
  {
    label: 'Out of Area (Manchester M1 1AD)',
    postcode: 'M1 1AD',
    coords: [-2.2449, 53.4838],
    desc: 'No supported DNO data here (not viable)',
    slug: 'manchester',
  },
];

/** The preset the demo opens with. */
export const DEFAULT_PRESET = DEMO_PRESETS[0];

const normalize = (postcode: string) => postcode.replace(/\s+/g, '').toUpperCase();

export const findPreset = (postcode: string) => DEMO_PRESETS.find((p) => normalize(p.postcode) === normalize(postcode));

const event = (id: number, stage: string, msg: string): TraceEvent => ({ id, t: new Date().toISOString(), stage, msg });

const SUBSTATIONS: Record<'dorking' | 'flexible' | 'default', SubstationOption[]> = {
  dorking: [
    { name: 'Dorking Town 11kV', distance_km: 1.24, voltage_kv: 11, import_headroom_mw: 10, export_headroom_mw: 12, effective_headroom_mw: 8, is_marginal: false },
    { name: 'Brockham 33kV Alternate', distance_km: 1.84, voltage_kv: 33, import_headroom_mw: 20, export_headroom_mw: 20, effective_headroom_mw: 15, is_marginal: false },
  ],
  flexible: [
    { name: 'Histon 33kV Primary', distance_km: 2.4, voltage_kv: 33, import_headroom_mw: 12, export_headroom_mw: 14, effective_headroom_mw: 3, is_marginal: true },
    { name: 'Milton Road Alternate', distance_km: 3.1, voltage_kv: 33, import_headroom_mw: 15, export_headroom_mw: 15, effective_headroom_mw: 4, is_marginal: true },
  ],
  default: [
    { name: 'Southwark Central Primary', distance_km: 0.65, voltage_kv: 33, import_headroom_mw: 15, export_headroom_mw: 18, effective_headroom_mw: 12, is_marginal: false },
    { name: 'Borough High Alternate', distance_km: 1.45, voltage_kv: 11, import_headroom_mw: 8, export_headroom_mw: 8, effective_headroom_mw: 6, is_marginal: true },
    { name: 'Elephant North Alternate', distance_km: 2.1, voltage_kv: 33, import_headroom_mw: 18, export_headroom_mw: 18, effective_headroom_mw: 14, is_marginal: true },
  ],
};

/** One timed step of a simulated screening: events to append, after `delayMs`. */
export interface SimStep {
  delayMs: number;
  events: TraceEvent[];
}

export interface SimScreening {
  steps: SimStep[];
  /** Applied after the last step: a capacity proposal to confirm, or a not-viable message. */
  outcome: { capacity: CapacityOutput } | { notViable: string };
}

/** Simulates the pre-confirmation stages for a postcode. Postcode prefixes pick the scenario. */
export function simulateScreening(
  postcode: string,
  center: [number, number],
  flexible: boolean
): SimScreening {
  const prefix = postcode.trim().toUpperCase();
  const first: SimStep = {
    delayMs: 0,
    events: [event(1, 'location', `Geocoded ${postcode} to [${center[1].toFixed(4)}, ${center[0].toFixed(4)}]`)],
  };
  const querying = event(2, 'grid', 'Querying UKPN network snapshot for distribution primary substation...');

  if (prefix.startsWith('M1')) {
    return {
      steps: [first, { delayMs: 800, events: [querying, event(3, 'grid', 'Error: Location falls outside UKPN licensed area.')] }],
      outcome: {
        notViable:
          'The requested postcode is located in Manchester (Electricity North West area). Bessible screening currently covers UKPN license regions (London, South East, Eastern England).',
      },
    };
  }

  const scenario = prefix.startsWith('RH') ? 'dorking' : prefix.startsWith('CB') ? 'flexible' : 'default';
  const [serving, ...rest] = SUBSTATIONS[scenario];
  const firm = serving.effective_headroom_mw;
  const ceiling = serving.export_headroom_mw;
  const capacity: CapacityOutput = {
    viable: scenario !== 'flexible' || flexible,
    out_of_area: false,
    serving_substation: serving.name,
    distance_km: serving.distance_km,
    voltage_kv: serving.voltage_kv,
    firm_mw: firm,
    ceiling_mw: ceiling,
    recommended_mw: flexible ? ceiling : firm,
    binding_direction: 'export',
    binding_season: 'summer',
    alternates: [serving, ...rest],
  };

  return {
    steps: [
      first,
      {
        delayMs: 350,
        events: [
          querying,
          event(3, 'capacity', `Identified serving substation: ${serving.name} (${firm} MW firm, ${ceiling} MW ceiling)`),
        ],
      },
      {
        delayMs: 750,
        events: [event(4, 'title', 'HM Land Registry INSPIRE boundaries retrieved. Awaiting human confirmation...')],
      },
    ],
    outcome: { capacity },
  };
}

/** A pin moved more than 0.9 km from where the run started falls to the first alternate substation. */
export function simulateCapacityMove(
  origin: [number, number],
  pos: [number, number],
  current: CapacityOutput,
  flexible: boolean
): CapacityOutput | null {
  const alt = current.alternates?.[0];
  if (!alt || distanceKm(origin, pos) <= 0.9) return null;
  return {
    ...current,
    serving_substation: alt.name,
    distance_km: alt.distance_km,
    voltage_kv: alt.voltage_kv,
    firm_mw: alt.effective_headroom_mw,
    ceiling_mw: alt.export_headroom_mw,
    recommended_mw: flexible ? alt.export_headroom_mw : alt.effective_headroom_mw,
  };
}

export const COMPLETION_EVENTS = (capacityMw: number, pos: [number, number]): TraceEvent[] => [
  event(5, 'feasibility', `Confirmed ${capacityMw} MW footprint at [${pos[1].toFixed(4)}, ${pos[0].toFixed(4)}]`),
  event(6, 'planning', 'Evaluated local TCPA planning policy and environmental constraints (Low Risk).'),
  event(7, 'financial', 'Generated 25-yr financial models across 2h, 4h, and 8h battery configurations.'),
  event(8, 'synthesis', 'Synthesis complete. All artifacts validated.'),
];

const CASES = [
  { duration_h: 2 as const, capex_gbp: 4800000, npv_gbp: 1650000, irr: 0.128 },
  { duration_h: 4 as const, capex_gbp: 8200000, npv_gbp: 3420000, irr: 0.145 },
  { duration_h: 8 as const, capex_gbp: 14900000, npv_gbp: 4100000, irr: 0.112 },
];

/** The simulated report for a confirmed site. */
export function simulateResult(
  runId: string,
  position: [number, number],
  capacityMw: number,
  capacity: CapacityOutput,
  postcode?: string
): AssessmentResult {
  const acres = Number((capacityMw * 4 * 0.0625).toFixed(2));
  return {
    run_id: runId,
    postcode,
    site: { position, capacity_mw: capacityMw, reserved_acres: acres },
    capacity,
    grid_connection: {
      serving_substation: capacity.serving_substation || 'Southwark Central Primary',
      distance_km: capacity.distance_km ?? 0.65,
      voltage_kv: capacity.voltage_kv || 33,
      rag_status: 'Green',
      gsp_status: 'Secure',
      tia_threshold_mw: 5,
    },
    land_planning: {
      reserved_acres: acres,
      green_belt: false,
      consenting_route: capacityMw >= 50 ? 'NSIP (DCO)' : 'TCPA (Local Planning Authority)',
      planning_risk: 'Low',
    },
    financial: { cases: CASES, recommended_h: 4, discount_rate_pct: 8, project_life_years: 25 },
    artifacts: [
      {
        id: 'art-01',
        stage: 'capacity',
        claim: `Substation ${capacity.serving_substation} provides ${capacity.firm_mw} MW firm headroom based on UKPN snapshot.`,
        confidence: 0.98,
        source_name: 'UKPN Long Term Development Statement (LTDS)',
        snapshot_date: 'Sep 2026',
        source_url: 'https://ukpn.opendatasoft.com',
      },
      {
        id: 'art-02',
        stage: 'grid',
        claim: 'Connection point verified at 33 kV primary busbar with no upstream transmission constraint.',
        confidence: 0.95,
        source_name: 'National Grid ESO Embedded Generation Register',
        snapshot_date: 'Q3 2026',
      },
      {
        id: 'site_land-flood_zone-05',
        stage: 'site_land',
        claim: 'Caveat: Flood Zone 2 covers 8% of the title; keep the compound on the northern part.',
        confidence: 0.9,
        source_name: 'Environment Agency Flood Map for Planning',
        snapshot_date: 'Sep 2026',
      },
      {
        id: 'art-03',
        stage: 'planning',
        claim: 'Site is outside Green Belt and SSSI environmental conservation areas.',
        confidence: 0.92,
        source_name: 'Natural England & Local Planning Register',
        snapshot_date: 'Aug 2026',
      },
      {
        id: 'art-04',
        stage: 'financial',
        claim: '4-hour configuration yields optimal 14.5% IRR with £3.42M NPV over 25-year operational lifecycle.',
        confidence: 0.89,
        source_name: 'Bessible Financial Model v1.2',
        snapshot_date: 'Current Model',
      },
    ],
  };
}
