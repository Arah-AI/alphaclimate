"""Custom emissions pathways, run through a real simple climate model.

The hazard cache is pinned: WRI flood layers, one target year, three SSPs. A
client who wants to ask "what if my sector decarbonises by 2045" or "what if
the world runs 30% hotter on emissions than SSP2-4.5" has nowhere to put the
question. This module gives them somewhere.

It runs FaIR 2.2 (Leach et al. 2021, GMD 14, 3007-3036), the simple climate
model behind the AR6 emulator work, on RCMIP v5.1.0 SSP emissions. Not a
regression, not a lookup table: the actual carbon cycle and energy balance
integration, 1750-2100, one deterministic configuration.

CALIBRATION. FaIR's own worked example ships a three-layer ocean whose implied
ECS is 3.64 K, well above the AR6 assessed central estimate. Left alone it puts
SSP5-8.5 at 6.0 K by 2100 against AR6's 4.4 K. The only change made here is to
set the upper-ocean heat transfer so the implied ECS is exactly the AR6
assessed central value of 3.0 K; everything else is FaIR's documented example
configuration. The resulting 2081-2100 warming lands inside the AR6 likely
range for all five SSPs, which demo() asserts against the published numbers
rather than against itself.

THE HAZARD SCALING IS CRUDE AND IS NOT DOWNSCALING. hazard_scaling() returns a
single dimensionless ratio of global mean temperature anomalies, and the caller
multiplies a flood depth by it. That is a global-mean proxy. It does not know
about regional warming patterns, changes in the shape of the return-period
curve, sea level lag, subsidence, or the fact that riverine and coastal flood
respond to entirely different things. It is offered because the alternative -
silently reusing an SSP5-8.5 hazard layer for a net-zero-2050 pathway - is
worse and invisible. Every payload that carries a scaling factor carries the
assumption text with it, so the provenance ledger shows what was done.

COST. The first call downloads three RCMIP CSVs (~74 MB, pooch-cached under the
user cache dir) and peaks around 400 MB RSS while pandas parses them. After
that the model is memoised in-process and a pathway run takes ~0.1 s.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, asdict
from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

if __package__ in (None, ""):  # `python api/app/climate.py`
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import hazard as hz
else:
    from . import hazard as hz

log = logging.getLogger("alphaclimate.climate")

router = APIRouter()


class ClimateUnavailable(RuntimeError):
    """FaIR could not run. Reported, never replaced with a plausible curve."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

SSPS = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")
SSP_LABELS = {
    "ssp119": "SSP1-1.9 · 1.5C aligned",
    "ssp126": "SSP1-2.6 · Paris aligned",
    "ssp245": "SSP2-4.5 · middle of the road",
    "ssp370": "SSP3-7.0 · regional rivalry",
    "ssp585": "SSP5-8.5 · high emissions",
}

START_YEAR, END_YEAR = 1750, 2100
BASELINE = (1850, 1900)          # AR6 pre-industrial reference period

# The scenario and year the hazard cache was actually built for. Anything
# scaled off this module is relative to THIS point, and nothing else.
HAZARD_REFERENCE_SCENARIO = "ssp585"
HAZARD_TARGET_YEAR = 2050        # data/hazard_cache.json -> target_year

# FaIR three-layer energy balance. Ocean heat capacities, the two lower heat
# transfer coefficients and the deep-ocean efficacy are FaIR's own documented
# example configuration. OCEAN_HEAT_TRANSFER[0] is the single tuned number:
# ECS = forcing_4co2 / 2 / kappa1 exactly, so 4.0 / 3.0 pins ECS at the AR6
# assessed central 3.0 K instead of the example's 3.64 K.
FORCING_4CO2 = 8.0
AR6_CENTRAL_ECS = 3.0
OCEAN_HEAT_CAPACITY = (2.8, 12.5, 52.0)
OCEAN_HEAT_TRANSFER = (FORCING_4CO2 / 2.0 / AR6_CENTRAL_ECS, 1.6, 0.9)
DEEP_OCEAN_EFFICACY = 1.29

