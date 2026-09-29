'use client';

import React, { useEffect, useRef, useState } from 'react';
import AppHeader from './AppHeader';
import PostcodeInput from './PostcodeInput';
import RunView from './RunView';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChevronDown, Film, LogIn, LogOut, PlayCircle, Sparkles } from 'lucide-react';
import { checkCapacity, startDemoRun } from '../../lib/api';
import { generateMockInspireParcels } from '../../lib/footprint';
import { geocodePostcode } from '../../lib/geocode';
import { CapacityChecker, DEFAULT_CENTER, useSiteRun } from '../../lib/useSiteRun';
import { useSiteData } from '../../lib/useSiteData';
import {
  COMPLETION_EVENTS,
  DEFAULT_PRESET,
  DEMO_PRESETS,
  DemoPreset,
  findPreset,
  simulateCapacityMove,
  simulateResult,
  simulateScreening,
} from '../../lib/demo/simulation';

// The capacity check reads free public data, so a moved pin gets the real answer; offline, it is simulated
const checkDemoCapacity: CapacityChecker = (pos, flexible, { origin, current }) =>
  checkCapacity(pos, flexible).catch(() => (current ? simulateCapacityMove(origin, pos, current, flexible) : null));

export type DemoPreview = 'confirm' | 'report';

interface DemoWorkspaceProps {
  /** Opens a fixed state for UI checks (`?state=confirm|report`) instead of the recorded replay. */
  preview?: DemoPreview | null;
  onExit: () => void;
  /** Offered while signed out; without it the header shows "Exit demo". */
  onSignIn?: () => void;
  signingIn?: boolean;
}

/**
 * Keyless demo: the preset sites replay runs recorded by the API, and every other postcode is simulated in the
 * browser. Map layers, title polygons and capacity re-checks read free public data. Nothing here calls a model or
 * needs a session.
 */
