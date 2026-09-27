import React from 'react';
import {
  Activity,
  Building,
  Coins,
  FileCheck,
  LandPlot,
  LucideIcon,
  MapPin,
  Sparkles,
  TrendingUp,
  Users,
  Zap,
} from 'lucide-react';

/** Colour and icon for one pipeline stage. Full class names, so Tailwind keeps them. Red and amber are left for
 * Blocker and Caveat outcomes. */
export interface StageStyle {
  badge: string; // label background, text and border
  icon: LucideIcon;
  iconClass: string;
  accent: string; // a left border in the stage colour
}

export const STAGE_STYLES: Record<string, StageStyle> = {
  location: {
    badge: 'bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/30',
    icon: MapPin,
    iconClass: 'text-indigo-500',
    accent: 'border-l-indigo-200 dark:border-l-indigo-500/30',
  },
  capacity: {
    badge: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
    icon: Zap,
    iconClass: 'text-emerald-500',
    accent: 'border-l-emerald-200 dark:border-l-emerald-500/30',
  },
  title: {
    badge: 'bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30',
    icon: FileCheck,
    iconClass: 'text-sky-500',
    accent: 'border-l-sky-200 dark:border-l-sky-500/30',
  },
  grid: {
    badge: 'bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/30',
    icon: Zap,
    iconClass: 'text-blue-500',
    accent: 'border-l-blue-200 dark:border-l-blue-500/30',
  },
  site_land: {
    badge: 'bg-lime-500/10 text-lime-700 dark:text-lime-300 border-lime-500/30',
    icon: LandPlot,
    iconClass: 'text-lime-600',
    accent: 'border-l-lime-200 dark:border-l-lime-500/30',
  },
  planning: {
    badge: 'bg-violet-500/10 text-violet-700 dark:text-violet-300 border-violet-500/30',
    icon: Building,
    iconClass: 'text-violet-500',
    accent: 'border-l-violet-200 dark:border-l-violet-500/30',
  },
  sentiment: {
    badge: 'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30',
    icon: Users,
    iconClass: 'text-rose-500',
    accent: 'border-l-rose-200 dark:border-l-rose-500/30',
  },
  market: {
    badge: 'bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300 border-fuchsia-500/30',
    icon: TrendingUp,
    iconClass: 'text-fuchsia-500',
    accent: 'border-l-fuchsia-200 dark:border-l-fuchsia-500/30',
  },
  financial: {
    badge: 'bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/30',
    icon: Coins,
    iconClass: 'text-purple-500',
    accent: 'border-l-purple-200 dark:border-l-purple-500/30',
  },
  synthesis: {
    badge: 'bg-teal-500/10 text-teal-700 dark:text-teal-300 border-teal-500/30',
    icon: Sparkles,
    iconClass: 'text-teal-500',
    accent: 'border-l-teal-200 dark:border-l-teal-500/30',
  },
};

const FALLBACK: StageStyle = {
  badge: 'bg-muted text-muted-foreground border-border',
  icon: Activity,
  iconClass: 'text-muted-foreground',
  accent: 'border-l-border',
};

export function stageStyle(stage: string): StageStyle {
  return STAGE_STYLES[stage.toLowerCase()] ?? FALLBACK;
}

/** The small uppercase stage label with its icon, as in the agent telemetry. */
export function StageBadge({ stage, label, size = 'sm' }: { stage: string; label?: string; size?: 'sm' | 'md' }) {
  const s = stageStyle(stage);
  const Icon = s.icon;
  const sizing = size === 'md' ? 'gap-1.5 px-2 py-1 text-xs' : 'gap-1 px-1.5 py-0.5 text-[10px]';
  return (
    <span className={`inline-flex items-center rounded-md uppercase font-bold border ${sizing} ${s.badge}`}>
      <Icon className={`${size === 'md' ? 'w-3.5 h-3.5' : 'w-3 h-3'} ${s.iconClass}`} />
      <span>{label ?? stage.replace(/_/g, ' ')}</span>
    </span>
  );
}
