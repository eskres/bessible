'use client';

import { useEffect, useRef, useState } from 'react';
import { AssessmentResult, CapacityOutput, RunStatus, SiteDecision, TraceEvent } from './types';
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

  /** Clears the previous run and centres the map. `pin` keeps a user-placed pin instead of the centre. */
  const begin = (center: [number, number], pin?: [number, number]) => {
    releaseRun();
    setErrorMsg(null);
    setResult(null);
    setEvents([]);
    setRunStatus(null);
    setInspireGeoJson(null);
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

  const decision = (): SiteDecision => ({
    confirmed: true,
    position: currentPosition,
    capacity_mw: selectedCapacityMw,
    footprint_acres: selectedCapacityMw * 4 * 0.0625,
    flexible_connection: flexibleConnection,
  });

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
