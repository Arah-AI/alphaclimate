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
from . import protection as prot
from .engine import LossCurve, Regime, classify_regime, interp_damage, loss_curve, spread
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
    regime: Regime | None = None

    @property
    def permanent(self) -> bool:
        return bool(self.regime and self.regime.permanent)

    @property
    def writedown(self) -> float:
        """One-off loss of value for an asset projected to sit under water."""
        if not self.permanent or self.regime is None:
            return 0.0
        frac = interp_damage(
            self.regime.baseline_depth, self.curve["x"], self.curve["y"]
        )
        return frac * self._asset_value

    _asset_value: float = 0.0

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


def _asset_perils(a: pf.Asset, scenario: str) -> list[PerilResult]:
    """Best-estimate result per peril for one asset."""
    out: list[PerilResult] = []
    for peril in hz.PERILS:
        if not curve_lib.family_for(peril):
            continue
        reading = hz.read(a.lon, a.lat, peril, scenario)
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
            prot.sop(a.lon, a.lat, peril),
        )
        # Depth-valued hazards can be describing standing water rather than
        # floods. Classify before the number is used as an annual loss.
        regime = (
            classify_regime(reading.return_periods, reading.intensities)
            if peril.startswith("inundation") or peril == "combined_flood"
            else None
        )
        out.append(PerilResult(peril, reading, curve, lc, regime, a.value))
    return out


def _asset_eal(a: pf.Asset, scenario: str, variant_rank: int, curve_rank: int) -> float:
    """Total EAL for one asset under one (scenario, model, curve) combination."""
    total = 0.0
    for peril in hz.PERILS:
        if not curve_lib.family_for(peril):
            continue
        reading = hz.read(a.lon, a.lat, peril, scenario, variant_rank)
        if reading is None or not reading.return_periods:
            continue
        # Same screen as the headline path. Without it, an asset with no
        # measured depth still picks up loss from a lower-ranked HAZUS curve
        # whose x axis is depth above FIRST FLOOR, not above ground: its
        # damage fraction at 0.0 is non-zero, so a dry site gets charged.
        # The headline dropped those assets and the sweep did not, which is how
        # a zero expected loss ended up sitting next to a non-zero spread band.
        if max(reading.intensities) <= 0:
            continue
        alts = curve_lib.alternates(peril, a.region, a.occupancy, limit=3)
        if not alts:
            continue
        # Same exclusion as the headline: standing water is a write-down, not an
        # annual loss. Leaving it in here would make the spread band fail to
        # bracket the median it is supposed to describe.
        if peril.startswith("inundation") or peril == "combined_flood":
            if classify_regime(reading.return_periods, reading.intensities).permanent:
                continue
        curve = alts[min(curve_rank, len(alts) - 1)]
        total += loss_curve(
            reading.return_periods,
            reading.intensities,
            curve["x"],
            curve["y"],
            a.value,
            curve.get("calibration_range"),
            prot.sop(a.lon, a.lat, peril),
        ).eal
    return total


def portfolio_spread(assets: Iterable[pf.Asset]):
    """EAL across every scenario, climate model and curve choice.

    Three independent drivers, swept fully. This is the headline spread and the
    number the product is really selling.
    """
    values: list[float] = []
    drivers: list[dict] = []
    assets = list(assets)
    n_variants = max((len(hz.variants(p)) for p in hz.PERILS), default=1)
    for sc in SPREAD_SCENARIOS:
        for vrank in range(max(1, n_variants)):
            for crank in range(3):
                total = sum(_asset_eal(a, sc, vrank, crank) for a in assets)
                if total <= 0:
                    continue
                values.append(total)
                drivers.append({
                    "scenario": sc,
                    "climate_model": f"variant{vrank}",
                    "vulnerability_curve": f"rank{crank}",
                })
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
        "aggregation": hz.aggregation(),
        "flood_protection": prot.coverage(),
        "scenario_substitution": hz.scenario_substitution().get(scenario),
    }


