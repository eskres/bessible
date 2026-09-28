import { useEffect, useState } from 'react';
import { getSiteData } from './api';
import { SiteData } from './types';
import { DEFAULT_CENTER } from './useSiteRun';

/** Real data for wherever the pin is: coordinate -> location.collate -> LocationData. */
export function useSiteData([lon, lat]: [number, number]) {
  const [siteData, setSiteData] = useState<SiteData | null>(null);
  const [siteDataLoading, setSiteDataLoading] = useState(false);

  useEffect(() => {
    if (lon === DEFAULT_CENTER[0] && lat === DEFAULT_CENTER[1]) return; // untouched default
    let stale = false;
    const timer = setTimeout(async () => {
      setSiteDataLoading(true);
      try {
        const data = await getSiteData(lat, lon);
        if (!stale && data) setSiteData(data);
      } catch {
        // keep the last layer
      } finally {
        if (!stale) setSiteDataLoading(false);
      }
    }, 400);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [lon, lat]);

  return { siteData, siteDataLoading };
}
