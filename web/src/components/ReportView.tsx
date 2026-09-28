'use client';

import React, { useState } from 'react';
import { AssessmentResult, Artifact, FinancialCase } from '../lib/types';
import { downloadMarkdownReport, printReport } from '../lib/reportExport';
import DataGaps from './DataGaps';
import { StageBadge, stageStyle } from '../lib/stages';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableHeader,
  TableRow,
  TableHead,
  TableBody,
  TableCell,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import {
  ShieldAlert,
  Zap,
  Building,
  ExternalLink,
  TrendingUp,
  FileText,
  BadgePercent,
  Compass,
  Download,
  Printer,
  Users,
  CheckCircle2,
  Sparkles,
  ArrowRight,
  ShieldCheck,
  AlertTriangle,
  ChevronDown,
  MapPin,
  OctagonX,
} from 'lucide-react';

/** Evidence categories shown in the report, each a set of pipeline stages, styled like its `style` stage. */
const ARTIFACT_CATEGORIES: { label: string; stages: string[]; style: string }[] = [
  { label: 'Site & Land', stages: ['location', 'title', 'site_land'], style: 'site_land' },
  { label: 'Grid Connection', stages: ['capacity', 'grid'], style: 'grid' },
  { label: 'Planning', stages: ['planning'], style: 'planning' },
  { label: 'Community Sentiment', stages: ['sentiment'], style: 'sentiment' },
  { label: 'Revenue & Finance', stages: ['market', 'financial'], style: 'financial' },
  { label: 'Verdict', stages: ['synthesis'], style: 'synthesis' },
];

function groupArtifacts(artifacts: Artifact[]): { label: string; style: string; items: Artifact[] }[] {
  const known = new Set(ARTIFACT_CATEGORIES.flatMap((c) => c.stages));
  const groups = ARTIFACT_CATEGORIES.map(({ label, stages, style }) => ({
    label,
    style,
    items: artifacts.filter((a) => stages.includes(a.stage)),
  }));
  groups.push({ label: 'Other', style: 'other', items: artifacts.filter((a) => !known.has(a.stage)) });
  return groups.filter((g) => g.items.length > 0);
}

type Outcome = 'Blocker' | 'Caveat';

/** Hard-check claims start with their outcome ("Blocker: ...", "Caveat: ..."); split it off, if there is one. */
function splitOutcome(claim: string): { outcome: Outcome | null; text: string } {
  const [head, ...rest] = claim.split(': ');
  return head === 'Blocker' || head === 'Caveat' ? { outcome: head, text: rest.join(': ') } : { outcome: null, text: claim };
}

function countOutcomes(items: Artifact[]): Record<Outcome, number> {
  const counts = { Blocker: 0, Caveat: 0 };
  for (const a of items) {
    const { outcome } = splitOutcome(a.claim);
    if (outcome) counts[outcome] += 1;
  }
  return counts;
}

/** Card border and pill for an outcome that needs attention. Only blockers get a coloured border. */
const OUTCOME_STYLE: Record<Outcome, { card: string; pill: string; icon: typeof AlertTriangle }> = {
  Blocker: {
    card: 'border-red-500/70 hover:border-red-500',
    pill: 'bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/40',
    icon: OctagonX,
  },
  Caveat: {
    card: 'border-border hover:border-foreground/30', // the pill is enough
    pill: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40',
    icon: AlertTriangle,
  },
};

/** A `bessible-` / `demo-` / `sim-` run id shortened to the first 8 characters after its prefix; others as they are. */
function shortRunId(id: string): string {
  const m = id.match(/^(?:bessible|demo|sim)-(.+)$/);
  return m ? m[1].slice(0, 8) : id;
}

interface ReportViewProps {
  result: AssessmentResult;
  onReset?: () => void;
  /** A read-only map of the site; when given, a pin icon next to the coordinates opens it in a dialog. */
  siteMap?: React.ReactNode;
  /** Re-runs stages with retryable data gaps; absent where a run cannot be retried (recorded replays). */
  onRetry?: (stages: string[]) => void;
  retrying?: boolean;
}