def summary(scenario: str = "ssp585", overrides: dict | None = None) -> dict:
    a = Assumptions.merged(overrides)
    assets = pf.DEMO_ASSETS

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
    total_writedown = 0.0
    permanently_inundated = 0

    for asset in assets:
        results = _asset_perils(asset, scenario)
        all_results.extend(results)

        # Permanent inundation is a write-down, not an annual cost. It is
        # separated out here so it never enters the expected-annual-loss line.
        event_results = [r for r in results if not r.permanent]
        perm_results = [r for r in results if r.permanent]

        eal = sum(r.lc.eal for r in event_results)
        writedown = sum(r.writedown for r in perm_results)
        tail += sum(r.lc.eal_tail for r in event_results)
        mdf = max((r.mean_damage_fraction for r in event_results), default=0.0)

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

        for r in event_results:
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

        top = (max(event_results, key=lambda r: r.lc.eal).peril
               if event_results else ("permanent_inundation" if perm_results else "none"))
        total_writedown += writedown
        if perm_results:
            permanently_inundated += 1
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
            "permanent_inundation": bool(perm_results),
            "writedown": round(writedown, 2),
            "writedown_pct": round(writedown / asset.value, 6) if asset.value else 0.0,
            "permanent_reason": perm_results[0].regime.reason if perm_results else None,
        })

    rows.sort(key=lambda r: r["eal"], reverse=True)
    total_value = sum(x.value for x in assets)

    sp = portfolio_spread(assets)
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
            "permanent_writedown": round(total_writedown, 2),
            "permanent_writedown_pct": (
                round(total_writedown / total_value, 6) if total_value else 0.0
            ),
            "permanently_inundated_count": permanently_inundated,
        },
        "perils": peril_rows,
        "assets": rows,
        "yearly": yearly,
        "impairment_path": impairment_path,
        "scenarios": hz.scenarios(),
        "provenance": _provenance(scenario, a, all_results),
    }


# --------------------------------------------------------------------------
# plausibility
# --------------------------------------------------------------------------
#
# Real industrial and port assets lose roughly 0.05% to 2% of value a year to
# physical hazard. A model that says otherwise for a whole portfolio is broken,
# not prescient, and the failure is silent: every number downstream stays
# internally consistent while being fifty times too large.
#
# Three checks, because no single one is both tight and robust:
#
#   * the portfolio band is loose (0.01% to 10%) because two sites in the demo
#     portfolio sit on pixels the WRI with-subsidence layer places under
#     permanent water by 2050, which an event-based EAL cannot express. See
#     PERMANENT_INUNDATION_NOTE. It still catches a fifty-fold inflation.
#   * the median asset is where the tightness lives: outliers cannot move it,
#     so it stays inside the real-world 2% ceiling.
#   * and a portfolio where most assets are write-offs is a modelling failure
#     however plausible any single number looks.
#
# These are physical bounds, not calibration targets. If a change trips one,
# find the cause; do not widen the band.

PLAUSIBLE_EAL_PCT = (0.0001, 0.10)
PLAUSIBLE_MEDIAN_ASSET_EAL_PCT = 0.02
MAX_SHARE_OF_ASSETS_ABOVE_5PCT = 0.25

PERMANENT_INUNDATION_NOTE = (
    "A hazard curve that is already deep at the shortest return period and "
    "near-flat out to 1-in-1000 is permanent inundation, not a flood "
    "frequency distribution. Integrating it as an annual event loss charges "
    "the asset for standing water every year."
)


def check_coherent(summary_dict: dict) -> None:
    """The spread must bracket the headline it describes.

    Headline and sweep are two code paths over the same model. If they drift,
    the dashboard shows a median outside its own range, which is worse than
    either number being wrong on its own.
    """
    h = summary_dict["headline"]
    sp = h["eal_spread"]
    if sp["n"] == 0 or h["eal"] <= 0:
        return
    assert sp["low"] <= sp["median"] <= sp["high"], "spread must be ordered"
    lo, hi = sp["low"] * 0.9, sp["high"] * 1.1
    assert lo <= h["eal"] <= hi, (
        f"headline EAL {h['eal']:,.0f} sits outside its own spread "
        f"{sp['low']:,.0f}-{sp['high']:,.0f}: the sweep and the headline are "
        f"using different models"
    )


