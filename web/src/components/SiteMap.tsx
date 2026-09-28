'use client';

import React, { useEffect, useRef, useState } from 'react';
import { importLibrary, setOptions } from '@googlemaps/js-api-loader';
import { CableRoute, PositionCoords, SiteData, SubstationOption, TitleParcel } from '../lib/types';
import type { RuntimeConfig } from '../lib/auth';
import {
  generateFootprintPolygon,
  distanceKm,
  clampPositionWithinDistance,
  footprintHalfDiagonalKm,
} from '../lib/footprint';
import { Zap, Layers, MapPinOff, Grid2x2Plus } from 'lucide-react';

/** Material Symbols "layers" icon, to match Google's own map controls. */
function GoogleLayersIcon({ className }: { className?: string }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M480-118 120-398l66-50 294 228 294-228 66 50-360 280Zm0-202L120-600l360-280 360 280-360 280Zm0-280Zm0 178 230-178-230-178-230 178 230 178Z" />
    </svg>
  );
}

interface SiteMapProps {
  initialCenter?: [number, number]; // [lng, lat]
  currentPosition: [number, number]; // [lng, lat]
  onPositionChange: (pos: [number, number]) => void;
  /** The map pulled the pin back inside the screening radius (no capacity re-check needed). */
  onPositionClamped?: (pos: [number, number]) => void;
  capacityMw: number;
  substations?: SubstationOption[];
  /** The predicted point of connection; `substations` holds only the alternates. */
  servingSubstation?: SubstationOption | null;
  /** Real position of the serving substation, when the capacity check returns it. */
  servingPosition?: PositionCoords | null;
  /** Cable route from the capacity check: by road, or a straight line. */
  cableRoute?: CableRoute | null;
  inspireGeoJson?: GeoJSON.GeoJSON | null;
  siteData?: SiteData | null;
  siteDataLoading?: boolean;
  maxDistanceKm?: number;
  /** No run holds the site: a click places the pin anywhere and drags are not held to the screening radius. */
  freePlacement?: boolean;
  /** The pin cannot move (the report's map). */
  readOnly?: boolean;
  /** Height classes for the map box; defaults to the workspace size. */
  heightClassName?: string;
  /** INSPIRE polygons the user can click (the title stage's candidates, plus any loaded from /inspire). */
  titleParcels?: TitleParcel[];
  /** The polygon under the original pin. */
  pinParcelId?: string | null;
  /** The polygons the site covers now. */
  siteParcelIds?: string[];
  /** Share labels drawn on the site polygons: [lng, lat] and text. */
  parcelLabels?: { id: string; position: [number, number]; text: string }[];
  onParcelClick?: (parcel: TitleParcel) => void;
  /** Loads the INSPIRE polygons in the current view (for polygons outside the candidates). */
  onLoadParcelsInView?: (bbox: [number, number, number, number]) => void;
  loadingParcels?: boolean;
}

const PARCEL_SITE_FILL = '#f97316';
const PARCEL_SITE_STROKE = '#c2410c';
const PARCEL_PIN_STROKE = '#7c2d12';
const PARCEL_CANDIDATE_STROKE = '#e11d48';

type MapsError = 'missing' | 'rejected' | 'failed';

// Google's 256 px tiles put its zoom one level below MapLibre's for the same scale (MapLibre used 14.5)
const MAP_ZOOM = 15.5;
const FOOTPRINT_STROKE = '#059669';
const FOOTPRINT_WEIGHT = 2; // outline px; the hatch stripes are a third lighter
const HATCH_WEIGHT = FOOTPRINT_WEIGHT * (2 / 3);
const TITLE_OPACITY = 2 / 3; // title boundary: same weight as the compound, a third more see-through

const MISSING_KEY = new Error('GOOGLE_MAPS_API_KEY is not set');

// setOptions may run only once per page; the loader then caches each library.
let mapsOptionsSet = false;
async function loadGoogleMaps(apiKey: string | undefined) {
  if (!apiKey) throw MISSING_KEY;
  if (!mapsOptionsSet) {
    setOptions({ key: apiKey, v: 'weekly' });
    mapsOptionsSet = true;
  }
  await Promise.all([importLibrary('maps'), importLibrary('marker')]);
}

const toLatLng = ([lng, lat]: [number, number]): google.maps.LatLngLiteral => ({ lat, lng });

/** Google anchors marker content by its bottom centre; MapLibre's default was the centre. */
function centered(el: HTMLElement): HTMLElement {
  el.style.transform = 'translateY(50%)';
  return el;
}

/** GeoJSON Polygon / MultiPolygon rings as Google paths (other geometry types draw nothing). */
function polygonPaths(geometry: GeoJSON.Geometry): google.maps.LatLngLiteral[][] {
  const ring = (r: GeoJSON.Position[]) => r.map(([lng, lat]) => ({ lat, lng }));
  if (geometry.type === 'Polygon') return geometry.coordinates.map(ring);
  if (geometry.type === 'MultiPolygon') return geometry.coordinates.flat().map(ring);
  return [];
}

/** Diagonal stripes in the outline's colour, translucent between them; fixed on-screen size at any zoom. */
const HATCH_CSS =
  `repeating-linear-gradient(45deg, ${FOOTPRINT_STROKE} 0 ${HATCH_WEIGHT}px, ` +
  `rgba(16, 185, 129, 0.12) ${HATCH_WEIGHT}px 7px)`;

type Bounds = google.maps.LatLngBoundsLiteral;
interface HatchOverlay extends google.maps.OverlayView {
  setBounds(bounds: Bounds): void;
}

// Built on first use: google.maps.OverlayView only exists once the API has loaded
let HatchOverlayClass: (new (bounds: Bounds) => HatchOverlay) | null = null;