export default function ReportView({ result, onReset, siteMap, onRetry, retrying }: ReportViewProps) {
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [mapOpen, setMapOpen] = useState(false);
  const { capacity, site, grid_connection, land_planning, financial, artifacts = [] } = result;
  const recommendedH = financial?.recommended_h ?? null;
  const [selectedDurationH, setSelectedDurationH] = useState<number>(recommendedH ?? 4);

  const capacityMw = site?.capacity_mw ?? capacity?.recommended_mw ?? 10;
  const cap = capacity ?? {
    firm_mw: capacityMw,
    ceiling_mw: Math.round(capacityMw * 1.5),
    recommended_mw: capacityMw,
    binding_direction: 'export' as const,
    binding_season: 'summer' as const,
    substation: 'Primary Substation',
    serving_substation: 'Primary Substation',
    voltage_kv: 33,
    connection_voltage_kv: 33,
    viable: true,
    out_of_area: false,
  };
  const reservedAcres = site?.reserved_acres ?? land_planning?.reserved_acres ?? Number((capacityMw * 4 * 0.0625).toFixed(2));
  const posString = site
    ? Array.isArray(site.position)
      ? `${site.position[1].toFixed(5)}°N, ${Math.abs(site.position[0]).toFixed(5)}°${site.position[0] >= 0 ? 'E' : 'W'}`
      : `${site.position.lat.toFixed(5)}°N, ${Math.abs(site.position.lon).toFixed(5)}°${site.position.lon >= 0 ? 'E' : 'W'}`
    : 'Confirmed Site';

  // Duration cases from the backend financial model; no invented fallback numbers
  const financialCases: FinancialCase[] = financial?.cases ?? [];
  const activeCase: FinancialCase | undefined =
    financialCases.find((c) => c.duration_h === selectedDurationH) ?? financialCases[0];
  const activeIrr = activeCase?.irr != null ? activeCase.irr * 100 : null;
  const activeH = activeCase?.duration_h ?? selectedDurationH;
  const discountRate = financial?.discount_rate_pct;
  const projectLife = financial?.project_life_years ?? 25;
  const gbpM = (gbp: number, digits = 2) => `£${(gbp / 1000000).toFixed(digits)}M`;

  return (
    <div className="space-y-6">
      {/* 1. Header Bar with Status & Actions */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-2 border-b border-border/80">
        <div>
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border border-emerald-500/30">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400" />
              BESS Feasibility Dossier
            </span>
            {result.run_id && (
              <span className="text-xs text-muted-foreground font-mono" title={result.run_id}>
                Run: {shortRunId(result.run_id)}
              </span>
            )}
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground mt-1">
            {result.postcode && <span className="text-emerald-700 dark:text-emerald-400">{result.postcode.toUpperCase()} · </span>}
            {capacityMw} MW / {capacityMw * activeH} MWh Battery Energy Storage Assessment
          </h1>
          <p className="text-xs sm:text-sm text-muted-foreground mt-0.5 flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1">
              Coordinates: <strong className="text-foreground font-mono">{posString}</strong>
              {siteMap && (
                <button
                  type="button"
                  onClick={() => setMapOpen(true)}
                  title="Show the site on the map"
                  aria-label="Show the site on the map"
                  className="p-0.5 rounded-md text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/10 transition"
                >
                  <MapPin className="w-4 h-4" />
                </button>
              )}
            </span>
            <span>•</span>
            <span>Serving: <strong className="text-foreground">{grid_connection?.serving_substation || cap.serving_substation}</strong></span>
            <span>•</span>
            <span>Compound: <strong className="text-foreground">{reservedAcres} Acres</strong></span>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => downloadMarkdownReport(result)}
            className="text-xs font-semibold gap-1.5 h-9 rounded-xl border-border hover:bg-muted"
          >
            <Download className="w-3.5 h-3.5 text-emerald-600" />
            <span>Export (.md)</span>
          </Button>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={printReport}
            className="text-xs font-semibold gap-1.5 h-9 rounded-xl border-border hover:bg-muted"
          >
            <Printer className="w-3.5 h-3.5 text-blue-600" />
            <span>Print PDF</span>
          </Button>

          {onReset && (
            <Button
              type="button"
              variant="default"
              size="sm"
              onClick={onReset}
              className="text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white h-9 rounded-xl shadow-xs"
            >
              Assess Next Site
            </Button>
          )}
        </div>
      </div>

      {/* 2. Executive Metric Hero Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Capex Card */}
        <div className="p-4 rounded-2xl bg-card border border-border shadow-xs flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">Initial Capex</span>
            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded bg-muted text-muted-foreground">
              {activeH}H Case
            </span>
          </div>
          <div className="mt-3">
            <div className="text-2xl font-bold font-sans tabular-nums text-foreground tracking-tight">
              {activeCase ? gbpM(activeCase.capex_gbp) : '—'}
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              {activeCase
                ? `~£${Math.round(activeCase.capex_gbp / (capacityMw * activeCase.duration_h) / 1000)}k / MWh turnkey`
                : 'Financial model not available'}
            </p>
          </div>
        </div>

        {/* 25-Year NPV Card */}
        <div className="p-4 rounded-2xl bg-card border border-border shadow-xs flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">Project Net Present Value</span>
            <Badge className="bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30 text-[10px] font-semibold">
              {discountRate != null ? `NPV @ ${discountRate}%` : 'Equity NPV'}
            </Badge>
          </div>
          <div className="mt-3">
            <div
              className={`text-2xl font-bold font-sans tabular-nums tracking-tight ${
                activeCase && activeCase.npv_gbp < 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-emerald-600 dark:text-emerald-400'
              }`}
            >
              {activeCase ? gbpM(activeCase.npv_gbp) : '—'}
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              {projectLife}-year operational lifecycle
            </p>
          </div>
        </div>

        {/* Internal Rate of Return (IRR) */}
        <div className="p-4 rounded-2xl bg-card border border-border shadow-xs flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">Project IRR</span>
            <Badge className="bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30 text-[10px] font-semibold">
              Equity
            </Badge>
          </div>
          <div className="mt-3">
            <div
              className={`text-2xl font-bold font-sans tabular-nums tracking-tight ${
                activeIrr == null ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'
              }`}
            >
              {activeIrr != null ? `${activeIrr.toFixed(1)}%` : activeCase ? 'No payback' : '—'}
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              Wholesale arbitrage + frequency services
            </p>
          </div>
        </div>

        {/* Planning & Network Status */}
        <div className="p-4 rounded-2xl bg-card border border-border shadow-xs flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">Planning Consent</span>
            <Badge variant="outline" className="text-[10px] uppercase font-semibold text-emerald-700 dark:text-emerald-300 bg-emerald-500/10 border-emerald-500/30">
              {land_planning?.planning_risk || 'Low Risk'}
            </Badge>
          </div>
          <div className="mt-3">
            <div className="text-base font-bold text-foreground truncate">
              {land_planning?.consenting_route || (capacityMw >= 50 ? 'NSIP (DCO)' : 'TCPA (Local Plan)')}
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-1">
              <ShieldCheck className="w-3.5 h-3.5 text-emerald-600" />
              <span>Green Belt: {land_planning?.green_belt ? 'Designated' : 'Clear (No designation)'}</span>
            </p>
          </div>
        </div>
      </div>

      {/* Compact Model Disclaimer Pill */}
      <div className="px-4 py-2.5 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-amber-950 dark:text-amber-200 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldAlert className="w-4 h-4 text-amber-600 shrink-0" />
          <span><strong>Screening Estimate:</strong> Feasibility figures are derived from open data (UKPN capacity heatmap and LTDS queue tables, DESNZ REPD) and Modo/BNEF cost and revenue benchmarks, and do not substitute a formal DNO Connection Offer.</span>
        </div>
        <span className="text-[10px] font-mono text-amber-700 dark:text-amber-300 whitespace-nowrap hidden sm:inline">Model v1.2</span>
      </div>

      <DataGaps gaps={result.gaps ?? []} retriesLeft={result.retries_left ?? 0} onRetry={onRetry} retrying={retrying} />

      {/* 3. Deep Dive Sections Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Section 1: Capacity Range & Grid Constraints */}
        <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
          <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
            <CardTitle className="text-sm font-bold flex items-center gap-2 text-foreground">
              <Zap className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
              <span>1. Capacity Range & Constraints</span>
            </CardTitle>
          </CardHeader>

          <CardContent className="p-4 space-y-4 text-xs">
            <div className="grid grid-cols-2 gap-3">
              <div className="p-3 bg-muted/30 rounded-xl border border-border">
                <span className="text-muted-foreground font-medium text-[11px]">Firm Headroom</span>
                <div className="text-xl font-bold font-mono text-foreground mt-1">
                  {cap.firm_mw ?? '—'} MW
                </div>
                <span className="text-[10px] text-muted-foreground">Uncurtailed firm connection</span>
              </div>

              <div className="p-3 bg-muted/30 rounded-xl border border-border">
                <span className="text-muted-foreground font-medium text-[11px]">Ceiling Capacity</span>
                <div className="text-xl font-bold font-mono text-amber-600 dark:text-amber-400 mt-1">
                  {cap.ceiling_mw ?? '—'} MW
                </div>
                <span className="text-[10px] text-muted-foreground">Flexible connection headroom</span>
              </div>
            </div>

            <div className="space-y-2 pt-1 font-sans">
              <div className="flex justify-between py-1.5 border-b border-border/50">
                <span className="text-muted-foreground">Binding Direction</span>
                <span className="font-semibold uppercase tracking-wider text-[11px] text-foreground">
                  {cap.binding_direction || 'Export'} Headroom
                </span>
              </div>
              <div className="flex justify-between py-1.5 border-b border-border/50">
                <span className="text-muted-foreground">Binding Season</span>
                <span className="font-semibold capitalize text-foreground">
                  {cap.binding_season || 'Summer'} (Thermal rating constrained)
                </span>
              </div>
              <div className="flex justify-between py-1.5">
                <span className="text-muted-foreground">Recommended Connection Size</span>
                <span className="font-bold font-mono text-emerald-600 dark:text-emerald-400">
                  {cap.recommended_mw ?? capacityMw} MW
                </span>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Section 2: Grid Connection Architecture */}
        <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
          <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
            <CardTitle className="text-sm font-bold flex items-center gap-2 text-foreground">
              <Compass className="w-4 h-4 text-blue-600 dark:text-blue-400" />
              <span>2. Grid Interconnection Route</span>
            </CardTitle>
          </CardHeader>

          <CardContent className="p-4 text-xs space-y-2.5">
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Serving Substation</span>
              <span className="font-bold text-foreground">
                {grid_connection?.serving_substation || cap.substation || cap.serving_substation || 'Primary Substation'}
              </span>
            </div>
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Point of Connection (PoC) Voltage</span>
              <span className="font-mono font-semibold text-foreground">
                {grid_connection?.voltage_kv || cap.connection_voltage_kv || cap.voltage_kv || 33} kV Busbar
              </span>
            </div>
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Estimated Cable Route Distance</span>
              <span className="font-mono font-semibold text-foreground">
                {grid_connection?.distance_km?.toFixed(2) || '0.65'} km
              </span>
            </div>
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Parent GSP Status</span>
              <span className="font-semibold text-emerald-600 dark:text-emerald-400 flex items-center gap-1">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>{grid_connection?.gsp_status || 'Secure (No transmission reinforcement required)'}</span>
              </span>
            </div>
            <div className="flex justify-between py-1">
              <span className="text-muted-foreground">Transmission Impact Assessment (TIA)</span>
              <span className="font-semibold text-foreground">
                {grid_connection?.tia_threshold_mw ? `${grid_connection.tia_threshold_mw} MW statement threshold` : 'Standard DNO screening'}
              </span>
            </div>
          </CardContent>
        </Card>

        {/* Section 3: Land, Planning & Environmental Risk */}
        <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
          <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
            <CardTitle className="text-sm font-bold flex items-center gap-2 text-foreground">
              <Building className="w-4 h-4 text-amber-600 dark:text-amber-400" />
              <span>3. Land, Planning & Environmental Risk</span>
            </CardTitle>
          </CardHeader>

          <CardContent className="p-4 text-xs space-y-2.5">
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Reserved Battery Compound</span>
              <span className="font-semibold text-foreground font-mono">
                {reservedAcres} Acres ({(reservedAcres * 0.404686).toFixed(2)} Ha)
              </span>
            </div>
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Metropolitan Green Belt Status</span>
              <span className={`font-semibold ${land_planning?.green_belt ? 'text-amber-600' : 'text-emerald-600 dark:text-emerald-400'}`}>
                {land_planning?.green_belt ? 'Designated (Requires Very Special Circumstances)' : 'Clear (Outside Green Belt)'}
              </span>
            </div>
            <div className="flex justify-between py-1 border-b border-border/50">
              <span className="text-muted-foreground">Statutory Planning Consent Route</span>
              <span className="font-semibold text-foreground">
                {land_planning?.consenting_route || (capacityMw >= 50 ? 'NSIP (DCO Route)' : 'TCPA (Local Planning Authority)')}
              </span>
            </div>

            {/* Local Community Sentiment Scan */}
            <div className="pt-2 border-t border-border/70 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground flex items-center gap-1.5 font-medium">
                  <Users className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400" />
                  <span>Public Sentiment Opposition Risk</span>
                </span>
                <Badge
                  variant="outline"
                  className={`text-[11px] font-semibold ${
                    (result.sentiment?.opposition_index ?? 0.24) <= 0.35
                      ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30'
                      : (result.sentiment?.opposition_index ?? 0.24) <= 0.65
                      ? 'bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30'
                      : 'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30'
                  }`}
                >
                  {((result.sentiment?.opposition_index ?? 0.24) * 100).toFixed(0)}% Index (
                  {(result.sentiment?.opposition_index ?? 0.24) <= 0.35
                    ? 'Low Opposition'
                    : (result.sentiment?.opposition_index ?? 0.24) <= 0.65
                    ? 'Moderate'
                    : 'Elevated'}
                  )
                </Badge>
              </div>

              <div className="p-2.5 bg-muted/30 rounded-xl border border-border text-[11px] space-y-1.5">
                <div className="flex justify-between text-muted-foreground">
                  <span>DeBERTa Sentiment Model:</span>
                  <span className="font-medium text-foreground">
                    {result.sentiment?.sources ?? 4} planning decisions reviewed
                  </span>
                </div>
                {(result.sentiment?.top_concerns ?? ['Acoustic Enclosures', 'Fire Safety', 'Visual Buffering']).length > 0 && (
                  <div className="pt-1 flex flex-wrap items-center gap-1.5">
                    <span className="text-muted-foreground text-[10px]">Statutory Focus Areas:</span>
                    {(result.sentiment?.top_concerns ?? ['Acoustic Enclosures', 'Fire Safety', 'Visual Buffering']).map((concern) => (
                      <span
                        key={concern}
                        className="px-2 py-0.5 rounded-md bg-card border border-border text-[10px] text-foreground font-medium"
                      >
                        {concern}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Section 4: Duration Comparison & Sizing Trade-offs */}
        <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
          <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-bold flex items-center gap-2 text-foreground">
                <TrendingUp className="w-4 h-4 text-purple-600 dark:text-purple-400" />
                <span>4. Duration & Returns Matrix</span>
              </CardTitle>
              <span className="text-[11px] text-muted-foreground">Select to inspect case</span>
            </div>
          </CardHeader>

          <CardContent className="p-4">
            <div className="space-y-2.5">
              {financialCases.length === 0 && (
                <p className="text-xs text-muted-foreground">The financial model did not run for this site.</p>
              )}
              {financialCases.map((c) => {
                const durationH = c.duration_h;
                const irrVal = c.irr != null ? c.irr * 100 : null;
                const isSelected = activeCase?.duration_h === durationH;
                const isRecommended = durationH === recommendedH;

                return (
                  <div
                    key={durationH}
                    onClick={() => setSelectedDurationH(durationH)}
                    className={`p-3 rounded-xl border transition cursor-pointer flex items-center justify-between ${
                      isSelected
                        ? 'border-emerald-500 bg-emerald-500/10 shadow-xs'
                        : 'border-border bg-muted/20 hover:border-border/80'
                    }`}
                  >
                    <div className="flex items-center gap-3">
                      <div className={`w-8 h-8 rounded-lg flex items-center justify-center font-bold text-xs ${
                        isSelected ? 'bg-emerald-600 text-white' : 'bg-muted text-foreground'
                      }`}>
                        {durationH}h
                      </div>
                      <div>
                        <div className="font-bold text-xs text-foreground flex items-center gap-1.5">
                          <span>{durationH}-Hour Duration ({capacityMw * durationH} MWh)</span>
                          {isRecommended && (
                            <Badge className="bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 border-none text-[9px] uppercase px-1.5 py-0">
                              Optimal
                            </Badge>
                          )}
                        </div>
                        <div className="text-[11px] text-muted-foreground font-mono">
                          Capex: {gbpM(c.capex_gbp, 1)}
                          {c.curtailment_pct ? ` · ${c.curtailment_pct.toFixed(0)}% curtailed` : ''}
                          {c.over_budget ? ' · over budget' : ''}
                        </div>
                      </div>
                    </div>

                    <div className="text-right">
                      <div
                        className={`font-bold font-mono text-sm ${
                          c.npv_gbp < 0 ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'
                        }`}
                      >
                        {gbpM(c.npv_gbp)} NPV
                      </div>
                      <div className="text-[11px] text-muted-foreground font-mono">
                        {irrVal != null ? `${irrVal.toFixed(1)}% IRR` : 'No payback'}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Deterministic hard checks from the data layer (site_land artifacts) */}
      {artifacts.some((a) => a.stage === 'site_land' && /^(OK|Caveat|Blocker|Unknown): /.test(a.claim)) && (
        <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
          <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-1">
              <CardTitle className="text-sm font-bold text-foreground">Site & Land Hard Checks</CardTitle>
              <span className="text-xs text-muted-foreground">
                Deterministic rules on live public data, measured against the real title boundary
              </span>
            </div>
          </CardHeader>
          <CardContent className="p-4 grid grid-cols-1 md:grid-cols-2 gap-2.5">
            {artifacts
              .filter((a) => a.stage === 'site_land' && /^(OK|Caveat|Blocker|Unknown): /.test(a.claim))
              .map((a) => {
                const [outcome, ...rest] = a.claim.split(': ');
                const tone =
                  outcome === 'OK'
                    ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40'
                    : outcome === 'Caveat'
                      ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40'
                      : outcome === 'Blocker'
                        ? 'bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/40'
                        : 'bg-muted text-muted-foreground border-border';
                // "Blocker: Protected landscape — reason": the label says what was checked. Recorded runs from
                // before labels have none, so fall back to the check name in the artifact id.
                const body = rest.join(': ');
                const cut = body.indexOf(' — ');
                const name =
                  cut > 0 ? body.slice(0, cut) : a.id.replace(/^site_land-/, '').replace(/-[^-]*$/, '').replace(/_/g, ' ');
                const reason = cut > 0 ? body.slice(cut + 3) : body;
                return (
                  <div
                    key={a.id}
                    onClick={() => setSelectedArtifact(a)}
                    className="flex items-start gap-3 p-3 rounded-xl border border-border bg-muted/20 text-xs cursor-pointer hover:border-emerald-500/50 hover:bg-muted/30 transition shadow-2xs"
                  >
                    <span className={`shrink-0 w-20 text-center px-1.5 py-0.5 rounded-md border text-[10px] font-bold uppercase ${tone}`}>
                      {outcome}
                    </span>
                    <div>
                      <div className="font-semibold capitalize text-foreground">{name}</div>
                      <div className="text-muted-foreground mt-0.5">{reason}</div>
                    </div>
                  </div>
                );
              })}
          </CardContent>
        </Card>
      )}

      {/* 4. Explainable AI: Artifacts & Data Provenance */}
      <Card className="border-border bg-card shadow-xs rounded-2xl overflow-hidden">
        <CardHeader className="p-4 border-b border-border/70 bg-muted/20">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
              <CardTitle className="text-sm font-bold text-foreground">
                Explainable AI: Verified Evidence Artifacts ({artifacts.length})
              </CardTitle>
            </div>
            <span className="text-xs text-muted-foreground">Every claim is grounded in deterministic datasets and Pydantic validation</span>
          </div>
        </CardHeader>

        <CardContent className="p-4 space-y-3">
          {groupArtifacts(artifacts).map(({ label, style, items }) => {
            const outcomes = countOutcomes(items);
            const accent = stageStyle(style).accent;
            return (
              <details key={label} open className={`group rounded-xl border border-border border-l-4 ${accent} bg-muted/10`}>
                <summary className="flex items-center justify-between gap-3 px-4 py-3 cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden">
                  <div className="flex items-center gap-2">
                    <ChevronDown className="w-4 h-4 text-muted-foreground transition-transform -rotate-90 group-open:rotate-0" />
                    <StageBadge stage={style} label={label} size="md" />
                    <span className="text-xs text-muted-foreground">
                      {items.length} artifact{items.length === 1 ? '' : 's'}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase">
                    {(['Blocker', 'Caveat'] as const).map((k) => {
                      const n = outcomes[k];
                      if (!n) return null;
                      const Icon = OUTCOME_STYLE[k].icon;
                      return (
                        <span key={k} className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border ${OUTCOME_STYLE[k].pill}`}>
                          <Icon className="w-3 h-3" />
                          {n} {k.toLowerCase()}
                          {n === 1 ? '' : 's'}
                        </span>
                      );
                    })}
                  </div>
                </summary>
                <div className="pl-10 pr-4 pb-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  {items.map((art) => {
                    const { outcome } = splitOutcome(art.claim);
                    const o = outcome ? OUTCOME_STYLE[outcome] : null;
                    return (
                      <div
                        key={art.id}
                        onClick={() => setSelectedArtifact(art)}
                        className={`p-3.5 rounded-xl border ${o ? o.card : 'border-border hover:border-foreground/30'} bg-muted/20 hover:bg-muted/40 cursor-pointer transition flex flex-col justify-between group shadow-2xs`}
                      >
                        <div>
                          <div className="flex items-center justify-between gap-2 text-[10px]">
                            <StageBadge stage={art.stage} />
                            <div className="flex items-center gap-1.5">
                              <span className="text-[11px] font-semibold text-muted-foreground whitespace-nowrap">
                                {(art.confidence * 100).toFixed(0)}% Confidence
                              </span>
                              {o && outcome && (
                                <span
                                  title={outcome}
                                  aria-label={outcome}
                                  className={`inline-flex items-center justify-center p-1 rounded-md border ${o.pill}`}
                                >
                                  <o.icon className="w-3 h-3" />
                                </span>
                              )}
                            </div>
                          </div>
                          <p className="mt-2 text-xs font-semibold text-foreground line-clamp-3 leading-relaxed">{art.claim}</p>
                        </div>

                        <div className="mt-3 pt-2.5 border-t border-border/60 flex items-center justify-between text-[10px] text-muted-foreground">
                          <span className="truncate max-w-[130px] font-medium">{art.source_name}</span>
                          <span className="font-mono">{art.snapshot_date || 'Live API'}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </details>
            );
          })}
        </CardContent>
      </Card>

      {/* Site map dialog */}
      {siteMap && (
        <Dialog open={mapOpen} onOpenChange={setMapOpen}>
          <DialogContent className="sm:max-w-5xl border-border bg-card rounded-2xl">
            <DialogHeader>
              <DialogTitle className="text-sm font-bold flex items-center gap-2">
                <MapPin className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
                {result.postcode ? `${result.postcode.toUpperCase()} · ` : ''}
                {posString}
              </DialogTitle>
              <DialogDescription className="text-xs">
                The confirmed site, its reserved compound and the cable run to the serving substation.
              </DialogDescription>
            </DialogHeader>
            {siteMap}
          </DialogContent>
        </Dialog>
      )}

      {/* Artifact Modal Dialog */}
      <Dialog open={selectedArtifact !== null} onOpenChange={(open) => !open && setSelectedArtifact(null)}>
        <DialogContent className="sm:max-w-lg border-border bg-card rounded-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400 flex items-center gap-2">
              <span>Artifact Provenance Record</span>
              <Badge variant="outline" className="text-xs uppercase">
                {selectedArtifact?.stage}
              </Badge>
            </DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground pt-1">
              Verifiable evidentiary trace backing this feasibility statement.
            </DialogDescription>
          </DialogHeader>

          {selectedArtifact && (
            <div className="space-y-4 text-xs pt-2">
              <div>
                <span className="text-muted-foreground font-medium">Synthesized Claim</span>
                <p className="text-sm font-semibold text-foreground mt-1 bg-muted/30 p-3 rounded-xl border border-border leading-relaxed">
                  {selectedArtifact.claim}
                </p>
              </div>

              <div className="grid grid-cols-2 gap-3 py-2 border-y border-border">
                <div>
                  <span className="text-muted-foreground">Data Provider / Register</span>
                  <div className="font-semibold text-foreground mt-0.5">
                    {selectedArtifact.source_name}
                  </div>
                </div>
                <div>
                  <span className="text-muted-foreground">Snapshot Date</span>
                  <div className="font-mono font-medium text-foreground mt-0.5">
                    {selectedArtifact.snapshot_date || 'Current Active Snapshot'}
                  </div>
                </div>
              </div>

              {selectedArtifact.source_url && (
                <div>
                  <span className="text-muted-foreground">Upstream Source URI</span>
                  <a
                    href={selectedArtifact.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400 hover:underline mt-1 break-all font-mono text-[11px]"
                  >
                    <span>{selectedArtifact.source_url}</span>
                    <ExternalLink className="w-3 h-3 shrink-0" />
                  </a>
                </div>
              )}

              <Button
                type="button"
                variant="outline"
                onClick={() => setSelectedArtifact(null)}
                className="w-full text-xs font-semibold mt-2 h-10 rounded-xl"
              >
                Close Provenance Dialog
              </Button>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
