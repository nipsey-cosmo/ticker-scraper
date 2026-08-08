import { useQuery } from '@tanstack/react-query';

import { getStockGraph, getUniverseGraph } from '@/services/graphsApi';

/**
 * The two graph queries.
 *
 * Both are pure server aggregates over data that only changes when the scraper
 * runs, so they are cached for a minute rather than refetched on every
 * timeframe toggle — flicking 30d → 90d → 30d should be instant the second
 * time.
 */
const STALE_TIME = 60_000;

export const useStockGraph = (symbol: string | null, days: number) =>
  useQuery({
    queryKey: ['graphs', 'stock', symbol, days],
    queryFn: () => getStockGraph(symbol as string, days),
    enabled: Boolean(symbol),
    staleTime: STALE_TIME,
  });

export const useUniverseGraph = (days: number) =>
  useQuery({
    queryKey: ['graphs', 'universe', days],
    queryFn: () => getUniverseGraph(days),
    staleTime: STALE_TIME,
  });
