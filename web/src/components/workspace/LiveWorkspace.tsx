'use client';

import React, { useEffect, useState } from 'react';
import KeyPanel from '../KeyPanel';
import FirstLoadModal from '../FirstLoadModal';
import AppHeader from './AppHeader';
import PostcodeInput from './PostcodeInput';
import RunView from './RunView';
import { Button } from '@/components/ui/button';
import { LogOut, Search, Settings } from 'lucide-react';
import { AssessmentRequest } from '../../lib/types';
import { ApiError, KeyStatus, checkCapacity, getKeyStatus, startRun } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { geocodePostcode } from '../../lib/geocode';
import { CapacityChecker, useSiteRun } from '../../lib/useSiteRun';
import { useSiteData } from '../../lib/useSiteData';

const checkLiveCapacity: CapacityChecker = (pos, flexible) => checkCapacity(pos, flexible).catch(() => null);


interface LiveWorkspaceProps {
  onViewDemo: () => void;
  welcomeDismissed: boolean;
  onDismissWelcome: () => void;
}

/** Real assessments against the API. Mounted per user (keyed on uid), so state never leaks between sessions. */
export default function LiveWorkspace({ onViewDemo, welcomeDismissed, onDismissWelcome }: LiveWorkspaceProps) {
  const auth = useAuth();
  const user = auth.user;
  const run = useSiteRun(checkLiveCapacity);
  const { siteData, siteDataLoading } = useSiteData(run.currentPosition);
  // Empty text means the pin is the site; placing the pin clears the text
  const [postcode, setPostcode] = useState('SE1 7PB');
  // No run holds the site: the pin can go anywhere
  const freePlacement = !run.starting && !['running', 'awaiting_confirmation'].includes(run.runStatus?.status ?? '');

  // Google key (BYOK), for the signed-in user only. Local mode (no Firebase config) has no key UI.
  const [keyStatus, setKeyStatus] = useState<KeyStatus | null>(null);
  const [keyPanelOpen, setKeyPanelOpen] = useState(false);

  const uid = user?.uid;
  useEffect(() => {
    if (!uid) return;
    let stale = false;
    getKeyStatus()
      .then((status) => !stale && setKeyStatus(status))
      .catch(() => !stale && setKeyStatus({ configured: false, last4: null, updated_at: null }));
    return () => {
      stale = true;
    };
  }, [uid]);

  const handleStartRun = async (flexibleOverride?: boolean) => {
    const input = postcode.trim();
    const isUrl = /^https?:\/\//.test(input);
    const flexible = flexibleOverride ?? run.flexibleConnection;
    const [lon, lat] = run.currentPosition;

    run.setStarting(true);
    const center = (input && !isUrl && (await geocodePostcode(input))) || run.currentPosition;
    run.begin(center);

    try {
      const payload: AssessmentRequest = !input
        ? { position: { lat, lon }, flexible_connection: flexible }
        : isUrl
          ? { link: input, property_url: input, flexible_connection: flexible }
          : { postcode: input, flexible_connection: flexible };
      const res = await startRun(payload);
      run.track(res.run_id);
    } catch (err) {
      run.setCapacityLoading(false);
      if (err instanceof ApiError && err.status === 401 && JSON.stringify(err.data).includes('missing_google_key')) {
        setKeyStatus({ configured: false, last4: null, updated_at: null });
        setKeyPanelOpen(true);
        run.setErrorMsg('Add your Google AI key in Settings to run a live assessment.');
      } else if (err instanceof ApiError && err.status === 401) {
        run.setErrorMsg('Your session has expired. Sign in again to continue.');
      } else {
        run.setErrorMsg(err instanceof Error ? err.message : 'Could not start the assessment.');
      }
    } finally {
      run.setStarting(false);
    }
  };

  const handleReset = () => {
    run.reset();
    setPostcode('');
  };

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col font-sans">
      {user && (
        <>
          <KeyPanel open={keyPanelOpen} onOpenChange={setKeyPanelOpen} status={keyStatus} onStatusChange={setKeyStatus} />
          <FirstLoadModal
            open={keyStatus?.configured === false && !welcomeDismissed && !keyPanelOpen}
            onConfigureKey={() => setKeyPanelOpen(true)}
            onViewDemo={onViewDemo}
            onDismiss={onDismissWelcome}
          />
        </>
      )}

      <AppHeader onHome={handleReset} runId={run.runId} onResetRun={handleReset}>
        {user && (
          <div className="flex items-center gap-2 text-xs">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setKeyPanelOpen(true)}
              className="gap-1.5 text-xs h-8 px-2.5 sm:px-3 rounded-xl border-border cursor-pointer hover:bg-muted/80 transition-colors"
              title="Settings & Google AI Key"
              aria-label="Settings"
            >
              <Settings className="w-3.5 h-3.5 text-muted-foreground" />
              <span className="hidden sm:inline">Settings</span>
              {keyStatus?.configured && (
                <span className="text-[10px] font-mono bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 px-1.5 py-0.5 rounded border border-emerald-500/20">
                  ••••{keyStatus.last4}
                </span>
              )}
            </Button>
            <span
              className="text-muted-foreground hidden md:inline max-w-[160px] truncate text-[11px] font-medium"
              title={user.email ?? undefined}
            >
              {user.email}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void auth.signOut()}
              className="gap-1.5 text-xs h-8 px-2.5 rounded-xl cursor-pointer text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Logout</span>
            </Button>
          </div>
        )}
      </AppHeader>

      <main className="flex-1 max-w-[1800px] w-full mx-auto p-4 sm:p-6 space-y-6">
        <RunView
          run={run}
          onConfirm={() => void run.submitDecision()}
          onReset={handleReset}
          onRetryFlexible={() => void handleStartRun(true)}
          locationBar={
            <div className="flex flex-col md:flex-row gap-3">
              <PostcodeInput
                value={postcode}
                onChange={setPostcode}
                onSubmit={() => void handleStartRun()}
                placeholder="UK postcode or property URL"
                disabled={run.starting}
              />
              <Button
                type="button"
                onClick={() => void handleStartRun()}
                disabled={run.starting}
                className="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-6 h-11 rounded-xl gap-2 shadow-xs cursor-pointer text-sm"
              >
                <Search className="w-4 h-4 stroke-[2.5]" />
                <span>{run.starting ? 'Screening Grid...' : 'Screen Location'}</span>
              </Button>
            </div>
          }
          siteData={siteData}
          siteDataLoading={siteDataLoading}
          freePlacement={freePlacement}
          onPinPlaced={() => setPostcode('')}
        />
      </main>
    </div>
  );
}