/** The hatch as a div the map keeps over `bounds`; unlike a GroundOverlay it can move, e.g. while the pin is dragged. */
function createHatchOverlay(bounds: Bounds): HatchOverlay {
  HatchOverlayClass ??= class extends google.maps.OverlayView implements HatchOverlay {
    private div = document.createElement('div');

    constructor(private bounds: Bounds) {
      super();
      Object.assign(this.div.style, { position: 'absolute', pointerEvents: 'none', background: HATCH_CSS });
    }

    onAdd() {
      this.getPanes()?.overlayLayer.appendChild(this.div);
    }

    onRemove() {
      this.div.remove();
    }

    draw() {
      const projection = this.getProjection();
      const sw = projection?.fromLatLngToDivPixel({ lat: this.bounds.south, lng: this.bounds.west });
      const ne = projection?.fromLatLngToDivPixel({ lat: this.bounds.north, lng: this.bounds.east });
      if (!sw || !ne) return;
      Object.assign(this.div.style, {
        left: `${sw.x}px`,
        top: `${ne.y}px`,
        width: `${ne.x - sw.x}px`,
        height: `${sw.y - ne.y}px`,
      });
    }

    setBounds(bounds: Bounds) {
      this.bounds = bounds;
      this.draw();
    }
  };
  return new HatchOverlayClass(bounds);
}

// Yellow, not blue (blue reads as water), on a dark casing so it still shows along Google's yellow main roads
const CABLE_COLOR = '#FBEC5D';
const CABLE_CASING = '#1f2937';
const CABLE_OPACITY = 1;
const POWER_LINE = '#D32F2F';
const POWER_LINE_ON_SITE = '#7F1D1D'; // darker and thicker: a line crossing the site

/** Cable line style: solid along a road route, dashed for a straight line (an estimate, not a route). */
function cableStyle(dashed: boolean): google.maps.PolylineOptions {
  return dashed
    ? {
        strokeOpacity: 0,
        icons: [
          {
            icon: { path: 'M 0,-1 0,1', strokeColor: CABLE_COLOR, strokeOpacity: CABLE_OPACITY, scale: 2.5 },
            offset: '0',
            repeat: '10px',
          },
        ],
      }
    : { strokeOpacity: CABLE_OPACITY, icons: [] };
}

/** Where an estimated substation is drawn: its distance from the site, fanned out by rank (no real coordinates). */
function estimatedSubstationCoords(center: [number, number], distanceKm: number, idx: number, count: number): [number, number] {
  const angle = (idx * 2 * Math.PI) / Math.max(count, 1) + 0.35;
  const dist = (distanceKm || 0.8) * 1000;
  const dLat = (dist * Math.cos(angle)) / 111139;
  const dLng = (dist * Math.sin(angle)) / (111139 * Math.cos((center[1] * Math.PI) / 180));
  return [center[0] + dLng, center[1] + dLat];
}

/** Estimated markers to draw: the serving substation first (unless the alternates already list it), then the rest. */
function rankedSubstations(serving: SubstationOption | null | undefined, alternates: SubstationOption[]) {
  const listed = serving && alternates.some((s) => s.name.toLowerCase() === serving.name.toLowerCase());
  return serving && !listed ? [serving, ...alternates] : alternates;
}

/** Bounds of the Reserved Compound square centred on `center`. */
function footprintBounds(center: [number, number], capacityMw: number): Bounds {
  const ring = generateFootprintPolygon(center, capacityMw, 4).geometry.coordinates[0];
  const [west, south] = ring[0];
  const [east, north] = ring[2];
  return { north, south, east, west };
}

