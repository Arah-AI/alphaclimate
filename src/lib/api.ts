import type { Summary, AssetDetail, Assumptions } from "./types";

/** Same-origin by default: Next rewrites /api/* to the FastAPI service. */
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError("Cannot reach the risk engine.", 0);
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the default message */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export function getSummary(
  portfolio = "demo",
  scenario = "ssp585",
  assumptions?: Partial<Assumptions>,
): Promise<Summary> {
  const q = new URLSearchParams({ scenario });
  if (assumptions && Object.keys(assumptions).length) {
    q.set("assumptions", JSON.stringify(assumptions));
  }
  return req<Summary>(`/api/portfolio/${portfolio}/summary?${q}`);
}

export function getAsset(
  id: string,
  scenario = "ssp585",
  assumptions?: Partial<Assumptions>,
): Promise<AssetDetail> {
  const q = new URLSearchParams({ scenario });
  if (assumptions && Object.keys(assumptions).length) {
    q.set("assumptions", JSON.stringify(assumptions));
  }
  return req<AssetDetail>(`/api/asset/${id}?${q}`);
}

export function getHealth(): Promise<{
  status: string;
  hazard_source: string;
  curves_loaded: number;
  detail?: string;
}> {
  return req("/api/health");
}

/* ------------------------------------------------------------- hazard map */

export interface TileLayerInfo {
  id: string;
  label: string;
  units: string;
  max_zoom: number;
  coverage: string;
  source: string;
  index_label: string;
}

export interface Legend {
  id: string;
  label: string;
  units: string;
  max_zoom: number;
  coverage: string;
  source: string;
  index_label: string;
  stops: { from: number; to: number | null; color: string }[];
  citation: string;
}

export interface MapAsset {
  id: string;
  name: string;
  country: string;
  lon: number;
  lat: number;
}

export function getTileLayers(): Promise<{ layers: TileLayerInfo[] }> {
  return req("/api/tiles");
}

export function getLegend(hazard: string): Promise<Legend> {
  return req(`/api/tiles/${hazard}/legend`);
}

export function getMapAssets(): Promise<{ assets: MapAsset[] }> {
  return req("/api/map/assets");
}
