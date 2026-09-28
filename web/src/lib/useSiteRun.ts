'use client';

import { useEffect, useRef, useState } from 'react';
import { AssessmentResult, CapacityOutput, RunStatus, SiteDecision, TitleParcel, TraceEvent } from './types';
import { generateFootprintPolygon } from './footprint';
import { footprintShares, parcelAt } from './parcels';
import { getRunResult, getRunStatus, retryStages, sendDecision, subscribeEvents } from './api';

/** Statuses a run never leaves: polling and streaming stop here. */
const FINAL_STATUSES = ['completed', 'not_viable', 'failed', 'rejected', 'out_of_area'];

/** Capacity at a new pin position, or null to keep the current proposal. */
export type CapacityChecker = (
  pos: [number, number],
  flexible: boolean,
  ctx: { origin: [number, number]; current: CapacityOutput | null }
) => Promise<CapacityOutput | null>;

export const DEFAULT_CENTER: [number, number] = [-0.1132, 51.5014];

/**
 * State for one site assessment: map pin, capacity proposal, trace and result.
 * `track(runId)` follows a backend run (live or recorded replay) by polling its status and streaming its trace;
 * `simulate(runId)` marks a run the caller drives itself. Mode-specific behaviour comes in through `checkCapacityAt`.
 */
