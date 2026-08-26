export type Band = "low" | "moderate" | "high" | "severe";

export interface Spread {
  low: number;
  median: number;
  high: number;
  n: number;
  range_ratio: number;
  by_driver: Record<string, { range: number; share: number; levels: number }>;
}

export interface PerilSlice {
  peril: string;
  label: string;
  eal: number;
  share: number;
}

export interface AssetRow {
  id: string;
  name: string;
  country: string;
  sector: string;
  value: number;
  eal: number;
  eal_pct: number;
  impairment: number;
  impairment_pct: number;
  band: Band;
  covenant_breach: boolean;
  uninsurable: boolean;
  extrapolated: boolean;
  top_peril: string;
}

export interface YearPoint {
  year: number;
  median: number;
  low: number;
  high: number;
}

export interface HazardSourceInfo {
  dataset: string;
  path: string;
  units: string;
  resolution: string;
  citation: string;
}

export interface Provenance {
  run_id: string;
  computed_at: string;
  engine_version: string;
  scenario: string;
  horizon_years: number;
  hazard_sources: HazardSourceInfo[];
  curve_sources: string[];
  degraded: boolean;
  degraded_reason?: string | null;
}

export interface Headline {
  eal: number;
  eal_spread: Spread;
  eal_pct_of_value: number;
  npv_climate_cost: number;
  value_impairment: number;
  value_impairment_pct: number;
  insured_share: number;
  retained_share: number;
  protection_gap: number;
  total_value: number;
  asset_count: number;
  bands: Record<Band, number>;
  covenant_breaches: number;
  uninsurable_count: number;
  tail_share: number;
}

export interface Summary {
  portfolio: { id: string; name: string; currency: string; note: string };
  headline: Headline;
  perils: PerilSlice[];
  assets: AssetRow[];
  yearly: YearPoint[];
  impairment_path: { year: number; value: number }[];
  scenarios: { id: string; label: string }[];
  provenance: Provenance;
}

export interface AssetDetail {
  asset: {
    id: string;
    name: string;
    country: string;
    sector: string;
    occupancy: string;
    lon: number;
    lat: number;
    value: number;
    annual_revenue: number;
    debt: number;
    annual_debt_service: number;
  };
  hazards: {
    peril: string;
    label: string;
    units: string;
    return_periods: number[];
    intensities: number[];
    damage_fractions: number[];
    losses: number[];
    eal: number;
    eal_tail: number;
    extrapolated: boolean;
    curve_id: string;
    curve_source: string;
    curve_confidence: string;
    source: HazardSourceInfo;
  }[];
  finance: {
    annual_physical_damage: number;
    annual_business_interruption: number;
    annual_insurance_recovery: number;
    annual_premium: number;
    annual_net_cost: number;
    npv_climate_cost: number;
    value_impairment: number;
    value_impairment_pct: number;
    adjusted_value: number;
    ltv_before: number;
    ltv_after: number | null;
    dscr_before: number;
    dscr_after: number;
    covenant_breach: boolean;
    uninsurable_flag: boolean;
    yearly: {
      year: number;
      damage: number;
      business_interruption: number;
      insurance_recovery: number;
      premium: number;
      net_cost: number;
      discounted: number;
    }[];
  };
  adaptation: {
    name: string;
    capex: number;
    loss_reduction: number;
    benefit_npv: number;
    net_npv: number;
    bcr: number;
    payback_years: number | null;
    annual_saving: number;
    worth_doing: boolean;
  }[];
  spread: Spread;
  provenance: Provenance;
}

export interface Assumptions {
  discount_rate: number;
  horizon_years: number;
  downtime_days_per_damage_unit: number;
  ebitda_margin: number;
  bi_recovery_fraction: number;
  deductible_fraction: number;
  limit_fraction: number;
  coinsurance: number;
  premium_rate_on_eal: number;
  premium_escalation: number;
  insurable: boolean;
  hazard_growth: number;
}