export default function SiteMap({
  initialCenter = [-0.1132, 51.5014],
  currentPosition,
  onPositionChange,
  onPositionClamped,
  capacityMw,
  substations = [],
  servingSubstation,
  servingPosition,
  cableRoute,
  inspireGeoJson,
  siteData,
  siteDataLoading = false,
  maxDistanceKm = 2.0,
  freePlacement = false,
  readOnly = false,
  heightClassName = 'h-[48vh] min-h-[370px] sm:h-[72vh] sm:min-h-[560px]',
  titleParcels,
  pinParcelId,
  siteParcelIds,
  parcelLabels,
  onParcelClick,
  onLoadParcelsInView,
  loadingParcels = false,
}: SiteMapProps) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const infoWindowRef = useRef<google.maps.InfoWindow | null>(null);
  const pinMarkerRef = useRef<google.maps.marker.AdvancedMarkerElement | null>(null);
  const substationMarkersRef = useRef<google.maps.marker.AdvancedMarkerElement[]>([]);
  const siteDataMarkersRef = useRef<google.maps.marker.AdvancedMarkerElement[]>([]);
  const footprintRef = useRef<google.maps.Rectangle | null>(null);
  const hatchRef = useRef<HatchOverlay | null>(null);
  const radiusMaskRef = useRef<google.maps.Polygon | null>(null);
  const radiusLineRef = useRef<google.maps.Polyline | null>(null);
  const cableRayRef = useRef<google.maps.Polyline | null>(null);
  const cableCasingRef = useRef<google.maps.Polyline | null>(null);
  const rayTargetRef = useRef<{ center: [number, number]; coords: [number, number] } | null>(null);
  const titleRefs = useRef<google.maps.Polygon[]>([]);
  const gridLinesRef = useRef<google.maps.Data | null>(null);
  const inspireRef = useRef<google.maps.Data | null>(null);
  const parcelLayerRef = useRef<google.maps.Data | null>(null);
  const parcelLabelRefs = useRef<google.maps.marker.AdvancedMarkerElement[]>([]);
  const parcelClickRef = useRef<{ byId: Map<string, TitleParcel>; onClick?: (p: TitleParcel) => void }>({ byId: new Map() });
  const [mapLoaded, setMapLoaded] = useState(false);
  const [mapsError, setMapsError] = useState<MapsError | null>(null);
  const [mapMode, setMapMode] = useState<'streets' | 'satellite'>('streets');
  const distanceFromOrigin = distanceKm(initialCenter, currentPosition);

  // Latest props for the marker's dragend handler and the clamp, which must not re-bind on every render
  const latestRef = useRef({ initialCenter, maxDistanceKm, capacityMw, onPositionChange, onPositionClamped, freePlacement, readOnly });
  useEffect(() => {
    latestRef.current = { initialCenter, maxDistanceKm, capacityMw, onPositionChange, onPositionClamped, freePlacement, readOnly };
  });

  // Initialize Map
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;
    const container = mapContainer.current;
    const config = (window as unknown as { __CONFIG__?: RuntimeConfig }).__CONFIG__?.googleMaps;
    // Google calls this when it rejects the key (wrong referrer, API not enabled)
    (window as unknown as { gm_authFailure?: () => void }).gm_authFailure = () => setMapsError('rejected');

    let cancelled = false;
    loadGoogleMaps(config?.apiKey)
      .then(() => {
        if (cancelled) return;
        const map = new google.maps.Map(container, {
          center: toLatLng(currentPosition),
          zoom: MAP_ZOOM,
          mapId: config?.mapId || 'DEMO_MAP_ID',
          mapTypeId: 'roadmap',
          tilt: 0,
          disableDefaultUI: true,
          zoomControl: true,
          zoomControlOptions: { position: google.maps.ControlPosition.RIGHT_TOP },
          gestureHandling: 'greedy',
          clickableIcons: false,
        });
        const infoWindow = new google.maps.InfoWindow();
        map.addListener('click', (e: google.maps.MapMouseEvent) => {
          infoWindow.close();
          // With no run holding the site, a click drops the pin there
          if (!latestRef.current.freePlacement || !e.latLng) return;
          const pos: [number, number] = [e.latLng.lng(), e.latLng.lat()];
          if (pinMarkerRef.current) pinMarkerRef.current.position = toLatLng(pos);
          latestRef.current.onPositionChange(pos);
        });
        infoWindowRef.current = infoWindow;
        mapRef.current = map;
        setMapLoaded(true);
      })
      .catch((err) => {
        if (!cancelled) setMapsError(err === MISSING_KEY ? 'missing' : 'failed');
      });

    return () => {
      cancelled = true;
      if (mapRef.current) google.maps.event.clearInstanceListeners(mapRef.current);
      mapRef.current = null;
      pinMarkerRef.current = null;
    };
  }, []);

  // Handle basemap switch (Streets vs Satellite); overlays are separate objects and survive it
  const handleToggleMapMode = () => {
    if (!mapRef.current) return;
    const nextMode = mapMode === 'streets' ? 'satellite' : 'streets';
    setMapMode(nextMode);
    mapRef.current.setMapTypeId(nextMode === 'streets' ? 'roadmap' : 'satellite');
  };

  // Update center when initialCenter changes
  useEffect(() => {
    if (mapRef.current && mapLoaded) {
      mapRef.current.panTo(toLatLng(currentPosition));
      mapRef.current.setZoom(MAP_ZOOM);
    }
  }, [initialCenter]);

  // Setup draggable site marker
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const map = mapRef.current;

    if (!pinMarkerRef.current) {
      const el = document.createElement('div');
      const fixed = latestRef.current.readOnly;
      el.className = `site-marker flex items-center justify-center group ${fixed ? '' : 'cursor-grab active:cursor-grabbing'}`;
      el.innerHTML = `
        <div class="relative flex items-center justify-center">
          <div class="absolute -inset-2 bg-emerald-500/20 rounded-full animate-ping pointer-events-none"></div>
          <div class="relative bg-emerald-600 text-white p-1.5 rounded-full shadow-lg border-2 border-white ring-2 ring-emerald-500/40 transition-transform transform group-hover:scale-110">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
          </div>
          <div class="absolute -bottom-6 bg-zinc-950 text-white font-mono text-[9px] font-semibold px-1.5 py-0.5 rounded shadow-lg whitespace-nowrap border border-zinc-800 pointer-events-none">
            BESS Point
          </div>
          <div data-drag-hint class="absolute bottom-full mb-3 bg-zinc-950 text-white text-[10px] font-medium px-2 py-1 rounded shadow-lg whitespace-nowrap pointer-events-none opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-hover:delay-500 group-active:opacity-0 group-active:delay-0"></div>
        </div>
      `;

      const marker = new google.maps.marker.AdvancedMarkerElement({
        map,
        position: toLatLng(currentPosition),
        content: centered(el),
        gmpDraggable: !fixed,
        zIndex: 1000,
      });

      // Move the compound with the pin while dragging; the clamp and capacity re-check wait for the drop
      marker.addEventListener('gmp-drag', () => {
        const p = marker.position;
        if (!p) return;
        const pos: [number, number] = p instanceof google.maps.LatLng ? [p.lng(), p.lat()] : [p.lng, p.lat];
        const bounds = footprintBounds(pos, latestRef.current.capacityMw);
        footprintRef.current?.setBounds(bounds);
        hatchRef.current?.setBounds(bounds);
        // Until the drop and re-check, the cable is a dashed straight line from the pin
        const target = rayTargetRef.current?.coords;
        if (target) {
          const straight = [toLatLng(pos), toLatLng(target)];
          cableRayRef.current?.setOptions({ path: straight, ...cableStyle(true) });
          cableCasingRef.current?.setPath(straight);
        }
      });

      marker.addEventListener('gmp-dragend', () => {
        const p = marker.position;
        if (!p) return;
        const rawPos: [number, number] = p instanceof google.maps.LatLng ? [p.lng(), p.lat()] : [p.lng, p.lat];
        const latest = latestRef.current;
        const limit = Math.max(0, latest.maxDistanceKm - footprintHalfDiagonalKm(latest.capacityMw));
        const clamped = latest.freePlacement ? rawPos : clampPositionWithinDistance(latest.initialCenter, rawPos, limit);
        marker.position = toLatLng(clamped);
        // The user has found the drag, so the hover hint has done its job
        el.querySelector('[data-drag-hint]')?.remove();
        latest.onPositionChange(clamped);
      });

      pinMarkerRef.current = marker;
    } else {
      pinMarkerRef.current.position = toLatLng(currentPosition);
    }
  }, [mapLoaded, currentPosition]);

  // The hover hint says what a drag can do now
  useEffect(() => {
    const hint = (pinMarkerRef.current?.content as HTMLElement | null)?.querySelector('[data-drag-hint]');
    if (hint && readOnly) hint.remove();
    else if (hint) {
      hint.textContent = freePlacement
        ? 'Drag, or click the map, to place the site'
        : `Drag to move within ${maxDistanceKm} km`;
    }
  }, [mapLoaded, freePlacement, maxDistanceKm, readOnly]);

  // Keep the whole Reserved Compound inside the screening radius after a capacity or centre change, not just on drag
  useEffect(() => {
    if (freePlacement) return;
    const limit = Math.max(0, maxDistanceKm - footprintHalfDiagonalKm(capacityMw));
    if (distanceKm(initialCenter, currentPosition) <= limit + 0.001) return;
    latestRef.current.onPositionClamped?.(clampPositionWithinDistance(initialCenter, currentPosition, limit));
  }, [currentPosition, capacityMw, initialCenter, maxDistanceKm, freePlacement]);

  // Reserved Compound: outlined square plus a diagonal-stripe hatch over the same bounds
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const map = mapRef.current;
    const bounds = footprintBounds(currentPosition, capacityMw);

    if (footprintRef.current) {
      footprintRef.current.setBounds(bounds);
    } else {
      footprintRef.current = new google.maps.Rectangle({
        map,
        bounds,
        strokeColor: FOOTPRINT_STROKE,
        strokeWeight: FOOTPRINT_WEIGHT,
        fillOpacity: 0,
        clickable: false,
        zIndex: 10,
      });
    }

    if (hatchRef.current) {
      hatchRef.current.setBounds(bounds);
    } else {
      hatchRef.current = createHatchOverlay(bounds);
      hatchRef.current.setMap(map);
    }
  }, [mapLoaded, currentPosition, capacityMw]);

  // Screening radius: boundary line plus a dulled exterior (a polygon with the radius circle as its hole)
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const map = mapRef.current;

    const points = 64;
    const circle: google.maps.LatLngLiteral[] = [];
    const [centerLng, centerLat] = initialCenter;
    const radiusMeters = maxDistanceKm * 1000;

    for (let i = 0; i <= points; i++) {
      const angle = (i * 2 * Math.PI) / points;
      const dLat = (radiusMeters * Math.cos(angle)) / 111139;
      const dLng = (radiusMeters * Math.sin(angle)) / (111139 * Math.cos((centerLat * Math.PI) / 180));
      circle.push({ lat: centerLat + dLat, lng: centerLng + dLng });
    }

    // Outer ring clockwise, far past any working zoom; the circle runs the other way, so it is a hole
    const span = 2;
    const outer = [
      { lat: centerLat + span, lng: centerLng - span },
      { lat: centerLat + span, lng: centerLng + span },
      { lat: centerLat - span, lng: centerLng + span },
      { lat: centerLat - span, lng: centerLng - span },
    ];
    const paths = [outer, [...circle].reverse()];

    // No stroke on the mask itself, or its outer box would draw as a square; the boundary is its own line
    if (radiusMaskRef.current) {
      radiusMaskRef.current.setPaths(paths);
      radiusLineRef.current?.setPath(circle);
    } else {
      radiusMaskRef.current = new google.maps.Polygon({
        map,
        paths,
        fillColor: '#0f172a',
        fillOpacity: 0.32,
        strokeOpacity: 0,
        clickable: false,
        zIndex: 1,
      });
      radiusLineRef.current = new google.maps.Polyline({
        map,
        path: circle,
        strokeColor: '#0284c7',
        strokeOpacity: 0.7,
        strokeWeight: 1.5,
        clickable: false,
        zIndex: 2,
      });
    }
  }, [mapLoaded, initialCenter, maxDistanceKm]);

  // The screening radius only means something around a run's site
  useEffect(() => {
    radiusMaskRef.current?.setVisible(!freePlacement);
    radiusLineRef.current?.setVisible(!freePlacement);
  }, [mapLoaded, initialCenter, freePlacement]);

  // Render estimated substation markers (live site data draws the real ones below)
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const map = mapRef.current;

    substationMarkersRef.current.forEach((m) => (m.map = null));
    substationMarkersRef.current = [];

    if (siteData) return;

    const ranked = rankedSubstations(servingSubstation, substations);
    ranked.forEach((sub, idx) => {
      // The serving substation's real position when the capacity check returns it; the rest are estimated
      const isServing = servingPosition && sub.name.toLowerCase() === servingSubstation?.name.toLowerCase();
      const subCoords: [number, number] = isServing
        ? [servingPosition.lon, servingPosition.lat]
        : sub.position
          ? [sub.position.lon, sub.position.lat]
          : estimatedSubstationCoords(initialCenter, sub.distance_km, idx, ranked.length);

      const el = document.createElement('div');
      el.className = 'substation-marker group cursor-pointer';
      el.innerHTML = `
        <div class="relative flex flex-col items-center">
          <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg shadow-md text-xs font-semibold ${
            sub.is_marginal
              ? 'bg-amber-50 text-amber-900 border border-amber-300 dark:bg-amber-950 dark:text-amber-100'
              : 'bg-blue-600 text-white border border-blue-500 shadow-blue-500/20'
          }">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            <span class="tracking-tight">${sub.name}</span>
            <span class="ml-0.5 opacity-90 font-mono text-[11px]">${sub.effective_headroom_mw}MW</span>
            ${sub.is_marginal ? '<span class="bg-amber-200 text-amber-950 text-[9px] px-1 rounded font-bold uppercase">Marginal</span>' : ''}
          </div>
          <div class="w-2 h-2 rotate-45 -mt-1 ${sub.is_marginal ? 'bg-amber-300' : 'bg-blue-600'}"></div>
        </div>
      `;

      const popupHtml = `
        <div class="p-1 text-xs font-sans space-y-1 text-zinc-800">
          <div class="font-bold text-sm">${sub.name}</div>
          <div class="text-zinc-500 flex justify-between gap-3">
            <span>Distance (straight line):</span>
            <span class="font-mono font-medium text-zinc-900">${sub.distance_km.toFixed(2)} km</span>
          </div>
          <div class="text-zinc-500 flex justify-between gap-3">
            <span>Primary Voltage:</span>
            <span class="font-mono font-medium text-zinc-900">${sub.voltage_kv} kV</span>
          </div>
          <div class="text-emerald-600 font-semibold pt-1 border-t border-zinc-200 flex justify-between">
            <span>Available Headroom:</span>
            <span class="font-mono">${sub.effective_headroom_mw} MW</span>
          </div>
          ${sub.is_marginal ? '<div class="text-amber-600 font-medium text-[11px] pt-0.5">Note: Distance > 1km introduces higher contestable cabling capex.</div>' : ''}
        </div>
      `;

      const marker = new google.maps.marker.AdvancedMarkerElement({
        map,
        position: toLatLng(subCoords),
        content: el,
        gmpClickable: true,
      });
      marker.addEventListener('gmp-click', () => {
        infoWindowRef.current?.setContent(popupHtml);
        infoWindowRef.current?.open({ anchor: marker, map });
      });

      substationMarkersRef.current.push(marker);
    });

  }, [mapLoaded, servingSubstation, servingPosition, substations, initialCenter, siteData]);

  // Cable from the pin to the serving substation. Target: the position the capacity check returns, else the same-named
  // substation in live site data, else its estimated marker. Always a straight line: solid once the check has priced
  // the cable run, dashed until then. While a capacity re-check is loading there is no serving substation, so keep a
  // dashed line to the last target for this site rather than blink out.
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const name = servingSubstation?.name.toLowerCase();
    let target: [number, number] | null = servingPosition ? [servingPosition.lon, servingPosition.lat] : null;
    if (!target && name) {
      const real = siteData?.deterministic?.grid?.substations?.find((s) => s.name.toLowerCase() === name);
      const ranked = rankedSubstations(servingSubstation, substations);
      const idx = ranked.findIndex((s) => s.name.toLowerCase() === name);
      if (real) target = [real.coords.lon, real.coords.lat];
      else if (!siteData) target = estimatedSubstationCoords(initialCenter, ranked[idx].distance_km, idx, ranked.length);
    }

    const last = rayTargetRef.current;
    if (target) rayTargetRef.current = { center: initialCenter, coords: target };
    else if (!name && last && last.center[0] === initialCenter[0] && last.center[1] === initialCenter[1])
      target = last.coords;

    if (!target) {
      cableRayRef.current?.setMap(null);
      cableCasingRef.current?.setMap(null);
      cableRayRef.current = null;
      cableCasingRef.current = null;
      return;
    }
    const path = [toLatLng(currentPosition), toLatLng(target)];
    const options = { path, ...cableStyle(!(cableRoute && name)) };
    if (cableRayRef.current) {
      cableRayRef.current.setOptions(options);
      cableCasingRef.current?.setPath(path);
    } else {
      cableCasingRef.current = new google.maps.Polyline({
        map: mapRef.current,
        path,
        strokeColor: CABLE_CASING,
        strokeOpacity: 0.6,
        strokeWeight: 5,
        clickable: false,
        zIndex: 5,
      });
      cableRayRef.current = new google.maps.Polyline({
        map: mapRef.current,
        strokeColor: CABLE_COLOR,
        strokeWeight: 3,
        clickable: false,
        zIndex: 6,
        ...options,
      });
    }
  }, [mapLoaded, servingSubstation, servingPosition, cableRoute, substations, initialCenter, currentPosition, siteData]);

  // Real data layer from LocationData: title boundary, overhead lines, substations, generation / storage projects
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    const map = mapRef.current;

    siteDataMarkersRef.current.forEach((m) => (m.map = null));
    siteDataMarkersRef.current = [];
    titleRefs.current.forEach((p) => p.setMap(null));
    titleRefs.current = [];

    const grid = siteData?.deterministic?.grid;
    const esc = (v: unknown) => String(v ?? '').replace(/[<>&]/g, '');
    const fmt = (v: unknown, unit = '') => (typeof v === 'number' ? `${Math.round(v * 10) / 10}${unit}` : 'n/a');

    // Title: a white halo under the orange outline, both above every other shape
    // The title stage's polygons, when shown, replace the one title under the pin
    const titlePaths = siteData?.title && !titleParcels?.length ? polygonPaths(siteData.title.geometry) : [];
    if (titlePaths.length) {
      titleRefs.current = [
        new google.maps.Polygon({
          map,
          paths: titlePaths,
          strokeColor: '#ffffff',
          strokeWeight: FOOTPRINT_WEIGHT * 2,
          strokeOpacity: TITLE_OPACITY,
          fillOpacity: 0,
          clickable: false,
          zIndex: 20,
        }),
        new google.maps.Polygon({
          map,
          paths: titlePaths,
          strokeColor: '#c2410c',
          strokeWeight: FOOTPRINT_WEIGHT,
          strokeOpacity: TITLE_OPACITY,
          fillColor: '#f97316',
          fillOpacity: 0.3 * TITLE_OPACITY,
          clickable: false,
          zIndex: 21,
        }),
      ];
    }

    if (!gridLinesRef.current) {
      const data = new google.maps.Data({ map });
      data.setStyle((f) => ({
        strokeColor: f.getProperty('crosses') ? POWER_LINE_ON_SITE : POWER_LINE,
        strokeWeight: f.getProperty('crosses') ? 3 : 1.5,
        clickable: false,
        zIndex: 4,
      }));
      gridLinesRef.current = data;
    }
    const lines = gridLinesRef.current;
    lines.forEach((f) => lines.remove(f));
    lines.addGeoJson({
      type: 'FeatureCollection',
      features: (grid?.lines ?? []).map((l) => ({
        type: 'Feature',
        properties: { crosses: !!l.crosses_site },
        geometry: l.geometry,
      })),
    });
    if (!grid) return;

    const add = (lngLat: [number, number], html: string, popupHtml: string) => {
      const el = document.createElement('div');
      el.innerHTML = html;
      const marker = new google.maps.marker.AdvancedMarkerElement({
        map,
        position: toLatLng(lngLat),
        content: centered(el),
        gmpClickable: true,
      });
      marker.addEventListener('gmp-click', () => {
        infoWindowRef.current?.setContent(`<div class="p-1 text-xs text-zinc-800">${popupHtml}</div>`);
        infoWindowRef.current?.open({ anchor: marker, map });
      });
      siteDataMarkersRef.current.push(marker);
    };

    grid.projects.slice(0, 40).forEach((p) => {
      const icon = p.is_storage ? '🔋' : p.is_solar ? '☀️' : '⚙️';
      add(
        [p.coords.lon, p.coords.lat],
        `<div class="text-sm leading-none bg-white/90 rounded-full border border-zinc-300 shadow p-1 cursor-pointer" title="${esc(p.name)}">${icon}</div>`,
        `<div class="font-bold">${esc(p.name) || 'Unnamed project'}</div>
         <div>${esc(p.technology)} · ${fmt(p.capacity_mw, ' MW')}${p.storage_mwh ? ` / ${fmt(p.storage_mwh, ' MWh')}` : ''}</div>
         <div>${esc(p.status)} · ${esc(p.operator)} · ${fmt(p.distance_km, ' km')}</div>`
      );
    });

    grid.substations.slice(0, 12).forEach((sub) => {
      const h = sub.headroom;
      const twoWay = h ? Math.max(0, Math.min(h.generation_mw ?? 0, h.demand ?? 0)) : null;
      const tone =
        twoWay === null
          ? 'bg-zinc-600 text-white'
          : twoWay >= 5
            ? 'bg-emerald-600 text-white'
            : twoWay > 0
              ? 'bg-amber-500 text-white'
              : 'bg-red-600 text-white';
      add(
        [sub.coords.lon, sub.coords.lat],
        `<div class="flex items-center gap-1 px-2 py-1 rounded-md shadow-md text-[11px] font-semibold cursor-pointer ${tone}">
           <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
           <span>${esc(sub.name)}</span>
           <span class="opacity-90">${twoWay === null ? '' : `(${fmt(twoWay, ' MW')})`}</span>
         </div>`,
        `<div class="font-bold text-sm">${esc(sub.name)}</div>
         <div>${esc(sub.operator)} · ${esc(sub.kind)} · ${esc(sub.voltages ?? sub.voltage_kv)} kV · ${fmt(sub.distance_km, ' km')}</div>
         ${
           h
             ? `<div class="font-semibold mt-1">Import ${fmt(h.demand)} ${esc(h.demand_unit)} · Export ${fmt(h.generation_mw, ' MW')}</div>
                <div>${esc(h.generation_constraint ?? h.demand_constraint ?? '')}</div>`
             : '<div class="mt-1">No published headroom</div>'
         }
         ${sub.gsp ? `<div>GSP: ${esc(sub.gsp)}${sub.bsp ? ` · BSP: ${esc(sub.bsp)}` : ''}</div>` : ''}`
      );
    });
  }, [mapLoaded, siteData, titleParcels?.length]);

  // Title polygons: candidates faint, the site filled, the pin's own polygon outlined dark; a click toggles one
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    if (!parcelLayerRef.current) {
      const layer = new google.maps.Data({ map: mapRef.current });
      layer.addListener('click', (e: google.maps.Data.MouseEvent) => {
        const { byId, onClick } = parcelClickRef.current;
        const parcel = byId.get(String(e.feature.getProperty('inspire_id')));
        if (parcel && onClick) onClick(parcel);
      });
      parcelLayerRef.current = layer;
    }
    const layer = parcelLayerRef.current;
    layer.forEach((f) => layer.remove(f));
    parcelClickRef.current.byId = new Map((titleParcels ?? []).map((p) => [p.inspire_id, p]));
    if (!titleParcels?.length) return;
    layer.addGeoJson({
      type: 'FeatureCollection',
      features: titleParcels.map((p) => ({ type: 'Feature', id: p.inspire_id, geometry: p.geometry, properties: { inspire_id: p.inspire_id } })),
    });
  }, [mapLoaded, titleParcels]);

  useEffect(() => {
    parcelClickRef.current.onClick = readOnly ? undefined : onParcelClick;
    const layer = parcelLayerRef.current;
    if (!layer) return;
    const site = new Set(siteParcelIds ?? []);
    layer.setStyle((f) => {
      const id = String(f.getProperty('inspire_id'));
      const onSite = site.has(id);
      const isPin = id === pinParcelId;
      return {
        strokeColor: isPin ? PARCEL_PIN_STROKE : onSite ? PARCEL_SITE_STROKE : PARCEL_CANDIDATE_STROKE,
        strokeWeight: isPin ? 3 : onSite ? 2 : 1,
        strokeOpacity: onSite || isPin ? 0.95 : 0.45,
        fillColor: onSite ? PARCEL_SITE_FILL : PARCEL_CANDIDATE_STROKE,
        fillOpacity: onSite ? 0.32 : 0.04,
        clickable: !readOnly && !!onParcelClick,
        cursor: 'pointer',
        zIndex: onSite ? 8 : 3,
      };
    });
  }, [mapLoaded, titleParcels, siteParcelIds, pinParcelId, onParcelClick, readOnly]);

  // Share of the footprint on each site polygon, as a small label
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    parcelLabelRefs.current.forEach((m) => (m.map = null));
    parcelLabelRefs.current = (parcelLabels ?? []).map((l) => {
      const el = document.createElement('div');
      el.className =
        'pointer-events-none bg-white/95 text-orange-800 border border-orange-300 rounded px-1.5 py-0.5 text-[10px] font-semibold font-mono shadow';
      el.textContent = l.text;
      return new google.maps.marker.AdvancedMarkerElement({
        map: mapRef.current,
        position: toLatLng(l.position),
        content: centered(el),
        zIndex: 900,
      });
    });
  }, [mapLoaded, parcelLabels]);

  const loadParcelsInView = () => {
    const b = mapRef.current?.getBounds();
    if (!b || !onLoadParcelsInView) return;
    const sw = b.getSouthWest();
    const ne = b.getNorthEast();
    onLoadParcelsInView([sw.lng(), sw.lat(), ne.lng(), ne.lat()]);
  };

  // Render INSPIRE Land Registry Parcels if present; clear them when they go away
  useEffect(() => {
    if (!mapRef.current || !mapLoaded) return;
    if (!inspireRef.current) {
      inspireRef.current = new google.maps.Data({ map: mapRef.current });
      inspireRef.current.setStyle({
        strokeColor: '#e11d48',
        strokeWeight: 1.5,
        strokeOpacity: 0.7,
        fillColor: '#f43f5e',
        fillOpacity: 0.08,
        clickable: false,
        zIndex: 3,
      });
    }
    const parcels = inspireRef.current;
    parcels.forEach((f) => parcels.remove(f));
    if (!inspireGeoJson) return;
    try {
      parcels.addGeoJson(inspireGeoJson);
    } catch {
      // not a Feature / FeatureCollection: draw nothing
    }
  }, [mapLoaded, inspireGeoJson]);

  const zoomToTitle = () => {
    const map = mapRef.current;
    if (!map || !siteData?.title) return;
    const [minLon, minLat, maxLon, maxLat] = siteData.title.bbox;
    map.fitBounds({ west: minLon, south: minLat, east: maxLon, north: maxLat }, 120);
    // fitBounds has no maxZoom: cap it once the move settles (MapLibre's maxZoom 18 = Google 19)
    google.maps.event.addListenerOnce(map, 'idle', () => {
      if ((map.getZoom() ?? 0) > 19) map.setZoom(19);
    });
  };

  return (
    <div className="relative w-full">
      <div className={`relative w-full ${heightClassName} rounded-2xl overflow-hidden border border-border shadow-md bg-muted`}>
        <div ref={mapContainer} className="w-full h-full" />

        {mapsError && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-muted p-6">
            <div className="max-w-sm text-center space-y-2">
              <MapPinOff className="w-8 h-8 mx-auto text-muted-foreground" />
              <div className="text-sm font-semibold text-foreground">Map unavailable</div>
              <div className="text-xs text-muted-foreground">
                {mapsError === 'missing' ? (
                  <>
                    Set <code className="font-mono">GOOGLE_MAPS_API_KEY</code> in the root <code className="font-mono">.env</code>{' '}
                    and restart the web app.
                  </>
                ) : mapsError === 'rejected' ? (
                  <>
                    Google rejected the Maps key. Check that the Maps JavaScript API is enabled and this domain is allowed
                    for <code className="font-mono">GOOGLE_MAPS_API_KEY</code>.
                  </>
                ) : (
                  <>Google Maps did not load. Check the network connection and reload.</>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Top Left: Location & Pin Coordinate Telemetry */}
        <div className="absolute top-3.5 left-3.5 flex flex-col gap-2 pointer-events-none z-10">
          <div className="bg-card/90 backdrop-blur-md px-3.5 py-2 rounded-xl shadow-sm border border-border/80 text-xs font-medium flex items-center gap-2.5">
            <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></div>
            <span className="font-mono text-foreground font-semibold">
              {currentPosition[1].toFixed(5)}°N, {Math.abs(currentPosition[0]).toFixed(5)}°{currentPosition[0] >= 0 ? 'E' : 'W'}
            </span>
            {!freePlacement && (
              <span className="hidden sm:inline text-muted-foreground text-[11px] font-mono border-l border-border pl-2">
                +{distanceFromOrigin.toFixed(2)} km offset
              </span>
            )}
          </div>

          {substations.length > 0 && (
            <div className="bg-card/90 backdrop-blur-md px-3.5 py-1.5 rounded-xl shadow-sm border border-border/80 text-xs text-foreground/90 flex items-center gap-2">
              <Zap className="w-3.5 h-3.5 text-blue-500" />
              <span>{substations.length} Substation Nodes Polled</span>
            </div>
          )}

          {onLoadParcelsInView && !mapsError && (
            <button
              type="button"
              onClick={loadParcelsInView}
              disabled={loadingParcels}
              className="pointer-events-auto self-start bg-card/90 hover:bg-card backdrop-blur-md px-3 py-1.5 rounded-xl shadow-sm border border-border/80 text-xs font-semibold text-foreground flex items-center gap-1.5 cursor-pointer disabled:opacity-60"
              title="Load HM Land Registry INSPIRE polygons in the current view, to add ones outside the candidates"
            >
              <Grid2x2Plus className="w-3.5 h-3.5 text-orange-600" />
              {loadingParcels ? 'Loading polygons…' : 'Load title polygons in view'}
            </button>
          )}
        </div>

        {/* Layer Switcher: a Google-style square under the zoom buttons on mobile, a labelled button left of them on wider screens */}
        {!mapsError && (
          <div className="absolute top-[101px] right-2.5 sm:top-3.5 sm:right-14 z-10 flex items-center gap-2">
            <button
              type="button"
              onClick={handleToggleMapMode}
              aria-label={mapMode === 'streets' ? 'Satellite View' : 'Street Map'}
              title={mapMode === 'streets' ? 'Satellite View' : 'Street Map'}
              className="w-10 h-10 justify-center bg-white text-[#666] shadow-[0_1px_4px_-1px_rgba(0,0,0,0.3)] sm:w-auto sm:h-auto sm:justify-start sm:bg-card/90 sm:hover:bg-card sm:text-foreground sm:backdrop-blur-md sm:px-3 sm:py-1.5 sm:rounded-lg sm:shadow-sm sm:border sm:border-border text-xs font-semibold flex items-center gap-1.5 transition active:scale-95 cursor-pointer"
            >
              <GoogleLayersIcon className="w-6 h-6 sm:hidden" />
              <Layers className="hidden sm:block w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400" />
              <span className="hidden sm:inline">{mapMode === 'streets' ? 'Satellite View' : 'Street Map'}</span>
            </button>
          </div>
        )}
      </div>

      {/* Legend: below the map on mobile; on wider screens it floats above Google's logo and terms, which must stay visible */}
      <div className="mt-2 sm:mt-0 sm:absolute sm:bottom-7 sm:left-3.5 sm:right-3.5 flex flex-wrap items-center justify-between gap-2 pointer-events-none z-10">
        <div className="bg-card/90 backdrop-blur-md px-3 py-2 sm:py-1.5 rounded-xl shadow-sm border border-border/80 text-[11px] text-muted-foreground flex flex-wrap sm:flex-nowrap items-center gap-x-3 gap-y-1.5 sm:gap-3 w-full sm:w-auto">
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block w-2.5 h-2.5 rounded-sm border border-emerald-600"
              style={{
                background:
                  'repeating-linear-gradient(45deg, #059669 0 1px, rgba(16, 185, 129, 0.15) 1px 3px)',
              }}
            ></span>
            <span className="font-medium text-foreground">Reserved Compound ({capacityMw} MW)</span>
          </div>
          <div className="flex items-center gap-1.5 sm:border-l sm:border-border sm:pl-3">
            {cableRoute ? (
              <>
                <span
                  className="inline-block w-4 h-1 rounded-sm"
                  style={{ background: CABLE_COLOR, boxShadow: `0 0 0 1px ${CABLE_CASING}` }}
                ></span>
                <span>
                  Cable run {cableRoute.straight_km.toFixed(2)} km, priced as {cableRoute.distance_km.toFixed(2)} km
                  (×{cableRoute.detour_factor} detour)
                </span>
              </>
            ) : (
              <>
                <span
                  className="inline-block w-4 h-1 rounded-sm"
                  style={{ background: `repeating-linear-gradient(90deg, ${CABLE_COLOR} 0 3px, ${CABLE_CASING} 3px 5px)` }}
                ></span>
                <span>Cable run, straight line</span>
              </>
            )}
          </div>
          {!!titleParcels?.length && (
            <div className="flex items-center gap-1.5 sm:border-l sm:border-border sm:pl-3">
              <span className="inline-block w-2.5 h-2.5 rounded-sm border-2 border-orange-700 bg-orange-300"></span>
              <span>Site polygons ({siteParcelIds?.length ?? 0})</span>
              <span className="inline-block w-2.5 h-2.5 rounded-sm border border-rose-500/60 ml-1.5"></span>
              <span>INSPIRE polygons, click to add or remove</span>
            </div>
          )}
          {siteData && (
            <>
              <div className={`flex items-center gap-1.5 sm:border-l sm:border-border sm:pl-3 ${titleParcels?.length ? 'hidden' : ''}`}>
                <span className="inline-block w-2.5 h-2.5 rounded-sm border-2 border-orange-700 bg-orange-300"></span>
                <span>Title {siteData.title ? `${siteData.title.area_ha.toFixed(2)} ha` : 'not registered'}</span>
                {siteData.title && !mapsError && (
                  <button
                    type="button"
                    className="pointer-events-auto underline text-orange-600 dark:text-orange-400 font-semibold cursor-pointer ml-1"
                    onClick={zoomToTitle}
                  >
                    zoom to title
                  </button>
                )}
              </div>
              <div className="flex sm:hidden lg:flex flex-wrap items-center gap-2 lg:border-l lg:border-border lg:pl-3 text-[10px]">
                <span>⚡ substations</span>
                <span>🔋 storage</span>
                <span>☀️ solar</span>
                <span>
                  <span style={{ color: POWER_LINE }}>—</span> power lines{' '}
                  <span className="font-black" style={{ color: POWER_LINE_ON_SITE }}>
                    —
                  </span>{' '}
                  crossing site
                </span>
              </div>
            </>
          )}
          {inspireGeoJson && !siteData && (
            <div className="flex items-center gap-1.5 sm:border-l sm:border-border sm:pl-3">
              <span className="inline-block w-2.5 h-2.5 rounded-sm bg-rose-500 opacity-70"></span>
              <span>Cadastral Boundary</span>
            </div>
          )}
          {siteDataLoading && (
            <span className="ml-1 text-emerald-600 dark:text-emerald-400 font-semibold animate-pulse sm:border-l sm:border-border sm:pl-3">
              Loading live site data…
            </span>
          )}
        </div>

      </div>
    </div>
  );
}