export function useSiteRun(checkCapacityAt: CapacityChecker) {
  const [runId, setRunId] = useState<string | null>(null);
  const [tracked, setTracked] = useState(false);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  // Simulated runs set this themselves; a tracked run streams until it reaches a final status.
  const [simStreaming, setIsStreaming] = useState(false);
  const [result, setResult] = useState<AssessmentResult | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const [initialCenter, setInitialCenter] = useState<[number, number]>(DEFAULT_CENTER);
  const [currentPosition, setCurrentPosition] = useState<[number, number]>(DEFAULT_CENTER);
  const [capacityProposal, setCapacityProposal] = useState<CapacityOutput | null>(null);
  const [capacityLoading, setCapacityLoading] = useState(false);
  const [selectedCapacityMw, setSelectedCapacityMw] = useState(10);
  const [flexibleConnection, setFlexibleConnection] = useState(false);
  const [submittingDecision, setSubmittingDecision] = useState(false);
  const [substationChangeNotice, setSubstationChangeNotice] = useState<string | null>(null);
  const [inspireGeoJson, setInspireGeoJson] = useState<GeoJSON.GeoJSON | null>(null);
  // Title polygons: null = the polygons under the footprint; else the INSPIRE ids the user clicked on
  const [clickedIds, setClickedIds] = useState<string[] | null>(null);
  const [addedParcels, setAddedParcels] = useState<TitleParcel[]>([]); // from /inspire, outside the candidates

  const positionedRunRef = useRef<string | null>(null);
  const proposedRunRef = useRef<string | null>(null);
  const pendingPinRef = useRef<[number, number] | null>(null);
  // The run on screen now: a poll that resolves after the user moved on is dropped.
  const activeRunRef = useRef<string | null>(null);

  /** Tells a tracked run that has not reached its report that the user moved on, so its workflow ends as rejected. */
  const releaseRun = () => {
    if (tracked && runId && (runStatus?.status === 'running' || runStatus?.status === 'awaiting_confirmation')) {
      void sendDecision(runId, { confirmed: false }).catch(() => {
        // the site was already confirmed, or the run is gone
      });
    }
  };

  const clearTitles = () => {
    setClickedIds(null);
    setAddedParcels([]);
  };

  /**
   * Clicks a title polygon on or off. The first click starts from `current`, the polygons under the footprint;
   * from then on the clicked set is the site. A polygon from /inspire outside the candidates is kept in `added`.
   */
  const toggleParcel = (parcel: TitleParcel) => {
    const base = clickedIds ?? titleSite.siteIds;
    const on = !base.includes(parcel.inspire_id);
    setClickedIds(on ? [...base, parcel.inspire_id] : base.filter((id) => id !== parcel.inspire_id));
    const candidate = runStatus?.boundary?.candidates?.some((p) => p.inspire_id === parcel.inspire_id);
    if (on && !candidate && !addedParcels.some((p) => p.inspire_id === parcel.inspire_id)) {
      setAddedParcels([...addedParcels, parcel]);
    }
  };

  /** Back to the polygons under the footprint. */
  const resetParcels = () => setClickedIds(null);

  /** Clears the previous run and centres the map. `pin` keeps a user-placed pin instead of the centre. */
  const begin = (center: [number, number], pin?: [number, number]) => {
    releaseRun();
    setErrorMsg(null);
    setResult(null);
    setEvents([]);
    setRunStatus(null);
    setInspireGeoJson(null);
    clearTitles();
    setSubstationChangeNotice(null);
    // Keep the previous run's capacity card mounted (SiteControls shows it dimmed, under a
    // loading overlay) instead of unmounting it while the new run's data is in flight.
    setCapacityLoading(true);
    positionedRunRef.current = null;
    proposedRunRef.current = null;
    pendingPinRef.current = pin ?? null;
    setInitialCenter(center);
    setCurrentPosition(pin ?? center);
  };

  const track = (id: string) => {
    activeRunRef.current = id;
    setRunId(id);
    setTracked(true);
    setRunStatus({ run_id: id, status: 'running' });
  };

  const simulate = (id: string) => {
    activeRunRef.current = id;
    setRunId(id);
    setTracked(false);
  };

  const reset = () => {
    releaseRun();
    activeRunRef.current = null;
    setRunId(null);
    setTracked(false);
    setRunStatus(null);
    setResult(null);
    setEvents([]);
    setErrorMsg(null);
    setCapacityProposal(null);
    setCapacityLoading(false);
    setInspireGeoJson(null);
    clearTitles();
    setSubstationChangeNotice(null);
    positionedRunRef.current = null;
    proposedRunRef.current = null;
    pendingPinRef.current = null;
  };

  /** Moves the pin and re-checks capacity there. */
  const moveTo = async (pos: [number, number], base: CapacityOutput | null = capacityProposal) => {
    setCurrentPosition(pos);
    setSubstationChangeNotice(null);
    setCapacityLoading(true);
    try {
      const updated = await checkCapacityAt(pos, flexibleConnection, { origin: initialCenter, current: base });
      if (!updated) return;
      if (
        base?.serving_substation &&
        updated.serving_substation &&
        updated.serving_substation !== base.serving_substation
      ) {
        const away = updated.distance_km != null ? ` (${updated.distance_km} km away)` : '';
        setSubstationChangeNotice(`Pin moved into new substation area: Now served by ${updated.serving_substation}${away}`);
      }
      setCapacityProposal(updated);
      if (updated.recommended_mw) setSelectedCapacityMw(updated.recommended_mw);
    } catch {
      // keep the current proposal
    } finally {
      setCapacityLoading(false);
    }
  };

  /** Moves the pin without a capacity re-check, e.g. when the map pulls it back inside the screening radius. */
  const clampTo = (pos: [number, number]) => setCurrentPosition(pos);

  const toggleFlexible = (enabled: boolean) => {
    setFlexibleConnection(enabled);
    if (enabled && capacityProposal?.ceiling_mw) {
      setSelectedCapacityMw(capacityProposal.ceiling_mw);
    } else if (!enabled && capacityProposal?.firm_mw) {
      setSelectedCapacityMw(Math.min(selectedCapacityMw, capacityProposal.firm_mw));
    }
  };

  // The title polygons the site covers now: the footprint's shares, or the user's clicks
  const candidates = runStatus?.boundary?.candidates;
  const confirmedParcels = runStatus?.boundary?.site_parcels;
  const titleSite = (() => {
    if (!candidates?.length && confirmedParcels?.length) {
      // After confirmation the backend's own measurement is the site
      const shares = confirmedParcels
        .filter((p) => p.footprint_overlap_pct != null)
        .map((p) => ({ parcel: p, pct: p.footprint_overlap_pct ?? 0 }));
      const covered = shares.reduce((sum, s) => sum + s.pct, 0);
      return {
        pool: confirmedParcels,
        shares,
        uncoveredPct: shares.length ? Math.max(0, 100 - covered) : 0,
        siteIds: confirmedParcels.map((p) => p.inspire_id),
        pinId: parcelAt(confirmedParcels, currentPosition)?.inspire_id ?? null,
      };
    }
    const pool = [...(candidates ?? []), ...addedParcels];
    const footprint = generateFootprintPolygon(currentPosition, selectedCapacityMw, 4);
    const { shares, uncoveredPct } = footprintShares(pool, footprint);
    const siteIds = clickedIds ?? shares.map((s) => s.parcel.inspire_id);
    // The polygon under the pin where it is now, not where the title search found it
    return { pool, shares, uncoveredPct, siteIds, pinId: parcelAt(pool, currentPosition)?.inspire_id ?? null };
  })();

  /** The site as the user left it. */
  const decision = (): SiteDecision => {
    const candidates = new Set((runStatus?.boundary?.candidates ?? []).map((p) => p.inspire_id));
    return {
      confirmed: true,
      position: currentPosition,
      capacity_mw: selectedCapacityMw,
      footprint_acres: selectedCapacityMw * 4 * 0.0625,
      flexible_connection: flexibleConnection,
      footprint_geojson: generateFootprintPolygon(currentPosition, selectedCapacityMw, 4),
      title_ids: clickedIds ? clickedIds.filter((id) => candidates.has(id)) : null,
      added_ids: clickedIds ? clickedIds.filter((id) => !candidates.has(id)) : [],
    };
  };

  /** Sends the confirmed site to a tracked run; the poll picks up the rest. */
  const submitDecision = async () => {
    if (!runId || !capacityProposal) return;
    setSubmittingDecision(true);
    try {
      const err = await sendDecision(runId, decision());
      if (err) {
        setErrorMsg(`Capacity must be between ${err.allowed_min} MW and ${err.allowed_max} MW.`);
        return;
      }
      setRunStatus({ run_id: runId, status: 'running' });
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Could not send the site decision. Try again.');
    } finally {
      setSubmittingDecision(false);
    }
  };

  /** Re-runs stages with retryable data gaps; the report stays on screen until the poll brings the new one. */
  const retry = async (stages: string[]) => {
    if (!runId || !tracked) return;
    setErrorMsg(null);
    try {
      await retryStages(runId, stages);
      setRunStatus({ run_id: runId, status: 'running' });
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Could not retry. Try again.');
    }
  };

  /** Declines the site: the run ends as rejected and the map stays put for the user to pick another location. */
  const exploreAnother = () => {
    releaseRun();
    // Drop polls still in flight, so a late status cannot bring the decision card back.
    activeRunRef.current = null;
    setCapacityProposal(null);
    setSubstationChangeNotice(null);
    if (runId) setRunStatus({ run_id: runId, status: 'rejected' });
  };

  // Poll status while a tracked run is in progress
  useEffect(() => {
    if (!runId || !tracked || FINAL_STATUSES.includes(runStatus?.status ?? '')) {
      return;
    }

    const interval = setInterval(async () => {
      try {
        const status = await getRunStatus(runId);
        if (activeRunRef.current !== runId) return;
        setRunStatus(status);
        if (status.status !== 'running') {
          setCapacityLoading(false);
        }
        if (status.status === 'not_viable' || status.status === 'out_of_area') {
          setCapacityProposal(null);
        }

        if (status.position && positionedRunRef.current !== runId) {
          positionedRunRef.current = runId;
          const p: [number, number] = Array.isArray(status.position)
            ? status.position
            : [status.position.lon, status.position.lat];
          setInitialCenter(p);
          setCurrentPosition(pendingPinRef.current ?? p);
        }
        if (status.status === 'awaiting_confirmation' && status.capacity && proposedRunRef.current !== runId) {
          proposedRunRef.current = runId;
          setCapacityProposal(status.capacity);
          if (status.capacity.recommended_mw) {
            setSelectedCapacityMw(status.capacity.recommended_mw);
          }
          if (pendingPinRef.current) {
            const pin = pendingPinRef.current;
            pendingPinRef.current = null;
            void moveTo(pin, status.capacity);
          }
        } else if (status.status === 'completed') {
          const res = await getRunResult(runId);
          if (activeRunRef.current === runId) setResult(res);
        }
      } catch {
        // transient: try again on the next tick
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [runId, tracked, runStatus?.status]);

  /** Appends trace events, skipping any id already in the trace (the trace is keyed by id). */
  const addEvents = (added: TraceEvent[]) =>
    setEvents((prev) => {
      const seen = new Set(prev.map((e) => e.id));
      const fresh = added.filter((e) => !seen.has(e.id) && seen.add(e.id));
      return fresh.length ? [...prev, ...fresh] : prev;
    });

  // Stream the tracked run's trace
  useEffect(() => {
    if (!runId || !tracked) return;
    return subscribeEvents(runId, (event) => {
      // Drop a late event from a run the user already left, and any event the stream sends twice.
      if (activeRunRef.current !== runId) return;
      addEvents([event]);
    });
  }, [runId, tracked]);

  const isStreaming = tracked
    ? !!runId && !FINAL_STATUSES.includes(runStatus?.status ?? '')
    : simStreaming;

  return {
    runId,
    tracked,
    runStatus,
    setRunStatus,
    events,
    setEvents,
    addEvents,
    isStreaming,
    setIsStreaming,
    result,
    setResult,
    errorMsg,
    setErrorMsg,
    starting,
    setStarting,
    initialCenter,
    currentPosition,
    capacityProposal,
    setCapacityProposal,
    capacityLoading,
    setCapacityLoading,
    selectedCapacityMw,
    setSelectedCapacityMw,
    flexibleConnection,
    setFlexibleConnection,
    submittingDecision,
    setSubmittingDecision,
    substationChangeNotice,
    inspireGeoJson,
    setInspireGeoJson,
    clickedIds,
    titleSite,
    addedParcels,
    setAddedParcels,
    toggleParcel,
    resetParcels,
    begin,
    track,
    simulate,
    reset,
    moveTo,
    clampTo,
    toggleFlexible,
    decision,
    submitDecision,
    exploreAnother,
    retry,
  };
}

export type SiteRun = ReturnType<typeof useSiteRun>;