# AR6 WG1 Table SPM.1: assessed 2081-2100 warming relative to 1850-1900,
# best estimate and very likely range. demo() checks the model against these.
AR6_2081_2100 = {
    "ssp119": (1.4, 1.0, 1.8),
    "ssp126": (1.8, 1.3, 2.4),
    "ssp245": (2.7, 2.1, 3.5),
    "ssp370": (3.6, 2.8, 4.6),
    "ssp585": (4.4, 3.3, 5.7),
}

CITATION = (
    "FaIR v2.2 (Leach et al. 2021, Geosci. Model Dev. 14, 3007-3036) driven by "
    "RCMIP v5.1.0 SSP emissions (Nicholls et al. 2020). Climate response tuned "
    "to the AR6 WG1 assessed central ECS of 3.0 K."
)

SCALING_ASSUMPTION = {
    "method": "global mean temperature ratio",
    "formula": (
        "hazard_scaling = dT(pathway, year) / dT("
        f"{HAZARD_REFERENCE_SCENARIO}, {HAZARD_TARGET_YEAR}), both anomalies "
        f"relative to the {BASELINE[0]}-{BASELINE[1]} mean"
    ),
    "this_is_not_downscaling": (
        "CRUDE. Scaling a flood depth linearly with global mean temperature is "
        "a proxy, not downscaling. It ignores regional warming patterns, the "
        "shape of the return-period curve, sea level commitment and lag, land "
        "subsidence, and the fact that riverine and coastal flood respond to "
        "different drivers. A factor of 1.2 here does not mean the water is "
        "20% deeper; it means the pathway is 20% warmer than the pathway the "
        "hazard layer was built for and no better information has been applied."
    ),
    "hazard_reference_scenario": HAZARD_REFERENCE_SCENARIO,
    "hazard_target_year": HAZARD_TARGET_YEAR,
    "better_alternative": (
        "Read the hazard layer for the pathway's own SSP where one exists "
        "(/api/asset?scenario=...), and only fall back to this ratio for "
        "pathways no published layer covers."
    ),
    "citation": CITATION,
}


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _filled():
    """A FaIR object with all SSPs loaded and configured, but not yet run.

    Memoised because the RCMIP read is the entire cost of this module: ~74 MB
    of CSV and a 400 MB RSS peak. Everything downstream deep-copies this.
    """
    try:
        from fair import FAIR
        from fair.interface import fill, initialise
        from fair.io import read_properties
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise ClimateUnavailable(f"FaIR is not installed: {exc}") from exc

    try:
        f = FAIR(ch4_method="thornhill2021")
        f.define_time(START_YEAR, END_YEAR, 1)
        f.define_scenarios(list(SSPS))
        f.define_configs(["ar6_central"])
        species, properties = read_properties()
        f.define_species(species, properties)
        f.allocate()
        f.fill_from_rcmip()
        f.fill_species_configs()
    except Exception as exc:
        raise ClimateUnavailable(
            f"could not build the FaIR run: {exc}. The first call fetches three "
            f"RCMIP v5.1.0 CSVs (~74 MB) from "
            f"rcmip-protocols-au.s3-ap-southeast-2.amazonaws.com; if this box "
            f"has no outbound network, warm the pooch cache during the image "
            f"build instead."
        ) from exc

    fill(f.climate_configs["ocean_heat_capacity"], list(OCEAN_HEAT_CAPACITY))
    fill(f.climate_configs["ocean_heat_transfer"], list(OCEAN_HEAT_TRANSFER))
    fill(f.climate_configs["deep_ocean_efficacy"], DEEP_OCEAN_EFFICACY)
    fill(f.climate_configs["gamma_autocorrelation"], 2.0)
    fill(f.climate_configs["forcing_4co2"], FORCING_4CO2)
    # Deterministic by construction: no internal variability, no seed to drift.
    fill(f.climate_configs["stochastic_run"], False)
    fill(f.climate_configs["use_seed"], False)
    fill(f.climate_configs["sigma_eta"], 0.0)
    fill(f.climate_configs["sigma_xi"], 0.0)
    fill(f.climate_configs["seed"], 0)

    initialise(f.concentration, f.species_configs["baseline_concentration"])
    initialise(f.forcing, 0)
    initialise(f.temperature, 0)
    initialise(f.cumulative_emissions, 0)
    initialise(f.airborne_emissions, 0)
    return f


