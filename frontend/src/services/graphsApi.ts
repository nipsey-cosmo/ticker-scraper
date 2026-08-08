import type { StockGraph, UniverseGraph } from '@/types/graphs';

import { apiFetch } from './api';

export const getStockGraph = (symbol: string, days: number): Promise<StockGraph> =>
  apiFetch<StockGraph>(`/graphs/stock/${encodeURIComponent(symbol)}`, { params: { days } });

export const getUniverseGraph = (days: number): Promise<UniverseGraph> =>
  apiFetch<UniverseGraph>('/graphs/universe', { params: { days } });
