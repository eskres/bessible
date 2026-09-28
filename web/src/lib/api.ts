import {
  AssessmentRequest,
  AssessmentResult,
  CapacityOutput,
  RawSubstationOption,
  RunStatus,
  SiteData,
  SiteDecision,
  SubstationOption,
  TraceEvent,
} from './types';

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL !== undefined
    ? process.env.NEXT_PUBLIC_API_URL
    : process.env.NODE_ENV === 'production'
      ? ''
      : 'http://localhost:8000';

type TokenGetter = () => Promise<string | null>;
let getToken: TokenGetter = async () => null;

/** Registered by the auth layer; returns a fresh Firebase ID token, or null when signed out / in local mode. */
export function setTokenGetter(fn: TokenGetter) {
  getToken = fn;
}

/** Recorded demo runs use `demo-` ids and public `/demo/runs` routes: no token is sent for them. */
export const isDemoRun = (runId: string | null | undefined) => !!runId?.startsWith('demo-');

const runPath = (id: string) => (isDemoRun(id) ? `/demo/runs/${id}` : `/runs/${id}`);

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** fetch against the API with the bearer token attached (skipped for public demo and free-data routes). */
async function apiFetch(path: string, init: RequestInit = {}, opts: { auth?: boolean } = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (opts.auth !== false) {
    for (const [k, v] of Object.entries(await authHeaders())) headers.set(k, v);
  }
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

export class ApiError extends Error {
  status: number;
  data: unknown;

  constructor(status: number, message: string, data?: unknown) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export async function startRun(
  req: AssessmentRequest
): Promise<{ run_id: string }> {
  const res = await apiFetch('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    if (res.status === 503) {
      throw new ApiError(
        503,
        detailMessage(errorData, 'Temporal server is unavailable. Start it with: temporal server start-dev'),
        errorData
      );
    }
    throw new ApiError(
      res.status,
      detailMessage(errorData, 'Failed to start assessment run'),
      errorData
    );
  }

  return res.json();
}

/** Replays the recorded run in `data/demo/<slug>`: public route, no sign-in, no keys, no live model calls. */
export async function startDemoRun(slug: string): Promise<{ run_id: string }> {
  const res = await apiFetch(
    '/demo/runs',
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug }) },
    { auth: false }
  );
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(res.status, detailMessage(errorData, 'Failed to start demo run'), errorData);
  }
  return res.json();
}

export async function getRunStatus(id: string): Promise<RunStatus> {
  const res = await apiFetch(`${runPath(id)}/status`);
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(res.status, detailMessage(errorData, 'Failed to get status'), errorData);
  }
  const status = await res.json();
  if (status?.capacity) status.capacity = normalizeCapacity(status.capacity);
  return status;
}

export async function sendDecision(
  id: string,
  decision: SiteDecision
): Promise<{ allowed_min?: number; allowed_max?: number } | null> {
  const res = await apiFetch(`${runPath(id)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(decision),
  });

  if (res.status === 204) {
    return null;
  }

  if (res.status === 422) {
    const data = await res.json();
    if (data.allowed_min === undefined) {
      // A field failed validation, not the capacity range
      throw new ApiError(res.status, detailMessage(data, 'Decision submission failed'), data);
    }
    return {
      allowed_min: data.allowed_min,
      allowed_max: data.allowed_max,
    };
  }

  const errorData = await res.json().catch(() => ({}));
  throw new ApiError(res.status, detailMessage(errorData, 'Decision submission failed'), errorData);
}

/** Re-runs the evidence stages whose data gaps a retry may fill; the run's status goes back to running. */
export async function retryStages(id: string, stages: string[]): Promise<void> {
  const res = await apiFetch(`${runPath(id)}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stages }),
  });
  if (res.status === 204) return;
  const errorData = await res.json().catch(() => ({}));
  throw new ApiError(res.status, detailMessage(errorData, 'Retry failed'), errorData);
}

export async function getRunResult(id: string): Promise<AssessmentResult> {
  const res = await apiFetch(`${runPath(id)}/result`);
  if (res.status === 409) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(409, 'Run is not finished yet', data);
  }
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(res.status, detailMessage(errorData, 'Failed to get result'), errorData);
  }
  return res.json();
}

export async function checkCapacity(
  position: [number, number],
  flexible: boolean
): Promise<CapacityOutput> {
  const res = await apiFetch('/capacity/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ position, flexible }),
  }, { auth: false });

  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(res.status, detailMessage(errorData, 'Capacity check failed'), errorData);
  }

  return normalizeCapacity(await res.json());
}

export async function getAreasGeoJson(): Promise<GeoJSON.GeoJSON | null> {
  const res = await apiFetch('/data/areas.geojson');
  if (!res.ok) {
    return null;
  }
  return res.json();
}

/** INSPIRE polygons in a box, as `TitleParcel` features (GET /inspire, planning.data title-boundary). */
export async function getInspirePolygons(
  bbox: [number, number, number, number]
): Promise<GeoJSON.FeatureCollection | null> {
  const [minLng, minLat, maxLng, maxLat] = bbox;
  const res = await apiFetch(`/inspire?bbox=${minLng},${minLat},${maxLng},${maxLat}`, {}, { auth: false });
  if (!res.ok) {
    return null;
  }
  return res.json();
}

/**
 * Streams a run's progress trace over Server-Sent Events.
 *
 * `EventSource` cannot send an Authorization header, so this reads the SSE stream with `fetch`. It resumes from the
 * last seen event id after a dropped connection. The server closes the stream when the run finishes, which ends it.
 */