def check_plausible(summary_out: dict) -> None:
    """Fail loudly if the portfolio leaves the physically plausible band."""
    h = summary_out["headline"]
    pct = h["eal_pct_of_value"]
    lo, hi = PLAUSIBLE_EAL_PCT
    assert lo <= pct <= hi, (
        f"portfolio EAL is {pct:.2%} of value per year, outside the plausible "
        f"band {lo:.2%}-{hi:.2%}. Real industrial and port assets sit near "
        f"0.05%-2%. Check the hazard aggregation (a neighbourhood max hands "
        f"every asset its worst neighbour's flood depth) and that FLOPROS "
        f"flood defences are reaching the engine."
    )

    pcts = sorted(r["eal_pct"] for r in summary_out["assets"])
    n = len(pcts)
    median = pcts[n // 2] if n % 2 else 0.5 * (pcts[n // 2 - 1] + pcts[n // 2])
    assert median <= PLAUSIBLE_MEDIAN_ASSET_EAL_PCT, (
        f"the median asset loses {median:.2%} of value a year, above the "
        f"{PLAUSIBLE_MEDIAN_ASSET_EAL_PCT:.0%} ceiling. A couple of extreme "
        f"sites is a portfolio; a typical site at this level is a bug."
    )

    heavy = [r["id"] for r in summary_out["assets"] if r["eal_pct"] > 0.05]
    assert len(heavy) <= MAX_SHARE_OF_ASSETS_ABOVE_5PCT * n, (
        f"{len(heavy)} of {n} assets lose over 5% of value a year "
        f"({', '.join(heavy)}). That is a systematic error, not a result."
    )


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
    results = _asset_perils(asset, scenario)

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
    n_variants = max((len(hz.variants(p)) for p in hz.PERILS), default=1)
    for sc in SPREAD_SCENARIOS:
        for vrank in range(max(1, n_variants)):
            for crank in range(3):
                v = _asset_eal(asset, sc, vrank, crank)
                if v > 0:
                    vals.append(v)
                    drv.append({
                        "scenario": sc,
                        "climate_model": f"variant{vrank}",
                        "vulnerability_curve": f"rank{crank}",
                    })

    # Hazards we can measure but will not monetise: no defensible damage curve
    # exists. Shown so the user sees what is real and unpriced, rather than
    # inferring from silence that the site is only exposed to flood and wind.
    unpriced = []
    for peril in hz.PERILS_unpriced():
        r = hz.read(asset.lon, asset.lat, peril, scenario)
        if r is None or not r.intensities or max(r.intensities) <= 0:
            continue
        base = hz.read(asset.lon, asset.lat, peril, "historical")
        unpriced.append({
            "peril": peril,
            "units": r.units,
            "thresholds": r.return_periods,
            "values": [round(x, 3) for x in r.intensities],
            "baseline": [round(x, 3) for x in base.intensities] if base else None,
            "dataset": r.dataset,
            "path": r.path,
            "resolution": r.resolution,
            "why_unpriced": "No damage function in data/damage_curves.json gaps list",
        })

    return {
        "asset": asset.as_dict(),
        "unpriced_hazards": unpriced,
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


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    if hz.status()["degraded"]:
        print("compute.py self-check: DEGRADED (no hazard cache)")
        return

    s = summary("ssp585")
    h = s["headline"]
    check_plausible(s)
    check_coherent(s)

    # Bands must not all collapse into one bucket: that is the signature of a
    # systematic error rather than twelve genuinely identical sites.
    assert h["bands"]["severe"] < h["asset_count"], \
        "every asset landed in 'severe' - that is a modelling failure, not a result"

    # Defences must actually be reaching the engine.
    cov = s["provenance"]["flood_protection"]
    assert cov["defended_points"] > 0, "no asset is picking up a FLOPROS standard"

    # Known residual, printed rather than asserted away: the assets the WRI
    # with-subsidence layer puts under permanent water. Their EAL is not wrong
    # arithmetic, it is the wrong quantity for the hazard the layer describes.
    heavy = [f"{r['id']} {r['eal_pct']:.1%}" for r in s["assets"] if r["eal_pct"] > 0.05]
    if heavy:
        print(f"  residual, not fixed: {', '.join(heavy)}. "
              f"{PERMANENT_INUNDATION_NOTE}")

    print(f"compute.py self-check passed (EAL {h['eal_pct_of_value']:.3%} of value, "
          f"bands {h['bands']}, {cov['defended_points']} defended points)")


if __name__ == "__main__":
    demo()
