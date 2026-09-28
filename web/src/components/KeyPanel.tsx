'use client';

import React, { useEffect, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { CheckCircle2, KeyRound, Loader2, ShieldAlert, XCircle } from 'lucide-react';
import {
  deleteKey,
  deleteTavilyKey,
  getTavilyKeyStatus,
  KeyStatus,
  KeyTestResult,
  saveKey,
  saveTavilyKey,
  testKey,
  testTavilyKey,
} from '../lib/api';

interface KeyPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The saved Google key state, or null while it is still loading. */
  status: KeyStatus | null;
  onStatusChange: (status: KeyStatus) => void;
}

type Feedback = { kind: 'ok' | 'error'; text: string } | null;

const NO_KEY: KeyStatus = { configured: false, last4: null, updated_at: null };

/**
 * One provider's key: its own status, input, Test / Save / Delete buttons and feedback.
 * The dialog unmounts its content on close, so a typed key never outlives the panel.
 */
interface KeySectionProps {
  provider: string;
  label: string;
  hint: React.ReactNode;
  placeholder: string;
  /** Shown when no key is saved. */
  emptyText: string;
  status: KeyStatus | null;
  onStatusChange: (status: KeyStatus) => void;
  test: (key?: string) => Promise<KeyTestResult>;
  save: (key: string) => Promise<KeyStatus>;
  remove: () => Promise<void>;
}

function testMessage(provider: string, res: KeyTestResult): string {
  if (res.ok) return res.message || `${provider} accepted the key.`;
  if (res.message) return res.message;
  switch (res.error) {
    case 'rate_limited':
      return `${provider} accepted the key, but it is out of quota.`;
    case 'unreachable':
      return `${provider} could not be reached. Try again.`;
    case 'provider_error':
      return `${provider} returned an error. Try again.`;
    default:
      return `${provider} rejected the key.`;
  }
}

function KeySection({
  provider,
  label,
  hint,
  placeholder,
  emptyText,
  status,
  onStatusChange,
  test,
  save,
  remove,
}: KeySectionProps) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState<'test' | 'save' | 'delete' | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);

  const typed = draft.trim();

  const run = async (kind: 'test' | 'save' | 'delete', fn: () => Promise<void>) => {
    setBusy(kind);
    setFeedback(null);
    try {
      await fn();
    } catch (err) {
      setFeedback({ kind: 'error', text: err instanceof Error ? err.message : 'Something went wrong' });
    } finally {
      setBusy(null);
    }
  };

  const handleTest = () =>
    run('test', async () => {
      const res = await test(typed || undefined);
      setFeedback({ kind: res.ok ? 'ok' : 'error', text: testMessage(provider, res) });
    });

  // Save tests the key first, so a saved key is always one the provider accepted.
  const handleSave = () =>
    run('save', async () => {
      const res = await test(typed);
      if (!res.ok) throw new Error(testMessage(provider, res));
      onStatusChange(await save(typed));
      setDraft('');
      setFeedback({ kind: 'ok', text: `Key verified with ${provider} and saved.` });
    });

  const handleDelete = () =>
    run('delete', async () => {
      await remove();
      onStatusChange(NO_KEY);
      setFeedback({ kind: 'ok', text: 'Key deleted.' });
    });

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        <span className="text-xs text-muted-foreground">
          {status === null ? (
            'Checking…'
          ) : status.configured ? (
            <span className="flex items-center gap-1.5 text-foreground">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
              Saved <span className="font-mono">••••{status.last4}</span>
            </span>
          ) : (
            emptyText
          )}
        </span>
      </div>
      <p className="text-[11px] text-muted-foreground">{hint}</p>

      <Input
        type="password"
        autoComplete="off"
        spellCheck={false}
        placeholder={status?.configured ? 'Paste a new key to replace it' : placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className="font-mono text-xs h-9"
      />

      <div className="flex items-center justify-between gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={handleDelete}
          disabled={!!busy || !status?.configured}
          className="text-destructive hover:text-destructive"
        >
          {busy === 'delete' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          Delete
        </Button>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleTest}
            disabled={!!busy || (typed.length === 0 && !status?.configured)}
          >
            {busy === 'test' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Test
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={handleSave}
            disabled={!!busy || typed.length === 0}
            className="bg-emerald-600 hover:bg-emerald-500 text-white"
          >
            {busy === 'save' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {busy === 'save' ? 'Testing & saving…' : 'Save'}
          </Button>
        </div>
      </div>

      {feedback && (
        <div
          role="status"
          className={`flex items-start gap-1.5 text-xs ${
            feedback.kind === 'ok' ? 'text-emerald-700 dark:text-emerald-300' : 'text-destructive'
          }`}
        >
          {feedback.kind === 'ok' ? (
            <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          ) : (
            <XCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          )}
          <span>{feedback.text}</span>
        </div>
      )}
    </section>
  );
}

export default function KeyPanel({ open, onOpenChange, status, onStatusChange }: KeyPanelProps) {
  // The optional Tavily key: loaded each time the panel opens.
  const [tavilyStatus, setTavilyStatus] = useState<KeyStatus | null>(null);

  useEffect(() => {
    if (!open) return;
    let stale = false;
    getTavilyKeyStatus()
      .then((s) => !stale && setTavilyStatus(s))
      .catch(() => !stale && setTavilyStatus(NO_KEY));
    return () => {
      stale = true;
    };
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-emerald-600" />
            API keys
          </DialogTitle>
          <DialogDescription>
            Bessible runs on your own keys, so the team&apos;s quota is never used.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <KeySection
            provider="Gemini"
            label="Google AI key"
            hint="Required. Every run reasons with Gemini on this key."
            placeholder="Paste your Google AI (Gemini) key"
            emptyText="No key saved"
            status={status}
            onStatusChange={onStatusChange}
            test={testKey}
            save={saveKey}
            remove={deleteKey}
          />

          <div className="border-t border-border" />

          <KeySection
            provider="Tavily"
            label="Tavily key (optional)"
            hint="Local news search and blocked listing pages go through Tavily. Without a key, runs use the server's."
            placeholder="tvly-…"
            emptyText="Using the server key"
            status={tavilyStatus}
            onStatusChange={setTavilyStatus}
            test={testTavilyKey}
            save={saveTavilyKey}
            remove={deleteTavilyKey}
          />

          <p className="flex items-start gap-1.5 text-[11px] text-muted-foreground">
            <ShieldAlert className="w-3.5 h-3.5 mt-0.5 shrink-0 text-amber-500" />
            <span>
              Keys are stored encrypted on the server and are never shown again. Use restricted or throwaway keys.
            </span>
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