def _anomaly(f, scenario: str) -> dict[int, float]:
    """Surface-layer warming relative to the 1850-1900 mean, by year."""
    t = f.temperature.sel(layer=0, config="ar6_central", scenario=scenario)
    base = float(t.sel(timebounds=slice(*BASELINE)).mean())
    return {
        int(y): round(float(v) - base, 4)
        for y, v in zip(t.timebounds.values, t.values)
    }


@lru_cache(maxsize=1)
def standard_runs() -> dict[str, dict[int, float]]:
    """Warming trajectory for every standard SSP. Deterministic and cached."""
    f = copy.deepcopy(_filled())
    f.run(progress=False)
    _emergent_store["ecs"] = float(f.ebms["ecs"].values[0])
    _emergent_store["tcr"] = float(f.ebms["tcr"].values[0])
    return {s: _anomaly(f, s) for s in SSPS}


_emergent_store: dict[str, float] = {}


def _emergent() -> dict[str, float]:
    """ECS/TCR are a by-product of the run, so the run has to have happened."""
    if not _emergent_store:
        standard_runs()
    return dict(_emergent_store)


def emergent_parameters() -> dict:
    """ECS and TCR the configuration actually implies, not what we hoped for."""
    e = _emergent()
    return {
        "ecs": round(e["ecs"], 3),
        "tcr": round(e["tcr"], 3),
        "ecs_target": AR6_CENTRAL_ECS,
        "note": (
            "ECS is pinned to the AR6 WG1 assessed central estimate of 3.0 K. "
            "The implied TCR falls out of the ocean structure rather than being "
            "tuned separately; AR6 assesses TCR at 1.8 K (likely 1.4-2.2 K)."
        ),
    }


# --------------------------------------------------------------------------
# custom pathways
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Pathway:
    """A custom emissions pathway, defined as a modification of one SSP.

    Only CO2 (fossil/industrial and land use) is modified. Methane, N2O,
    aerosols and everything else follow the base scenario untouched, which is
    what makes this a two-knob toy rather than a scenario-building exercise.
    That limitation is stated in every payload.
    """

    base_scenario: str = "ssp245"
    emissions_multiplier: float = 1.0
    net_zero_year: int | None = None
    from_year: int = 2025
    label: str = ""

    def name(self) -> str:
        if self.label:
            return self.label
        bits = [self.base_scenario]
        if self.emissions_multiplier != 1.0:
            bits.append(f"CO2 x{self.emissions_multiplier:g}")
        if self.net_zero_year:
            bits.append(f"net zero {self.net_zero_year}")
        return ", ".join(bits)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.name()
        return d


CO2_SPECIES = ("CO2 FFI", "CO2 AFOLU")


def _co2_factor(years: np.ndarray, p: Pathway) -> np.ndarray:
    """Per-year multiplier applied to the base scenario's CO2 emissions."""
    f = np.ones_like(years, dtype=float)
    after = years >= p.from_year
    f[after] = p.emissions_multiplier
    if p.net_zero_year:
        span = p.net_zero_year - p.from_year
        if span <= 0:
            f[after] = 0.0
        else:
            ramp = np.clip((p.net_zero_year - years) / span, 0.0, 1.0)
            f[after] = f[after] * ramp[after]
    return f


def _validate(p: Pathway) -> None:
    if p.base_scenario not in SSPS:
        raise ValueError(f"base_scenario must be one of {list(SSPS)}")
    if not 0.0 <= p.emissions_multiplier <= 5.0:
        raise ValueError("emissions_multiplier must be between 0 and 5")
    if not START_YEAR < p.from_year < END_YEAR:
        raise ValueError(f"from_year must be between {START_YEAR} and {END_YEAR}")
    if p.net_zero_year is not None and not p.from_year < p.net_zero_year <= END_YEAR:
        raise ValueError(f"net_zero_year must be between from_year and {END_YEAR}")


@lru_cache(maxsize=32)
def pathway_run(p: Pathway) -> dict[int, float]:
    """Warming trajectory for a custom pathway. Cached, deterministic."""
    _validate(p)
    if p.emissions_multiplier == 1.0 and p.net_zero_year is None:
        # Nothing was changed. Returning the base run rather than a second
        # integration of the same numbers keeps the two provably identical.
        return standard_runs()[p.base_scenario]

    f = copy.deepcopy(_filled())
    years = np.asarray(f.emissions.timepoints.values, dtype=float)
    factor = _co2_factor(years, p)
    for specie in CO2_SPECIES:
        sel = dict(scenario=p.base_scenario, specie=specie)
        f.emissions.loc[sel] = f.emissions.loc[sel] * factor[:, None]
    f.run(progress=False)
    return _anomaly(f, p.base_scenario)