export function subscribeEvents(
  runId: string,
  onEvent: (event: TraceEvent) => void,
  lastEventId?: number
): () => void {
  const controller = new AbortController();
  const MAX_RETRIES = 5;

  const run = async () => {
    let lastId = lastEventId;
    let failures = 0;

    while (!controller.signal.aborted) {
      try {
        const query = lastId !== undefined ? `?last_event_id=${lastId}` : '';
        const res = await apiFetch(
          `${runPath(runId)}/events${query}`,
          { headers: { Accept: 'text/event-stream' }, signal: controller.signal, cache: 'no-store' },
          { auth: !isDemoRun(runId) }
        );
        if (!res.ok || !res.body) {
          // 4xx (not signed in, not your run, unknown run) will not fix itself; only retry server errors.
          if (res.status >= 400 && res.status < 500) return;
          throw new Error(`SSE HTTP ${res.status}`);
        }

        failures = 0;
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) return; // server closed the stream: the run is finished
          buffer += value.replace(/\r\n/g, '\n');
          let sep: number;
          while ((sep = buffer.indexOf('\n\n')) !== -1) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const data = frame
              .split('\n')
              .filter((l) => l.startsWith('data:'))
              .map((l) => l.slice(5).replace(/^ /, ''))
              .join('\n');
            if (!data) continue; // comment / keep-alive frame
            try {
              const event: TraceEvent = JSON.parse(data);
              lastId = event.id;
              onEvent(event);
            } catch (err) {
              console.error('Failed to parse SSE trace event:', err);
            }
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        if (++failures > MAX_RETRIES) {
          console.warn('SSE connection failed, giving up:', err);
          return;
        }
        await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** (failures - 1), 8000)));
      }
    }
  };

  void run();
  return () => controller.abort();
}

// --- Per-user Google (Gemini) key: the only credential. The API never returns a saved key, only its last 4 characters.

export interface KeyStatus {
  configured: boolean;
  last4: string | null;
  updated_at: string | null;
}

export interface KeyTestResult {
  ok: boolean;
  message?: string;
}

// FastAPI `detail` may be a string, an object with a `message`, or a validation-error list: only show readable text.
function detailMessage(data: { detail?: unknown; message?: unknown }, fallback: string): string {
  const d = data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    const msgs = d.map((e) => (e && typeof e === 'object' ? (e as { msg?: unknown }).msg : null)).filter((m) => typeof m === 'string');
    if (msgs.length) return msgs.join('; ');
  }
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') {
    return (d as { message: string }).message;
  }
  return typeof data.message === 'string' ? data.message : fallback;
}

async function throwApiError(res: Response, fallback: string): Promise<never> {
  const data = await res.json().catch(() => ({}));
  throw new ApiError(res.status, detailMessage(data, fallback), data);
}

export async function getKeyStatus(): Promise<KeyStatus> {
  const res = await apiFetch('/me/key');
  if (res.status === 404) return { configured: false, last4: null, updated_at: null };
  if (!res.ok) return throwApiError(res, 'Failed to load key status');
  const data = await res.json();
  return {
    configured: data.configured ?? Boolean(data.last4),
    last4: data.last4 ?? null,
    updated_at: data.updated_at ?? null,
  };
}

export async function saveKey(googleKey: string): Promise<KeyStatus> {
  const res = await apiFetch('/me/key', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ google_api_key: googleKey }),
  });
  if (!res.ok) return throwApiError(res, 'Failed to save key');
  const data = await res.json();
  return { configured: true, last4: data.last4 ?? null, updated_at: data.updated_at ?? null };
}

export async function deleteKey(): Promise<void> {
  const res = await apiFetch('/me/key', { method: 'DELETE' });
  if (!res.ok && res.status !== 404) return throwApiError(res, 'Failed to delete key');
}

/** Pings Gemini with the typed key, or the stored key when none is given. */
export async function testKey(googleKey?: string): Promise<KeyTestResult> {
  const res = await apiFetch('/me/key/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(googleKey ? { google_api_key: googleKey } : {}),
  });
  if (res.status === 401 || res.status === 404) return throwApiError(res, 'Not signed in or no key saved');
  const data = await res.json().catch(() => ({}));
  if (res.ok) return { ok: data.ok ?? true, message: data.message };
  return { ok: false, message: detailMessage(data, 'Key test failed') };
}

// LocationData for a coordinate: title boundary, substations with headroom, nearby projects, overhead lines.
export async function getSiteData(lat: number, lon: number): Promise<SiteData | null> {
  const res = await apiFetch(`/site-data?lat=${lat}&lon=${lon}`, {}, { auth: false });
  if (!res.ok) {
    return null;
  }
  return res.json();
}

type RawCapacityOutput = Omit<CapacityOutput, 'alternates'> & { alternates?: RawSubstationOption[] };

// The backend names things slightly differently from the UI types: map them once here.
function normalizeCapacity(cap: RawCapacityOutput): CapacityOutput {
  if (!cap) return cap;
  return {
    ...cap,
    serving_substation: cap.serving_substation ?? cap.substation,
    voltage_kv: cap.voltage_kv ?? cap.connection_voltage_kv,
    alternates: (cap.alternates ?? []).map(
      (a) =>
        ({
          ...a,
          name: a.name ?? a.substation,
          effective_headroom_mw: a.effective_headroom_mw ?? a.size_mw,
          is_marginal: a.is_marginal ?? a.marginal,
        }) as SubstationOption
    ),
  };
}
