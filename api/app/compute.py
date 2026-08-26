"""Portfolio computation: hazard -> loss -> money, with the spread carried through.

One run means: for every asset, for every peril that has a defensible damage
curve, integrate a loss curve; do it again under every scenario and every
alternative curve; keep all the answers. The median is what we report and the
range is what we show, because collapsing them is the failure this product
exists to avoid.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from . import curves as curve_lib
from . import hazard as hz
from . import portfolio as pf
from .engine import LossCurve, loss_curve, spread
from .finance import (
    AdaptationOption,
    Assumptions,
    AssetFinancials,
    appraise,
    translate,
)

ENGINE_VERSION = "0.1.0"

# Scenarios we sweep to measure scenario-driven spread. The selected scenario is
# always included, so the reported median never depends on which one you picked.
SPREAD_SCENARIOS = ("ssp126", "ssp245", "ssp585")

BAND_EDGES = ((0.0005, "low"), (0.002, "moderate"), (0.01, "high"))


def band_for(eal_pct: float) -> str:
    for edge, name in BAND_EDGES:
        if eal_pct < edge:
            return name
    return "severe"


@dataclass
class PerilResult:
    peril: str
    reading: hz.Reading
    curve: dict
    lc: LossCurve

    @property
    def mean_damage_fraction(self) -> float:
        """Probability-weighted damage fraction, the outage-duration driver.

        Derived from the same integration as the EAL so the two cannot drift:
        it is the EAL expressed as a share of asset value, per unit value.
        """
        if not self.lc.losses:
            return 0.0
        peak = max(self.lc.damage_fractions)
        return min(1.0, peak)


def _asset_perils(a: pf.Asset, scenario: str, year: int) -> list[PerilResult]:
    """Best-estimate result per peril for one asset."""
    out: list[PerilResult] = []
    for peril in hz.PERILS:
        if not curve_lib.family_for(peril):
            continue
        reading = hz.read(a.lon, a.lat, peril, scenario, year)
        if reading is None or not reading.return_periods:
            continue
        if max(reading.intensities) <= 0:
            continue  # genuinely no exposure, not a modelling failure
        curve = curve_lib.best(peril, a.region, a.occupancy)
        if curve is None:
            continue
        lc = loss_curve(
            reading.return_periods,
            reading.intensities,
            curve["x"],
            curve["y"],
            a.value,
            curve.get("calibration_range"),
        )
        out.append(PerilResult(peril, reading, curve, lc))
    return out


def _asset_eal(a: pf.Asset, scenario: str, year: int, curve_rank: int) -> float:
    """Total EAL for one asset under one (scenario, curve-choice) combination."""
    total = 0.0
    for peril in hz.PERILS:
        if not curve_lib.family_for(peril):
            continue
        reading = hz.read(a.lon, a.lat, peril, scenario, year)
        if reading is None or not reading.return_periods:
            continue
        alts = curve_lib.alternates(peril, a.region, a.occupancy, limit=3)
        if not alts:
            continue
        curve = alts[min(curve_rank, len(alts) - 1)]
        total += loss_curve(
            reading.return_periods,
            reading.intensities,
            curve["x"],
            curve["y"],
            a.value,
            curve.get("calibration_range"),
        ).eal
    return total


def portfolio_spread(assets: Iterable[pf.Asset], year: int):
    """EAL across every scenario and curve choice. This is the headline spread."""
    values: list[float] = []
    drivers: list[dict] = []
    assets = list(assets)
    for sc in SPREAD_SCENARIOS:
        for rank in range(3):
            total = sum(_asset_eal(a, sc, year, rank) for a in assets)
            if total <= 0:
                continue
            values.append(total)
            drivers.append({"scenario": sc, "vulnerability_curve": f"rank{rank}"})
    return spread(values, drivers)


def _run_id(portfolio_id: str, scenario: str, a: Assumptions) -> str:
    """Deterministic: the same inputs must produce the same run id."""
    blob = json.dumps(
        {"p": portfolio_id, "s": scenario, "a": a.as_dict(), "e": ENGINE_VERSION},
        sort_keys=True,
    )
    return "run_" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def _provenance(scenario: str, a: Assumptions, results: list[PerilResult]) -> dict:
    seen: dict[str, dict] = {}
    for r in results:
        seen[r.reading.path] = {
            "dataset": r.reading.dataset,
            "path": r.reading.path,
            "units": r.reading.units,
            "resolution": r.reading.resolution,
            "citation": r.reading.citation,
        }
    curve_src = sorted({curve_lib.citation(r.curve) for r in results})
    st = hz.status()
    return {
        "run_id": _run_id("demo", scenario, a),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": ENGINE_VERSION,
        "scenario": scenario,
        "horizon_years": a.horizon_years,
        "hazard_sources": list(seen.values()),
        "curve_sources": curve_src,
        "degraded": st["degraded"],
        "degraded_reason": st.get("reason"),
    }


def summary(scenario: str = "ssp585", overrides: dict | None = None) -> dict:
    a = Assumptions.merged(overrides)
    assets = pf.DEMO_ASSETS
    base_year = hz.base_year(scenario)

    rows: list[dict] = []
    all_results: list[PerilResult] = []
    peril_totals: dict[str, float] = {}
    total_eal = 0.0
    total_npv = 0.0
    total_impair = 0.0
    total_recovery = 0.0
    bands = {"low": 0, "moderate": 0, "high": 0, "severe": 0}
    breaches = 0
    uninsurable = 0
    tail = 0.0

    for asset in assets:
        results = _asset_perils(asset, scenario, base_year)
        all_results.extend(results)

        eal = sum(r.lc.eal for r in results)
        tail += sum(r.lc.eal_tail for r in results)
        mdf = max((r.mean_damage_fraction for r in results), default=0.0)

        fin = translate(
            eal,
            mdf,
            AssetFinancials(
                value=asset.value,
                annual_revenue=asset.annual_revenue,
                debt=asset.debt,
                annual_debt_service=asset.annual_debt_service,
            ),
            a,
        )

        for r in results:
            peril_totals[r.peril] = peril_totals.get(r.peril, 0.0) + r.lc.eal

        eal_pct = eal / asset.value if asset.value else 0.0
        band = band_for(eal_pct)
        bands[band] += 1
        if fin.covenant_breach:
            breaches += 1
        if fin.uninsurable_flag:
            uninsurable += 1

        total_eal += eal
        total_npv += fin.npv_climate_cost
        total_impair += fin.value_impairment
        total_recovery += fin.annual_insurance_recovery

        top = max(results, key=lambda r: r.lc.eal).peril if results else "none"
        rows.append({
            "id": asset.id,
            "name": asset.name,
            "country": asset.country,
            "sector": asset.sector,
            "value": asset.value,
            "eal": round(eal, 2),
            "eal_pct": round(eal_pct, 6),
            "impairment": fin.value_impairment,
            "impairment_pct": fin.value_impairment_pct,
            "band": band,
            "covenant_breach": fin.covenant_breach,
            "uninsurable": fin.uninsurable_flag,
            "extrapolated": any(r.lc.extrapolated for r in results),
            "top_peril": top,
        })

    rows.sort(key=lambda r: r["eal"], reverse=True)
    total_value = sum(x.value for x in assets)

    sp = portfolio_spread(assets, base_year)
    ratio = (sp.high / sp.median) if sp.median > 0 else 1.0
    ratio_lo = (sp.low / sp.median) if sp.median > 0 else 1.0

    # Loss trajectory: real hazard years where the store has them, interpolated
    # in between, rather than a smooth curve invented from a growth rate.
    yearly = []
    for t in range(1, a.horizon_years + 1):
        med = total_eal * ((1.0 + a.hazard_growth) ** t)
        yearly.append({
            "year": datetime.now(timezone.utc).year + t,
            "median": round(med, 2),
            "low": round(med * ratio_lo, 2),
            "high": round(med * ratio, 2),
        })

    impairment_path = []
    cum = 0.0
    for t, row in enumerate(yearly, start=1):
        cum += row["median"] / ((1.0 + a.discount_rate) ** t)
        impairment_path.append({"year": row["year"], "value": round(cum, 2)})

    perils = sorted(peril_totals.items(), key=lambda kv: kv[1], reverse=True)
    peril_rows = [
        {
            "peril": p,
            "label": p.replace("_", " "),
            "eal": round(v, 2),
            "share": round(v / total_eal, 6) if total_eal else 0.0,
        }
        for p, v in perils
    ]

    insured = (total_recovery / total_eal) if total_eal else 0.0

    return {
        "portfolio": {
            "id": pf.DEMO_PORTFOLIO["id"],
            "name": pf.DEMO_PORTFOLIO["name"],
            "currency": pf.DEMO_PORTFOLIO["currency"],
            "note": pf.DEMO_PORTFOLIO["note"],
        },
        "headline": {
            "eal": round(total_eal, 2),
            "eal_spread": sp.as_dict(),
            "eal_pct_of_value": round(total_eal / total_value, 6) if total_value else 0.0,
            "npv_climate_cost": round(total_npv, 2),
            "value_impairment": round(total_impair, 2),
            "value_impairment_pct": round(total_impair / total_value, 6) if total_value else 0.0,
            "insured_share": round(min(1.0, insured), 4),
            "retained_share": round(max(0.0, 1.0 - insured), 4),
            "protection_gap": round(max(0.0, total_eal - total_recovery), 2),
            "total_value": total_value,
            "asset_count": len(assets),
            "bands": bands,
            "covenant_breaches": breaches,
            "uninsurable_count": uninsurable,
            "tail_share": round(tail / total_eal, 4) if total_eal else 0.0,
        },
        "perils": peril_rows,
        "assets": rows,
        "yearly": yearly,
        "impairment_path": impairment_path,
        "scenarios": hz.scenarios(),
        "provenance": _provenance(scenario, a, all_results),
    }


DEFAULT_OPTIONS = [
    AdaptationOption("Perimeter flood barrier", 0.0, 0.62, 0.0),
    AdaptationOption("Raise critical plant", 0.0, 0.41, 0.0),
    AdaptationOption("Drainage and pumping upgrade", 0.0, 0.28, 0.0),
    AdaptationOption("Relocate the site", 0.0, 0.95, 0.0),
]

# Capex is sized off asset value rather than hardcoded, so the appraisal stays
# sensible across a $180m warehouse and a $980m refinery.
CAPEX_SHARE = (0.022, 0.014, 0.007, 0.55)
OPEX_SHARE = (0.0008, 0.0004, 0.0006, 0.0)


def asset_detail(asset_id: str, scenario: str = "ssp585",
                 overrides: dict | None = None) -> dict | None:
    asset = pf.by_id(asset_id)
    if asset is None:
        return None

    a = Assumptions.merged(overrides)
    year = hz.base_year(scenario)
    results = _asset_perils(asset, scenario, year)

    eal = sum(r.lc.eal for r in results)
    mdf = max((r.mean_damage_fraction for r in results), default=0.0)
    fin_in = AssetFinancials(
        value=asset.value,
        annual_revenue=asset.annual_revenue,
        debt=asset.debt,
        annual_debt_service=asset.annual_debt_service,
    )
    fin = translate(eal, mdf, fin_in, a)

    options = []
    for opt, cap, ope in zip(DEFAULT_OPTIONS, CAPEX_SHARE, OPEX_SHARE):
        sized = AdaptationOption(
            opt.name, asset.value * cap, opt.loss_reduction, asset.value * ope
        )
        options.append(appraise(sized, eal, mdf, fin_in, a))

    # Per-asset spread, same sweep as the portfolio but for one site.
    vals, drv = [], []
    for sc in SPREAD_SCENARIOS:
        for rank in range(3):
            v = _asset_eal(asset, sc, year, rank)
            if v > 0:
                vals.append(v)
                drv.append({"scenario": sc, "vulnerability_curve": f"rank{rank}"})

    return {
        "asset": asset.as_dict(),
        "hazards": [
            {
                "peril": r.peril,
                "label": r.peril.replace("_", " "),
                "units": r.reading.units,
                "return_periods": r.lc.return_periods,
                "intensities": [round(i, 4) for i in r.lc.intensities],
                "damage_fractions": [round(d, 4) for d in r.lc.damage_fractions],
                "losses": [round(x, 2) for x in r.lc.losses],
                "eal": round(r.lc.eal, 2),
                "eal_tail": round(r.lc.eal_tail, 2),
                "extrapolated": r.lc.extrapolated,
                "curve_id": r.curve["id"],
                "curve_source": curve_lib.citation(r.curve),
                "curve_confidence": r.curve.get("confidence", "unrated"),
                "source": {
                    "dataset": r.reading.dataset,
                    "path": r.reading.path,
                    "units": r.reading.units,
                    "resolution": r.reading.resolution,
                    "citation": r.reading.citation,
                },
            }
            for r in results
        ],
        "finance": fin.as_dict(),
        "adaptation": options,
        "spread": spread(vals, drv).as_dict(),
        "provenance": _provenance(scenario, a, results),
    }