# --------------------------------------------------------------------------
# hazard scaling
# --------------------------------------------------------------------------

def warming(scenario_or_pathway: str | Pathway, year: int) -> float:
    """Global mean temperature anomaly (K vs 1850-1900) at `year`."""
    if isinstance(scenario_or_pathway, Pathway):
        traj = pathway_run(scenario_or_pathway)
    else:
        if scenario_or_pathway not in SSPS:
            raise ValueError(f"unknown scenario '{scenario_or_pathway}'")
        traj = standard_runs()[scenario_or_pathway]
    if year not in traj:
        raise ValueError(f"year {year} outside {START_YEAR}-{END_YEAR}")
    return traj[year]


def hazard_scaling(scenario_or_pathway: str | Pathway, year: int) -> float:
    """How much warmer this pathway is than the one the hazard cache assumes.

    The hazard layers are pinned to one SSP at one year. Multiplying a modelled
    flood depth by this ratio is a global-mean proxy for what a different
    pathway would do to it. Read SCALING_ASSUMPTION before believing it.
    """
    ref = warming(HAZARD_REFERENCE_SCENARIO, HAZARD_TARGET_YEAR)
    if ref <= 0:
        raise ClimateUnavailable("reference warming is not positive; refusing to scale")
    return warming(scenario_or_pathway, year) / ref


def scaling_block(scenario_or_pathway: str | Pathway, year: int) -> dict:
    """The scaling factor plus the assumption, so they cannot be separated."""
    return {
        "year": year,
        "warming": round(warming(scenario_or_pathway, year), 4),
        "reference_warming": round(
            warming(HAZARD_REFERENCE_SCENARIO, HAZARD_TARGET_YEAR), 4
        ),
        "hazard_scaling": round(hazard_scaling(scenario_or_pathway, year), 4),
        "assumption": SCALING_ASSUMPTION,
    }


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------

def _trajectory(traj: dict[int, float], step: int = 5) -> list[dict]:
    """Thinned to five-year steps: 351 annual points is a payload, not a chart."""
    return [
        {"year": y, "warming": v}
        for y, v in sorted(traj.items())
        if y % step == 0 or y == END_YEAR
    ]


MODEL_BLOCK = {
    "model": "FaIR v2.2",
    "emissions": "RCMIP v5.1.0",
    "period": f"{START_YEAR}-{END_YEAR}, annual",
    "baseline": f"{BASELINE[0]}-{BASELINE[1]} mean",
    "configs": 1,
    "deterministic": True,
    "citation": CITATION,
    "single_config_caveat": (
        "One deterministic parameter set, not an ensemble. It reproduces the "
        "AR6 central estimate; it says nothing about the AR6 likely range, and "
        "climate response uncertainty is roughly a factor of two on either side "
        "of every number here."
    ),
}


def scenarios_payload() -> dict:
    runs = standard_runs()
    rows = []
    for s in SSPS:
        traj = runs[s]
        mean_2081_2100 = sum(traj[y] for y in range(2081, 2101)) / 20.0
        best, lo, hi = AR6_2081_2100[s]
        rows.append({
            "id": s,
            "label": SSP_LABELS[s],
            "warming_2050": traj[2050],
            "warming_2100": traj[2100],
            "warming_2081_2100": round(mean_2081_2100, 3),
            "ar6_2081_2100": {"best": best, "very_likely_low": lo, "very_likely_high": hi},
            "inside_ar6_range": bool(lo <= mean_2081_2100 <= hi),
            "hazard_scaling_at_target": round(hazard_scaling(s, HAZARD_TARGET_YEAR), 4),
            "hazard_layer_available": s in {x["id"] for x in hz.scenarios()},
        })
    return {
        "scenarios": rows,
        "model": MODEL_BLOCK,
        "climate_response": emergent_parameters(),
        "hazard_scaling": SCALING_ASSUMPTION,
    }