export default function DemoWorkspace({ preview, onExit, onSignIn, signingIn }: DemoWorkspaceProps) {
  const run = useSiteRun(checkDemoCapacity, DEFAULT_PRESET.coords); // the map opens on the first preset, not London
  const { siteData, siteDataLoading } = useSiteData(run.currentPosition);
  const [postcode, setPostcode] = useState(DEFAULT_PRESET.postcode);
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearTimers = () => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  };
  const schedule = (fn: () => void, delayMs: number) => {
    timersRef.current.push(setTimeout(fn, delayMs));
  };

  const startReplay = async (preset: DemoPreset = DEFAULT_PRESET) => {
    if (!preset.slug) return;
    clearTimers();
    setPostcode(preset.postcode);
    run.begin(preset.coords);
    run.setCapacityProposal(null);
    run.setStarting(true);
    try {
      const res = await startDemoRun(preset.slug);
      run.track(res.run_id);
    } catch (err) {
      run.setCapacityLoading(false);
      run.setErrorMsg(err instanceof Error ? err.message : 'Could not start the demo run.');
    } finally {
      run.setStarting(false);
    }
  };

  const startSimulation = async (target: string, coords?: [number, number], immediate = false) => {
    clearTimers();
    setPostcode(target);
    const center = coords ?? findPreset(target)?.coords ?? (await geocodePostcode(target)) ?? run.currentPosition;
    run.begin(center);
    const id = `sim-${Date.now().toString(36)}`;
    run.simulate(id);

    const sim = simulateScreening(target, center, run.flexibleConnection);
    for (const step of sim.steps) {
      schedule(() => {
        run.setIsStreaming(true);
        run.addEvents(step.events);
      }, immediate ? 0 : step.delayMs);
    }
    schedule(() => {
      if ('notViable' in sim.outcome) {
        run.setCapacityProposal(null);
        run.setRunStatus({ run_id: id, status: 'not_viable', message: sim.outcome.notViable });
      } else {
        const { capacity } = sim.outcome;
        run.setCapacityProposal(capacity);
        run.setSelectedCapacityMw(capacity.recommended_mw ?? 10);
        run.setInspireGeoJson(generateMockInspireParcels(center));
        run.setRunStatus({ run_id: id, status: 'awaiting_confirmation', capacity, position: center });
      }
      run.setCapacityLoading(false);
      run.setIsStreaming(false);
    }, immediate ? 0 : sim.steps[sim.steps.length - 1].delayMs);
  };

  const startSite = (target: string) => {
    const preset = findPreset(target);
    if (preset?.slug) void startReplay(preset);
    else void startSimulation(preset?.postcode ?? target, preset?.coords);
  };

  const restart = () => (postcode.trim() ? startSite(postcode.trim()) : void startReplay());

  const showReportPreview = () => {
    const sim = simulateScreening('SE1 7PB', DEFAULT_CENTER, false);
    if (!('capacity' in sim.outcome)) return;
    run.begin(DEFAULT_CENTER);
    run.simulate('run_demo_report');
    run.setCapacityLoading(false);
    run.setResult(simulateResult('run_demo_report', DEFAULT_CENTER, 12, sim.outcome.capacity, 'SE1 7PB'));
    run.setRunStatus({ run_id: 'run_demo_report', status: 'completed' });
  };

  // Recorded replays confirm through the API; simulated runs finish in the browser.
  const handleConfirm = () => {
    if (run.tracked) return void run.submitDecision();
    const { runId, capacityProposal, currentPosition, selectedCapacityMw } = run;
    if (!runId || !capacityProposal) return;
    run.setSubmittingDecision(true);
    run.setRunStatus({ run_id: runId, status: 'running' });
    run.addEvents(COMPLETION_EVENTS(selectedCapacityMw, currentPosition));
    schedule(() => {
      run.setResult(simulateResult(runId, currentPosition, selectedCapacityMw, capacityProposal, postcode.trim() || undefined));
      run.setRunStatus({ run_id: runId, status: 'completed' });
      run.setSubmittingDecision(false);
    }, 1000);
  };

  // Start once on mount; the ref survives React's dev double-mount. Pending timers (under 1 s) may outlive an unmount.
  const startedRef = useRef(false);
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps -- a mount-only kick-off */
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    if (preview === 'report') showReportPreview();
    else if (preview === 'confirm') void startSimulation('SE1 7PB', DEFAULT_CENTER, true);
    else void startReplay();
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col font-sans">
      <AppHeader onHome={restart} runId={run.runId} onResetRun={restart}>
        <Badge
          variant="outline"
          className="gap-1 text-[10px] font-bold uppercase tracking-wider bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30"
        >
          <Film className="w-3 h-3" /> Demo
        </Badge>
        {onSignIn ? (
          <Button
            size="sm"
            onClick={onSignIn}
            disabled={signingIn}
            className="bg-emerald-600 hover:bg-emerald-500 text-white gap-1.5 text-xs h-8 px-3 rounded-xl cursor-pointer shadow-xs"
          >
            <LogIn className="w-3.5 h-3.5" />
            <span>{signingIn ? 'Signing in…' : 'Sign in'}</span>
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            onClick={onExit}
            className="gap-1.5 text-xs h-8 px-3 rounded-xl border-border cursor-pointer hover:bg-muted/80 transition-colors"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span>Exit demo</span>
          </Button>
        )}
      </AppHeader>

      <main className="flex-1 max-w-[1800px] w-full mx-auto p-4 sm:p-6 space-y-6">
        <RunView
          run={run}
          siteData={siteData}
          siteDataLoading={siteDataLoading}
          onConfirm={handleConfirm}
          onReset={restart}
          mapZoom={findPreset(postcode)?.zoom}
          locationBar={
            <>
              <div className="flex flex-col md:flex-row gap-3">
                <PostcodeInput
                  value={postcode}
                  onChange={setPostcode}
                  onSubmit={() => startSite(postcode.trim())}
                  placeholder="UK postcode, e.g. CB24 9ZR"
                  disabled={run.starting}
                />
                <div className="relative min-w-[280px]">
                  <Sparkles className="absolute left-3.5 top-3.5 w-4 h-4 text-amber-500 pointer-events-none z-10" />
                  <select
                    aria-label="Select demo site"
                    value={findPreset(postcode)?.postcode ?? ''}
                    onChange={(e) => startSite(e.target.value)}
                    disabled={run.starting}
                    className="w-full h-11 pl-10 pr-9 bg-card border border-border text-foreground font-semibold rounded-xl text-sm shadow-xs appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-emerald-500/30 hover:border-emerald-500/40 transition-colors"
                  >
                    <option value="" disabled>
                      Choose a demo site
                    </option>
                    {DEMO_PRESETS.map((demo) => (
                      <option key={demo.postcode} value={demo.postcode}>
                        {demo.label}
                      </option>
                    ))}
                  </select>
                  <ChevronDown className="absolute right-3.5 top-3.5 w-4 h-4 text-muted-foreground pointer-events-none z-10" />
                </div>
              </div>

              <div className="p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-900 dark:text-amber-200 flex items-center gap-2">
                <PlayCircle className="w-4 h-4 shrink-0" />
                <span>
                  This is a <strong>demo</strong>: the example sites replay recorded runs, and other postcodes are simulated
                  in your browser. The map reads free public data; nothing here calls a model or uses your key.
                </span>
              </div>
            </>
          }
          errorAction={
            onSignIn && (
              <Button
                size="sm"
                variant="outline"
                onClick={onSignIn}
                disabled={signingIn}
                className="h-7 text-xs border-destructive/40 text-destructive hover:bg-destructive/10 shrink-0 cursor-pointer"
              >
                Sign in
              </Button>
            )
          }
        />
      </main>
    </div>
  );
}
