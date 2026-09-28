/**
 * Share of the BESS footprint on each INSPIRE polygon, measured in the browser while the user moves the pin.
 *
 * The footprint is an axis-aligned lon/lat box (`generateFootprintPolygon`), so each polygon ring is clipped
 * against it with Sutherland-Hodgman (exact for area, even for concave rings) and measured on a local metric
 * frame. The backend recomputes the same shares after confirmation (`bessible.titles.parcels`); these are for display.
 */

import type { TitleParcel } from './types';

const M_PER_DEG_LAT = 110_540;
const M_PER_DEG_LON_EQUATOR = 111_320;
/** Below this share a polygon is a boundary sliver, not part of the site (same as the backend). */
export const MIN_SHARE_PCT = 0.5;

type Ring = GeoJSON.Position[];
interface Box {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** Signed shoelace area of a ring, in m² on a frame centred at `lat0`. */
function ringArea(ring: Ring, lat0: number): number {
  const kx = M_PER_DEG_LON_EQUATOR * Math.cos((lat0 * Math.PI) / 180);
  let sum = 0;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % ring.length];
    sum += x1 * kx * (y2 * M_PER_DEG_LAT) - x2 * kx * (y1 * M_PER_DEG_LAT);
  }
  return sum / 2;
}

/** Sutherland-Hodgman: the part of a ring inside the box. */
function clipRing(ring: Ring, box: Box): Ring {
  const edges: Array<[(p: GeoJSON.Position) => boolean, (a: GeoJSON.Position, b: GeoJSON.Position) => GeoJSON.Position]> = [
    [(p) => p[0] >= box.west, (a, b) => [box.west, a[1] + ((b[1] - a[1]) * (box.west - a[0])) / (b[0] - a[0])]],
    [(p) => p[0] <= box.east, (a, b) => [box.east, a[1] + ((b[1] - a[1]) * (box.east - a[0])) / (b[0] - a[0])]],
    [(p) => p[1] >= box.south, (a, b) => [a[0] + ((b[0] - a[0]) * (box.south - a[1])) / (b[1] - a[1]), box.south]],
    [(p) => p[1] <= box.north, (a, b) => [a[0] + ((b[0] - a[0]) * (box.north - a[1])) / (b[1] - a[1]), box.north]],
  ];
  let out = ring;
  for (const [inside, cross] of edges) {
    const input = out;
    out = [];
    for (let i = 0; i < input.length; i++) {
      const cur = input[i];
      const prev = input[(i + input.length - 1) % input.length];
      if (inside(cur)) {
        if (!inside(prev)) out.push(cross(prev, cur));
        out.push(cur);
      } else if (inside(prev)) {
        out.push(cross(prev, cur));
      }
    }
    if (!out.length) break;
  }
  return out;
}

function polygons(geometry: TitleParcel['geometry']): Ring[][] {
  return geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
}

/** Area of a polygon's part inside the box (outer rings minus holes), in m². */
function areaInBox(geometry: TitleParcel['geometry'], box: Box, lat0: number): number {
  let total = 0;
  for (const poly of polygons(geometry)) {
    poly.forEach((ring, i) => {
      const a = Math.abs(ringArea(clipRing(ring, box), lat0));
      total += i === 0 ? a : -a;
    });
  }
  return Math.max(0, total);
}

function boxOf(footprint: GeoJSON.Feature<GeoJSON.Polygon>): Box {
  const ring = footprint.geometry.coordinates[0];
  const xs = ring.map((p) => p[0]);
  const ys = ring.map((p) => p[1]);
  return { west: Math.min(...xs), east: Math.max(...xs), south: Math.min(...ys), north: Math.max(...ys) };
}

/** Cheap pre-filter: the polygon's bounds touch the box. */
function overlaps(geometry: TitleParcel['geometry'], box: Box): boolean {
  const outer = polygons(geometry).flatMap((p) => p[0]);
  const xs = outer.map((p) => p[0]);
  const ys = outer.map((p) => p[1]);
  return Math.min(...xs) <= box.east && Math.max(...xs) >= box.west && Math.min(...ys) <= box.north && Math.max(...ys) >= box.south;
}

export interface ParcelShare {
  parcel: TitleParcel;
  pct: number; // share of the footprint on this polygon
}

/** Every parcel under at least `MIN_SHARE_PCT` of the footprint, largest share first; and the share on none. */
export function footprintShares(
  parcels: TitleParcel[],
  footprint: GeoJSON.Feature<GeoJSON.Polygon>
): { shares: ParcelShare[]; uncoveredPct: number } {
  const box = boxOf(footprint);
  const lat0 = (box.south + box.north) / 2;
  const total = Math.abs(ringArea(footprint.geometry.coordinates[0], lat0));
  if (!total) return { shares: [], uncoveredPct: 0 };
  const shares = parcels
    .filter((p) => overlaps(p.geometry, box))
    .map((parcel) => ({ parcel, pct: (100 * areaInBox(parcel.geometry, box, lat0)) / total }))
    .filter((s) => s.pct >= MIN_SHARE_PCT)
    .sort((a, b) => b.pct - a.pct);
  const covered = shares.reduce((sum, s) => sum + s.pct, 0);
  return { shares, uncoveredPct: Math.max(0, 100 - covered) };
}

/** Ray casting: the point is inside the ring. */
function inRing([x, y]: [number, number], ring: Ring): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** The parcel under a point (lon, lat), or null: a road, river, unregistered land, or no loaded polygon there. */
export function parcelAt(parcels: TitleParcel[], point: [number, number]): TitleParcel | null {
  return (
    parcels.find((p) => polygons(p.geometry).some(([outer, ...holes]) => inRing(point, outer) && !holes.some((h) => inRing(point, h)))) ??
    null
  );
}

/** Parcels from a `/inspire` FeatureCollection (features carry the `TitleParcel` fields as properties). */
export function parcelsFromCollection(fc: GeoJSON.FeatureCollection | null): TitleParcel[] {
  if (!fc) return [];
  return fc.features
    .filter((f) => f.geometry && (f.geometry.type === 'Polygon' || f.geometry.type === 'MultiPolygon'))
    .map((f) => ({ ...(f.properties as Omit<TitleParcel, 'geometry'>), geometry: f.geometry as TitleParcel['geometry'] }));
}

/** Where to label a polygon's share: the middle of the part of the footprint box its bounds cover. */
export function shareLabelPosition(parcel: TitleParcel, footprint: GeoJSON.Feature<GeoJSON.Polygon>): [number, number] {
  const box = boxOf(footprint);
  const outer = polygons(parcel.geometry).flatMap((p) => p[0]);
  const west = Math.max(box.west, Math.min(...outer.map((p) => p[0])));
  const east = Math.min(box.east, Math.max(...outer.map((p) => p[0])));
  const south = Math.max(box.south, Math.min(...outer.map((p) => p[1])));
  const north = Math.min(box.north, Math.max(...outer.map((p) => p[1])));
  return [(west + east) / 2, (south + north) / 2];
}
