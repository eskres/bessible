'use client';

import React, { useEffect, useRef, useState } from 'react';
import { TraceEvent } from '../lib/types';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { StageBadge } from '../lib/stages';
import {
  Activity,
  Clock,
  Terminal,
  ArrowDown,
  CheckCircle2,
  Hand,
  XCircle,
} from 'lucide-react';

interface LiveTraceProps {
  events: TraceEvent[];
  isConnected?: boolean;
  status?: string;
  /** Temporal workflow id of the run, shown under the title once a run exists. */
  runId?: string | null;
  /** Fill the parent's height and scroll inside it (large screens); otherwise the list caps at a fixed height. */
  fill?: boolean;
}

/** How the stream ended, or why it is paused; null while the agents are still working. */
const RUN_STATE: Record<string, { label: string; tone: 'done' | 'paused' | 'stopped' }> = {
  awaiting_confirmation: { label: 'Paused: waiting for your site confirmation', tone: 'paused' },
  completed: { label: 'Pipeline complete: report ready', tone: 'done' },
  not_viable: { label: 'Run ended: no viable grid connection', tone: 'stopped' },
  rejected: { label: 'Run ended: site declined', tone: 'stopped' },
  out_of_area: { label: 'Run ended: location outside the screened area', tone: 'stopped' },
  failed: { label: 'Run failed', tone: 'stopped' },
};

const TONE_STYLE = {
  done: { className: 'text-emerald-600 dark:text-emerald-400 border-emerald-500/30 bg-emerald-500/10', icon: CheckCircle2 },
  paused: { className: 'text-amber-700 dark:text-amber-300 border-amber-500/30 bg-amber-500/10', icon: Hand },
  stopped: { className: 'text-muted-foreground border-border bg-muted/40', icon: XCircle },
};

export default function LiveTrace({
  events,
  isConnected = false,
  status = 'running',
  runId = null,
  fill = false,
}: LiveTraceProps) {
  const runState = RUN_STATE[status] ?? null;
  const working = !runState && (isConnected || status === 'running');
  const lastEventAt = events.length ? events[events.length - 1].t : null;
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  return (
    <Card className="flex flex-col h-full shadow-md rounded-2xl overflow-hidden border-border bg-card">
      <CardHeader className="p-4 border-b border-border/80 bg-muted/20">
        <div className="flex flex-col items-start gap-2">
          <div className="flex items-center min-w-0 max-w-full">
            {runState ? (
              <Badge variant="outline" className={`flex items-center gap-1 text-[10px] font-semibold px-[7px] py-0.5 max-w-full ${TONE_STYLE[runState.tone].className}`}>
                {React.createElement(TONE_STYLE[runState.tone].icon, { className: 'w-[11px] h-[11px] shrink-0' })}
                <span className="capitalize truncate">{status.replace(/_/g, ' ')}</span>
              </Badge>
            ) : isConnected ? (
              <Badge variant="outline" className="flex items-center gap-[5px] text-[10px] text-emerald-600 dark:text-emerald-400 font-semibold border-emerald-500/30 bg-emerald-500/10 px-[7px] py-0.5">
                <span className="relative flex h-[7px] w-[7px]">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-[7px] w-[7px] bg-emerald-500"></span>
                </span>
                SSE Active
              </Badge>
            ) : (
              <Badge variant="outline" className="text-[10px] text-muted-foreground font-mono flex items-center gap-1 px-[7px] py-0.5 max-w-full" title={status}>
                <Clock className="w-[11px] h-[11px] shrink-0" />
                <span className="capitalize truncate">{status.replace(/_/g, ' ')}</span>
              </Badge>
            )}
          </div>

          <div className="flex items-center gap-2 min-w-0 max-w-full">
            <div className="p-1 rounded-md bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
              <Terminal className="w-4 h-4" />
            </div>
            <div className="min-w-0">
              <CardTitle className="text-sm font-bold tracking-tight">Agent Telemetry</CardTitle>
              <div className="text-[10px] text-muted-foreground font-mono truncate" title={runId ?? undefined}>
                {runId ?? 'Temporal Workflow Stream'}
              </div>
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="p-0 flex-1 min-h-0 flex flex-col justify-between">
        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            setAutoScroll(isNearBottom);
          }}
          className={`p-4 overflow-y-auto space-y-2.5 font-mono text-xs max-h-[460px] min-h-[300px] ${
            fill ? 'lg:flex-1 lg:max-h-none lg:min-h-0' : ''
          }`}
        >
          {events.length === 0 ? (
            <div className="text-muted-foreground text-center py-16 font-sans text-xs flex flex-col items-center gap-2">
              <Activity className="w-6 h-6 text-muted-foreground/40 animate-pulse" />
              <span>Awaiting pipeline execution events...</span>
              <span className="text-[10px] text-muted-foreground/70">Enter a UK postcode or place the pin on the map</span>
            </div>
          ) : (
            events.map((ev) => {
              return (
                <div
                  key={ev.id}
                  className="flex items-start gap-2.5 p-2 rounded-xl bg-muted/20 border border-border/50 hover:bg-muted/40 transition"
                >
                  <span className="text-muted-foreground/60 text-[10px] w-5 shrink-0 text-right pt-0.5">
                    {ev.id}
                  </span>

                  <div className="shrink-0 flex items-center gap-1">
                    <StageBadge stage={ev.stage} />
                  </div>

                  <div className="flex-1 min-w-0 text-foreground text-xs leading-relaxed font-sans pt-0.5 [overflow-wrap:anywhere]">
                    {ev.msg}
                  </div>

                  <span className="text-muted-foreground/70 text-[10px] shrink-0 font-mono pt-0.5">
                    {ev.t
                      ? new Date(ev.t).toLocaleTimeString([], {
                          hour: '2-digit',
                          minute: '2-digit',
                          second: '2-digit',
                        })
                      : ''}
                  </span>
                </div>
              );
            })
          )}

          {working && events.length > 0 && (
            <div className="flex items-center gap-2 pt-1 text-[11px] text-emerald-600 dark:text-emerald-400 font-mono animate-pulse">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
              <span>Autonomous agent processing stage...</span>
            </div>
          )}

          {runState && events.length > 0 && (
            <div
              className={`flex items-center gap-2 p-2 rounded-xl border text-[11px] font-semibold font-sans ${TONE_STYLE[runState.tone].className}`}
            >
              {React.createElement(TONE_STYLE[runState.tone].icon, { className: 'w-4 h-4 shrink-0' })}
              <span className="flex-1">{runState.label}</span>
              {runState.tone !== 'paused' && lastEventAt && (
                <span className="text-[10px] font-mono font-normal opacity-80">
                  {new Date(lastEventAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Trace Footer Telemetry info */}
        <div className="p-3 border-t border-border/60 bg-muted/10 flex items-center justify-between text-[11px] text-muted-foreground font-mono">
          <span>{events.length} Events Logged</span>
          {!autoScroll && (
            <button
              type="button"
              onClick={() => {
                setAutoScroll(true);
                if (scrollRef.current) {
                  scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
                }
              }}
              className="text-emerald-600 dark:text-emerald-400 flex items-center gap-1 hover:underline cursor-pointer"
            >
              <span>Scroll to live</span>
              <ArrowDown className="w-3 h-3" />
            </button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