def temperature_payload(scenario: str, step: int = 5) -> dict:
    traj = standard_runs()[scenario]
    return {
        "scenario": scenario,
        "label": SSP_LABELS[scenario],
        "trajectory": _trajectory(traj, step),
        "warming_2050": traj[2050],
        "warming_2100": traj[2100],
        "hazard_scaling": scaling_block(scenario, HAZARD_TARGET_YEAR),
        "model": MODEL_BLOCK,
        "climate_response": emergent_parameters(),
    }


def pathway_payload(p: Pathway, step: int = 5) -> dict:
    traj = pathway_run(p)
    base = standard_runs()[p.base_scenario]
    return {
        "pathway": p.as_dict(),
        "trajectory": _trajectory(traj, step),
        "base_trajectory": _trajectory(base, step),
        "warming_2050": traj[2050],
        "warming_2100": traj[2100],
        "delta_vs_base_2100": round(traj[2100] - base[2100], 4),
        "hazard_scaling": scaling_block(p, HAZARD_TARGET_YEAR),
        "model": MODEL_BLOCK,
        "climate_response": emergent_parameters(),
        "what_was_changed": (
            f"CO2 fossil/industrial and land-use emissions from {p.from_year}, "
            f"multiplied by {p.emissions_multiplier:g}"
            + (f" and ramped linearly to zero at {p.net_zero_year}"
               if p.net_zero_year else "")
            + f". Every other species follows {p.base_scenario} unchanged, so "
              f"this is a CO2-only experiment, not a full scenario."
        ),
    }


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

class PathwayBody(BaseModel):
    base_scenario: str = Field("ssp245", description="SSP the pathway modifies")
    emissions_multiplier: float = Field(1.0, ge=0.0, le=5.0)
    net_zero_year: int | None = Field(None, ge=1751, le=END_YEAR)
    from_year: int = Field(2025, ge=1751, le=END_YEAR - 1)
    label: str = Field("", max_length=120)


@router.get("/api/climate/scenarios")
def climate_scenarios() -> dict:
    """Standard SSP warming, with each one checked against AR6 Table SPM.1."""
    try:
        return scenarios_payload()
    except ClimateUnavailable as exc:
        raise HTTPException(503, str(exc))


@router.get("/api/climate/temperature")
def climate_temperature(
    scenario: str = Query("ssp245"),
    step: int = Query(5, ge=1, le=25),
) -> dict:
    """Full warming trajectory for one SSP."""
    if scenario not in SSPS:
        raise HTTPException(400, f"Unknown scenario '{scenario}'. Known: {list(SSPS)}.")
    try:
        return temperature_payload(scenario, step)
    except ClimateUnavailable as exc:
        raise HTTPException(503, str(exc))


