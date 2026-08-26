"""Warm the hazard cache for the demo portfolio.

Reads real values from the public OS-Climate hazard store and writes
data/hazard_cache.json. Run it when the portfolio or the array catalogue
changes; the API serves from the cache so a web request never waits on S3.

    python scripts/warm_cache.py

Three decisions worth knowing about:

1. **The asset's own pixel, with a nearest-valid fallback.** An asset is a
   point, so the reading that describes it is the pixel it stands on. Where
   that pixel is nodata (the -9999 sentinel) or off-grid we step outward to the
   nearest valid pixel, at most two pixels / ~2 km away, and record how far we
   had to go. We do NOT take a neighbourhood max: physrisk's
   get_max_curves_on_grid exists to show that data is present near a location,
   not to characterise one building, and using it here handed every coastal
   asset the depth of the nearest tidal flat or permanent water body.

2. **Flood defences from FLOPROS.** The WRI inundation layers are undefended.
   FLOPROS standard-of-protection is read per asset and written into the cache
   so the engine can hold back everything more frequent than the local
   standard. Where FLOPROS has no value the asset stays undefended and is
   listed as such, rather than being given an assumed default.

3. **Documented scenario substitution.** WRI flood ships RCP scenarios while the
   heat and drought collections ship SSPs. Rather than silently relabelling, the
   substitution is written into the cache and surfaced in the provenance ledger.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from probe_hazard import read_hazard, read_sop  # noqa: E402
from app import portfolio  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "hazard_cache.json")

# The asset's own pixel first. If it is nodata, walk outward on the WRI 30
# arcsec grid step, at most two pixels (~1.9 km at the equator), and take the
# nearest valid reading. Coarser grids (IRIS 0.1 deg, OSC 0.5 deg) simply keep
# resolving to the same pixel, so one code path covers every collection.
PIXEL_DEG = 1.0 / 120.0     # 30 arcsec
SEARCH_RINGS = 2            # 2 pixels ~= 1.9 km

SCENARIOS = [
    {"id": "historical", "label": "Historical baseline"},
    {"id": "ssp126", "label": "SSP1-2.6 · Paris aligned"},
    {"id": "ssp245", "label": "SSP2-4.5 · middle of the road"},
    {"id": "ssp585", "label": "SSP5-8.5 · high emissions"},
]

# WRI publishes RCPs, not SSPs. This mapping is deliberate and disclosed.
SCENARIO_SUBSTITUTION = {
    "ssp126": {"wri": "rcp4p5", "note": "WRI ships no RCP2.6; RCP4.5 substituted"},
    "ssp245": {"wri": "rcp4p5", "note": "RCP4.5 is the closest WRI pathway"},
    "ssp585": {"wri": "rcp8p5", "note": "direct equivalent"},
}

YEAR = 2050

RIVERINE_GCMS = [
    "00000NorESM1-M",
    "0000GFDL-ESM2M",
    "0000HadGEM2-ES",
    "00IPSL-CM5A-LR",
    "MIROC-ESM-CHEM",
]

HEAT_GCM = "ACCESS-CM2"

RES = {
    "wri": "30 arcsec (~1 km)",
    "iris": "0.1 deg (~10 km)",
    "wisc": "~1 km",
    "jupiter": "~1 km",
    "osc": "0.5 deg (~55 km)",
}


def paths_for(peril: str, scenario: str) -> list[tuple[str, str, str, str]]:
    """(variant, path, units, resolution) options for a peril and scenario."""
    hist = scenario == "historical"
    wri = SCENARIO_SUBSTITUTION.get(scenario, {}).get("wri", "rcp8p5")

    if peril == "inundation_coastal":
        p = (
            "inundation/wri/v2/inuncoast_historical_nosub_hist_0"
            if hist
            else f"inundation/wri/v2/inuncoast_{wri}_wtsub_{YEAR}_0"
        )
        return [("wri-coastal", p, "m", RES["wri"])]

    if peril == "inundation_riverine":
        if hist:
            return [(
                "WATCH",
                "inundation/wri/v2/inunriver_historical_000000000WATCH_1980",
                "m", RES["wri"],
            )]
        return [
            (g.lstrip("0"), f"inundation/wri/v2/inunriver_{wri}_{g}_{YEAR}", "m", RES["wri"])
            for g in RIVERINE_GCMS
        ]

    if peril == "wind":
        if hist:
            return [
                ("iris", "wind/iris/v1/max_speed_historical_2010", "m/s", RES["iris"]),
                ("wisc", "wind/wisc/v1/max_speed_historical_1999/max_speed", "m/s", RES["wisc"]),
            ]
        sc = "ssp245" if scenario in ("ssp126", "ssp245") else "ssp585"
        return [
            ("iris", f"wind/iris/v1/max_speed_{sc}_{YEAR}", "m/s", RES["iris"]),
            ("wisc", "wind/wisc/v1/max_speed_historical_1999/max_speed", "m/s", RES["wisc"]),
        ]

    # Monitored but not monetised: no defensible damage curve exists for these.
    if peril == "chronic_heat":
        sc = "historical" if hist else scenario
        yr = 2005 if hist else YEAR
        return [(
            HEAT_GCM,
            f"chronic_heat/osc/v2/days_wbgt_above_{HEAT_GCM}_{sc}_{yr}",
            "days/year above WBGT threshold", RES["osc"],
        )]

    if peril == "drought":
        sc = "historical" if hist else scenario
        yr = 2005 if hist else YEAR
        return [(
            HEAT_GCM,
            f"drought/osc/v2/months_spei12m_below_threshold_{HEAT_GCM}_{sc}_{yr}/indicator",
            "months/year below SPEI threshold", RES["osc"],
        )]

    if peril == "fire":
        sc = "ssp126" if scenario in ("historical", "ssp126") else "ssp585"
        yr = 2020 if hist else YEAR
        return [(
            "jupiter",
            f"fire/jupiter/v1/fire_probability_{sc}_{yr}",
            "probability", RES["jupiter"],
        )]

    return []


def _offsets() -> list[tuple[float, float]]:
    """(dlon, dlat) candidates, the asset's own pixel first, then outward by
    distance. Built once; it is the same for every point."""
    r = range(-SEARCH_RINGS, SEARCH_RINGS + 1)
    cells = [(i, j) for i in r for j in r]
    cells.sort(key=lambda c: c[0] ** 2 + c[1] ** 2)
    return [(i * PIXEL_DEG, j * PIXEL_DEG) for i, j in cells]


OFFSETS = _offsets()


def point_read(lon: float, lat: float, path: str):
    """Reading at the asset's own pixel, or the nearest valid one within
    ~2 km. Returns (index_values, values, rings_out) or None."""
    for dlon, dlat in OFFSETS:
        try:
            idx, vals = read_hazard(lon + dlon, lat + dlat, path)
        except Exception:
            continue
        arr = np.array(vals, dtype=float)
        if np.all(np.isnan(arr)):
            continue  # nodata sentinel or off-grid: try the next-nearest pixel
        rings = int(round(max(abs(dlon), abs(dlat)) / PIXEL_DEG))
        return idx, np.nan_to_num(arr, nan=0.0).tolist(), rings
    return None


SOP_KIND = {"inundation_coastal": "coastal", "inundation_riverine": "riverine"}

FLOPROS_CITE = {
    k: f"FLOPROS (Scussolini et al. 2016) via inundation/flopros_{k}/v1/flood_sop"
    for k in ("coastal", "riverine")
}

PRICED = ["inundation_coastal", "inundation_riverine", "wind"]
UNPRICED = ["chronic_heat", "drought", "fire"]


def read_protection() -> tuple[dict, list[str]]:
    """FLOPROS standard of protection per asset, per flood peril.

    Returns the cache block and the list of asset/peril pairs FLOPROS has no
    value for. Those stay undefended: an unknown standard is not a default.
    """
    out: dict[str, dict] = {}
    undefended: list[str] = []
    for asset in portfolio.DEMO_ASSETS:
        for peril, kind in SOP_KIND.items():
            got = read_sop(asset.lon, asset.lat, kind)
            key = f"{asset.lon:.4f}|{asset.lat:.4f}|{peril}"
            if got is None:
                undefended.append(f"{asset.id}/{kind}")
                out[key] = {"sop_years": None, "source": FLOPROS_CITE[kind],
                            "note": "no FLOPROS value; asset treated as undefended"}
                continue
            lo, hi = got
            out[key] = {
                "sop_years": lo,
                "sop_range_years": [lo, hi],
                "basis": "lower bound of the FLOPROS min/max range",
                "source": FLOPROS_CITE[kind],
            }
            print(f"  SOP {asset.id:14s} {kind:9s} {lo:g}-{hi:g} yr", flush=True)
    return out, undefended


def main() -> None:
    points: dict[str, dict] = {}
    variants: dict[str, list[str]] = {}
    misses: list[str] = []
    t0 = time.time()

    for asset in portfolio.DEMO_ASSETS:
        for peril in PRICED + UNPRICED:
            for sc in SCENARIOS:
                for variant, path, units, res in paths_for(peril, sc["id"]):
                    got = point_read(asset.lon, asset.lat, path)
                    variants.setdefault(peril, [])
                    if variant not in variants[peril]:
                        variants[peril].append(variant)
                    key = f"{asset.lon:.4f}|{asset.lat:.4f}|{peril}|{sc['id']}|{variant}"
                    if got is None:
                        misses.append(f"{asset.id} {peril} {sc['id']} {variant}")
                        continue
                    idx, vals, rings = got
                    points[key] = {
                        "return_periods": [float(x) for x in idx],
                        "intensities": [round(float(x), 5) for x in vals],
                        "units": units,
                        "dataset": path.split("/")[0] + "/" + path.split("/")[1],
                        "path": path,
                        "resolution": res,
                        "variant": variant,
                        "pixel_offset_rings": rings,
                    }
                print(f"  {asset.id:14s} {peril:20s} {sc['id']:11s} "
                      f"{len(points):4d} cached", flush=True)

    protection, undefended = read_protection()

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_bucket": "os-climate-physical-risk",
        "zarr_root": "hazard-indicators/hazard.zarr",
        "aggregation": {
            "method": "asset pixel, nearest valid pixel on nodata",
            "pixel_degrees": PIXEL_DEG,
            "search_rings": SEARCH_RINGS,
            "search_radius_km": round(SEARCH_RINGS * PIXEL_DEG * 111.32, 2),
            "why": (
                "An asset is a point, so the pixel it stands on is the reading "
                "that describes it. Where that pixel is nodata we step outward "
                "to the nearest valid pixel and record the distance in "
                "pixel_offset_rings. A neighbourhood max (physrisk's "
                "get_max_curves_on_grid) answers a different question -- is "
                "there data near here -- and gives a port the flood depth of "
                "the nearest tidal flat."
            ),
        },
        "protection": protection,
        "scenario_substitution": SCENARIO_SUBSTITUTION,
        "target_year": YEAR,
        "perils": PRICED,
        "unpriced_perils": UNPRICED,
        "variants": variants,
        "scenarios": SCENARIOS,
        "misses": misses,
        "points": points,
    }
    with open(os.path.abspath(OUT), "w") as fh:
        json.dump(out, fh, separators=(",", ":"))

    print(f"\nwrote {len(points)} points to {OUT} in {time.time() - t0:.0f}s")
    print(f"{len(undefended)} asset/peril pairs with no FLOPROS standard "
          f"(left undefended): {', '.join(undefended) or 'none'}")
    print(f"{len(misses)} misses (off-grid or unmodelled):")
    for m in misses[:20]:
        print("  -", m)


if __name__ == "__main__":
    main()
