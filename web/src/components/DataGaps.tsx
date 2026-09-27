'use client';

import React from 'react';
import { DataGap } from '../lib/types';
import { StageBadge } from '../lib/stages';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { AlertTriangle, RefreshCw } from 'lucide-react';

interface DataGapsProps {
  gaps: DataGap[];
  retriesLeft: number;
  /** Absent for recorded replays, which cannot be re-run. */
  onRetry?: (stages: string[]) => void;
  retrying?: boolean;
}

/** What the run could not find out, grouped by stage, with a retry for stages whose sources failed. */
export default function DataGaps({ gaps, retriesLeft, onRetry, retrying = false }: DataGapsProps) {
  if (gaps.length === 0) return null;
  const stages = [...new Set(gaps.map((g) => g.stage))];

  return (
    <Card className="border-amber-500/30 bg-card shadow-xs rounded-2xl overflow-hidden">
      <CardHeader className="p-4 border-b border-border/70 bg-amber-500/5">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-1">
          <CardTitle className="text-sm font-bold flex items-center gap-2 text-foreground">
            <AlertTriangle className="w-4 h-4 text-amber-600" />
            <span>Data Gaps ({gaps.length})</span>
          </CardTitle>
          <span className="text-xs text-muted-foreground">
            {onRetry && retriesLeft > 0
              ? `Temporary gaps can be retried (${retriesLeft} ${retriesLeft === 1 ? 'retry' : 'retries'} left)`
              : 'Evidence this assessment could not obtain'}
          </span>
        </div>
      </CardHeader>
      <CardContent className="p-4 space-y-4 text-xs">
        {stages.map((stage) => {
          const stageGaps = gaps.filter((g) => g.stage === stage);
          const canRetry = !!onRetry && retriesLeft > 0 && stageGaps.some((g) => g.retryable);
          return (
            <div key={stage} className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <StageBadge stage={stage} size="md" />
                {canRetry && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={retrying}
                    onClick={() => onRetry?.([stage])}
                    className="text-xs font-semibold gap-1.5 h-8 rounded-xl"
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${retrying ? 'animate-spin' : ''}`} />
                    <span>{retrying ? 'Retrying…' : 'Retry'}</span>
                  </Button>
                )}
              </div>
              <ul className="space-y-1.5">
                {stageGaps.map((g) => (
                  <li key={g.what} className="flex items-start gap-2 text-muted-foreground">
                    <span
                      className={`shrink-0 px-1.5 py-0.5 rounded border text-[10px] font-bold uppercase ${
                        g.retryable
                          ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40'
                          : 'bg-muted text-muted-foreground border-border'
                      }`}
                    >
                      {g.retryable ? 'Temporary' : 'No coverage'}
                    </span>
                    <span>
                      <strong className="text-foreground">{g.what.replace(/_/g, ' ')}</strong>
                      {g.could_block && <span className="text-amber-700 dark:text-amber-300"> · could hide a blocker</span>}
                      {': '}
                      {g.reason}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
