'use client';

import React from 'react';
import type { TitleNumber, TitleOutput, TitleParcel } from '../lib/types';
import type { ParcelShare } from '../lib/parcels';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Landmark, RotateCcw, X } from 'lucide-react';

const SOURCE_LABEL: Record<TitleNumber['source'], string> = {
  listing: 'listing text',
  ccod: 'HMLR CCOD',
  ocod: 'HMLR OCOD',
  user: 'entered by you',
};

interface TitleParcelsPanelProps {
  title: TitleOutput;
  /** Every polygon the user can pick from: candidates plus any loaded from /inspire. */
  pool: TitleParcel[];
  shares: ParcelShare[];
  uncoveredPct: number;
  siteIds: string[];
  /** True once the user clicked a polygon: the clicked set is the site, not the footprint's. */
  clicked: boolean;
  onToggle: (parcel: TitleParcel) => void;
  onReset: () => void;
  locked?: boolean;
}

const ha = (m2: number) => `${(m2 / 10_000).toFixed(2)} ha`;

/** The polygons the BESS footprint covers, each with its share, and title numbers from public data with their source. */
export default function TitleParcelsPanel({
  title,
  pool,
  shares,
  uncoveredPct,
  siteIds,
  clicked,
  onToggle,
  onReset,
  locked = false,
}: TitleParcelsPanelProps) {
  const byId = new Map(pool.map((p) => [p.inspire_id, p]));
  const share = new Map(shares.map((s) => [s.parcel.inspire_id, s.pct]));
  const site = siteIds.map((id) => byId.get(id)).filter((p): p is TitleParcel => !!p);
  const siteArea = site.reduce((sum, p) => sum + p.area_m2, 0);
  const pinId = title.pin_parcel?.inspire_id;
  const numbers = title.title_numbers ?? [];

  return (
    <Card className="border-border">
      <CardHeader className="pt-4 pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Landmark className="w-4 h-4 text-orange-600" />
          Title polygons under the BESS footprint
          <Badge variant="outline" className="ml-auto font-mono text-[10px]">
            {site.length} polygon{site.length === 1 ? '' : 's'} · {ha(siteArea)}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <p className="text-muted-foreground">
          {pinId ? (
            <>
              The pin is on INSPIRE polygon <span className="font-mono">{pinId}</span>.{' '}
            </>
          ) : (
            <>The pin is on no INSPIRE polygon (a road, river, unregistered land, or outside England). </>
          )}
          {clicked
            ? 'You chose the polygons below by clicking the map.'
            : 'Move the pin to move the footprint; click polygons on the map to add or remove them.'}{' '}
          INSPIRE polygons show indicative extents, not legal boundaries, and carry no title numbers.
        </p>

        <div className="border border-border rounded-lg divide-y divide-border">
          {site.length === 0 && <div className="p-2.5 text-muted-foreground">No polygon selected.</div>}
          {site.map((p) => {
            const pct = share.get(p.inspire_id);
            return (
              <div key={p.inspire_id} className="p-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <a
                  href={p.source_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono font-semibold text-foreground underline decoration-dotted"
                >
                  {p.inspire_id}
                </a>
                {p.inspire_id === pinId && <Badge className="text-[9px] bg-orange-800 text-white">pin</Badge>}
                <span className="text-muted-foreground">{ha(p.area_m2)}</span>
                <span className="font-mono font-semibold text-orange-700 dark:text-orange-400 min-w-[7.5rem]">
                  {pct !== undefined ? `${pct.toFixed(1)}% of footprint` : 'not under footprint'}
                </span>
                {p.title_number && (
                  <span className="font-mono text-foreground">
                    {p.title_number} ({p.title_source})
                  </span>
                )}
                <div className="flex items-center gap-1.5 ml-auto">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={locked}
                    onClick={() => onToggle(p)}
                    className="h-7 w-7 p-0 cursor-pointer"
                    aria-label={`Remove polygon ${p.inspire_id}`}
                    title="Remove from the site"
                  >
                    <X className="w-3.5 h-3.5" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
          {uncoveredPct >= 0.5 && !clicked && (
            <span>{uncoveredPct.toFixed(1)}% of the footprint is on no polygon.</span>
          )}
          {clicked && (
            <Button type="button" variant="outline" size="sm" onClick={onReset} disabled={locked} className="h-7 gap-1 text-xs cursor-pointer">
              <RotateCcw className="w-3 h-3" /> Use the footprint again
            </Button>
          )}
        </div>

        <div className="space-y-1">
          <div className="font-semibold text-foreground">Title numbers from public data</div>
          <div className="text-muted-foreground">
            Listing numbers are stated for the site; CCOD / OCOD numbers are company-owned titles at the site&apos;s
            postcodes, candidates only: none is tied to a polygon.
          </div>
          {numbers.length === 0 && <div className="text-muted-foreground">None found for this site.</div>}
          {numbers.map((n) => (
            <div key={`${n.source}-${n.title_number}`} className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-mono font-semibold text-foreground">{n.title_number}</span>
              <Badge variant="outline" className="text-[9px]">
                {SOURCE_LABEL[n.source]}
              </Badge>
              <span className="text-muted-foreground">
                {n.proprietor ? `${n.proprietor} · ` : ''}
                {n.tenure ? `${n.tenure} · ` : ''}
                {n.address ?? n.evidence}
                {n.link === 'site' ? ' · site-level' : ''}
              </span>
              {n.source_url && (
                <a href={n.source_url} target="_blank" rel="noreferrer" className="underline text-muted-foreground">
                  source
                </a>
              )}
            </div>
          ))}
          {(title.notes ?? []).filter((note) => !note.startsWith('The pin is on no')).map((note) => (
            <div key={note} className="text-muted-foreground italic">
              {note}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