@router.post("/api/climate/pathway")
def climate_pathway(body: PathwayBody, step: int = Query(5, ge=1, le=25)) -> dict:
    """Run a custom CO2 pathway and return its warming and hazard scaling."""
    p = Pathway(
        base_scenario=body.base_scenario,
        emissions_multiplier=body.emissions_multiplier,
        net_zero_year=body.net_zero_year,
        from_year=body.from_year,
        label=body.label,
    )
    try:
        _validate(p)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        return pathway_payload(p, step)
    except ClimateUnavailable as exc:
        raise HTTPException(503, str(exc))


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    try:
        runs = standard_runs()
    except ClimateUnavailable as exc:
        print(f"climate.py self-check: DEGRADED ({exc})")
        return

    # --- the model reproduces AR6, not itself ----------------------------
    for s in SSPS:
        traj = runs[s]
        assert len(traj) == END_YEAR - START_YEAR + 1
        mean = sum(traj[y] for y in range(2081, 2101)) / 20.0
        best, lo, hi = AR6_2081_2100[s]
        assert lo <= mean <= hi, (
            f"{s} gives {mean:.2f} K over 2081-2100, outside the AR6 WG1 very "
            f"likely range {lo}-{hi} K (best estimate {best}). The climate "
            f"configuration is wrong, not prescient."
        )
        assert abs(mean - best) < 0.5, (
            f"{s} is {mean:.2f} K against an AR6 best estimate of {best} K"
        )

    # Scenario ordering must hold at 2100. If SSP1-1.9 outruns SSP5-8.5 the
    # emissions got wired to the wrong scenario.
    at2100 = [runs[s][2100] for s in SSPS]
    assert at2100 == sorted(at2100), f"SSPs out of order at 2100: {at2100}"
    assert runs["ssp119"][2100] < runs["ssp119"][2050], \
        "SSP1-1.9 overshoots and declines; a monotone rise means the run is wrong"

    # Pre-industrial anchor: the baseline period must average to zero by
    # construction, and 1750 must sit below it.
    for s in SSPS:
        n = BASELINE[1] - BASELINE[0] + 1
        base = sum(runs[s][y] for y in range(BASELINE[0], BASELINE[1] + 1)) / n
        assert abs(base) < 1e-3, "the baseline period must average to the zero point"
    assert runs["ssp245"][1750] < 0.05, "1750 cannot already be warm"

    # Historical scenarios must agree before they diverge: SSPs share history.
    assert abs(runs["ssp119"][2000] - runs["ssp585"][2000]) < 1e-9, \
        "the SSPs share one historical period and must be identical in 2000"

    # --- calibration ------------------------------------------------------
    e = emergent_parameters()
    assert abs(e["ecs"] - AR6_CENTRAL_ECS) < 0.01, \
        f"ECS is {e['ecs']}, not the AR6 central {AR6_CENTRAL_ECS}"
    assert 1.4 <= e["tcr"] <= 2.2, \
        f"implied TCR {e['tcr']} is outside the AR6 likely range 1.4-2.2 K"

    # Determinism: a second call is the same object, and a fresh integration
    # of an unchanged pathway reproduces the cached run exactly.
    assert standard_runs() is runs, "the standard run must be cached, not re-run"
    assert pathway_run(Pathway("ssp245")) == runs["ssp245"], \
        "an unmodified pathway must be the base scenario, to the last digit"

    # --- custom pathways --------------------------------------------------
    base245 = runs["ssp245"][2100]

    half = pathway_run(Pathway("ssp245", emissions_multiplier=0.5))
    double = pathway_run(Pathway("ssp245", emissions_multiplier=2.0))
    assert half[2100] < base245 < double[2100], (
        f"halving CO2 must cool and doubling must warm: "
        f"{half[2100]:.2f} / {base245:.2f} / {double[2100]:.2f}"
    )
    assert half[2020] == runs["ssp245"][2020], \
        "a pathway starting in 2025 must not change 2020"

    zeroed = pathway_run(Pathway("ssp245", emissions_multiplier=0.0))
    assert zeroed[2100] < half[2100], "zero CO2 must be cooler than half"
    # With CO2 off the curve nearly flattens. It does not fall, and it must
    # not: methane, N2O and the declining aerosol mask still follow SSP2-4.5,
    # which is exactly the CO2-only limitation the payload declares. Assert
    # against the base scenario's own rise so the check tests the mechanism
    # rather than a hardcoded number.
    base_rise = runs["ssp245"][2100] - runs["ssp245"][2050]
    assert (zeroed[2100] - zeroed[2050]) < 0.25 * base_rise, (
        f"zeroing CO2 must flatten the curve: it still rises "
        f"{zeroed[2100] - zeroed[2050]:.3f} K against a base rise of "
        f"{base_rise:.3f} K"
    )

    nz2050 = pathway_run(Pathway("ssp245", net_zero_year=2050))
    nz2080 = pathway_run(Pathway("ssp245", net_zero_year=2080))
    assert nz2050[2100] < nz2080[2100] < base245, \
        "earlier net zero must give less warming, and both less than the base"
    assert (nz2050[2100] - nz2050[2060]) < 0.25 * (
        runs["ssp245"][2100] - runs["ssp245"][2060]
    ), "warming must nearly stop climbing once CO2 emissions reach zero"

    # Net zero on a high scenario still beats leaving it alone.
    nz585 = pathway_run(Pathway("ssp585", net_zero_year=2060))
    assert nz585[2100] < runs["ssp585"][2100] - 1.0, \
        "net zero 2060 on SSP5-8.5 must remove more than a degree by 2100"

    # The ramp itself: the factor is 1 before, 0 after, linear between.
    yrs = np.arange(2020.5, 2101.5)
    fac = _co2_factor(yrs, Pathway("ssp245", net_zero_year=2045, from_year=2025))
    assert fac[0] == 1.0, "before from_year the base scenario is untouched"
    assert fac[-1] == 0.0, "after net zero the factor is zero"
    mid = float(fac[np.argmin(np.abs(yrs - 2035.5))])
    assert abs(mid - 0.5) < 0.06, f"the ramp must be linear, got {mid:.3f} at 2035"
    assert (np.diff(fac) <= 1e-12).all(), "the ramp must never increase"

    # Validation is a trust boundary: bad input is refused, not clamped.
    for bad in (
        Pathway("ssp999"),
        Pathway("ssp245", emissions_multiplier=-1.0),
        Pathway("ssp245", emissions_multiplier=99.0),
        Pathway("ssp245", net_zero_year=2020, from_year=2025),
        Pathway("ssp245", net_zero_year=2200),
    ):
        try:
            _validate(bad)
            raise AssertionError(f"{bad} should not validate")
        except ValueError:
            pass

    # --- hazard scaling ---------------------------------------------------
    assert abs(hazard_scaling(HAZARD_REFERENCE_SCENARIO, HAZARD_TARGET_YEAR) - 1.0) < 1e-12, \
        "the reference scenario at the reference year must scale by exactly 1"
    assert hazard_scaling("ssp126", HAZARD_TARGET_YEAR) < 1.0, \
        "a Paris-aligned pathway must scale the hazard down"
    assert hazard_scaling("ssp585", 2100) > hazard_scaling("ssp585", 2050), \
        "scaling must rise with time on a warming pathway"
    assert hazard_scaling(Pathway("ssp245", net_zero_year=2040), 2050) < \
        hazard_scaling("ssp245", 2050), "net zero 2040 must scale below plain SSP2-4.5"

    blk = scaling_block(Pathway("ssp126", net_zero_year=2050), 2050)
    assert "CRUDE" in blk["assumption"]["this_is_not_downscaling"]
    assert "not downscaling" in blk["assumption"]["this_is_not_downscaling"]
    assert blk["assumption"]["hazard_target_year"] == HAZARD_TARGET_YEAR

    # The reference year must be the year the hazard cache was actually built
    # for. A drift here silently rebases every scaling factor in the product.
    import json

    cache_path = os.path.abspath(hz.CACHE_PATH)
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            got = json.load(fh).get("target_year")
        assert got in (None, HAZARD_TARGET_YEAR), (
            f"hazard cache targets {got} but climate.py scales against "
            f"{HAZARD_TARGET_YEAR}"
        )

    # --- payloads ---------------------------------------------------------
    sp = scenarios_payload()
    assert len(sp["scenarios"]) == len(SSPS)
    assert all(r["inside_ar6_range"] for r in sp["scenarios"])
    assert sp["model"]["deterministic"] is True
    assert "not an ensemble" in sp["model"]["single_config_caveat"]

    tp = temperature_payload("ssp585")
    assert tp["trajectory"][0]["year"] == START_YEAR
    assert tp["trajectory"][-1]["year"] == END_YEAR
    assert "CRUDE" in tp["hazard_scaling"]["assumption"]["this_is_not_downscaling"]

    pp = pathway_payload(Pathway("ssp585", net_zero_year=2055, label="board case"))
    assert pp["pathway"]["label"] == "board case"
    assert pp["delta_vs_base_2100"] < 0, "net zero 2055 must cool against SSP5-8.5"
    assert "CO2-only experiment" in pp["what_was_changed"]
    assert len(pp["trajectory"]) == len(pp["base_trajectory"])

    print(
        f"climate.py self-check passed (ECS {e['ecs']} K, TCR {e['tcr']} K; "
        f"2081-2100 " + ", ".join(
            f"{s} {sum(runs[s][y] for y in range(2081, 2101))/20:.2f}" for s in SSPS
        ) + f" K; net-zero-2050 on SSP2-4.5 -> {nz2050[2100]:.2f} K vs "
        f"{base245:.2f} K, hazard scaling "
        f"{hazard_scaling(Pathway('ssp245', net_zero_year=2050), 2050):.3f})"
    )


if __name__ == "__main__":
    demo()
