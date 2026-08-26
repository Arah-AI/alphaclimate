import type { Band } from "./types";

/** Compact currency: $4.2m, $980k, $5.33bn. Used for every headline figure. */
export function money(v: number | null | undefined, currency = "USD"): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sym = currency === "USD" ? "$" : "";
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}${sym}${(abs / 1e9).toFixed(2)}bn`;
  if (abs >= 1e6) return `${sign}${sym}${(abs / 1e6).toFixed(abs >= 1e8 ? 0 : 1)}m`;
  if (abs >= 1e3) return `${sign}${sym}${(abs / 1e3).toFixed(0)}k`;
  return `${sign}${sym}${abs.toFixed(0)}`;
}

/** Full precision, for tables and tooltips where the exact number matters. */
export function moneyExact(v: number | null | undefined, currency = "USD"): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(v);
}

export function pct(v: number | null | undefined, dp = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(dp)}%`;
}

export function ratio(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${v.toFixed(dp)}x`;
}

export function num(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(dp);
}

export const BAND_LABEL: Record<Band, string> = {
  low: "Low",
  moderate: "Moderate",
  high: "High",
  severe: "Severe",
};

/** Band colours. Semantic, deliberately separate from the brand blue. */
export const BAND_COLOR: Record<Band, string> = {
  low: "#1f8a53",
  moderate: "#b8770f",
  high: "#d1453b",
  severe: "#7c1d17",
};

export const BAND_TINT: Record<Band, string> = {
  low: "#e6f5ed",
  moderate: "#fdf3e2",
  high: "#fdeceb",
  severe: "#f7dedb",
};

/** Categorical series colours for perils. Blue-to-mint plus charcoal. */
export const PERIL_COLORS = [
  "#0b6be1",
  "#3fd9c6",
  "#16181d",
  "#aecdf5",
  "#7aa9e8",
  "#0959bd",
  "#b6f0e8",
];

export const PERIL_LABEL: Record<string, string> = {
  inundation_riverine: "Riverine flood",
  inundation_coastal: "Coastal flood",
  combined_flood: "Combined flood",
  chronic_heat: "Chronic heat",
  wind: "Cyclone wind",
  drought: "Drought",
  fire: "Wildfire",
  hail: "Hail",
  precipitation: "Extreme rainfall",
  landslide: "Landslide",
  subsidence: "Subsidence",
  water_risk: "Water stress",
};

export function perilLabel(p: string): string {
  return PERIL_LABEL[p] ?? p.replace(/_/g, " ");
}

export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 45) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} hr ago`;
  return `${Math.floor(secs / 86400)} d ago`;
}
