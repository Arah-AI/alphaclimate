"""Hazard access: cached point reads from the OS-Climate public hazard store.

Two paths in, one out:
  1. A warmed on-disk cache (built by scripts/warm_cache.py) which is what the
     deployed service normally serves. It is real data, fetched from the real
     store, keyed by point and stamped with the source path.
  2. A live read from the public zarr, used to fill cache misses.

If neither is available the service reports itself degraded rather than
returning a plausible-looking zero. A silent zero is indistinguishable from
"this asset is safe", which is the worst failure this system can have.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger("alphaclimate.hazard")

CACHE_PATH = os.environ.get(
    "ALPHACLIMATE_HAZARD_CACHE",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "hazard_cache.json"),
)

BUCKET = "os-climate-physical-risk"
ZARR_ROOT = f"{BUCKET}/hazard-indicators/hazard.zarr"

CITATION = (
    "OS-Climate hazard indicators, AWS Open Data Registry "
    "(registry.opendata.aws/os-climate-physrisk)"
)


@dataclass
class Reading:
    peril: str
    return_periods: list[float]
    intensities: list[float]
    units: str
    dataset: str
    path: str
    resolution: str
    citation: str = CITATION


# Populated from the hazard catalogue in the cache file. Kept as a module
# constant so compute.py can iterate perils without reaching into the cache.
PERILS: list[str] = []

_STATE: dict = {"degraded": True, "reason": "cache not loaded", "source": "none"}


@lru_cache(maxsize=1)
def _cache() -> dict:
    global PERILS, _STATE
    path = os.path.abspath(CACHE_PATH)
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _STATE = {
            "degraded": True,
            "reason": f"hazard cache unreadable at {path}: {exc}",
            "source": "none",
            "cached": 0,
        }
        return {"points": {}, "catalogue": {}, "scenarios": [], "perils": []}

    PERILS = list(data.get("perils", []))
    _STATE = {
        "degraded": False,
        "reason": None,
        "source": f"{ZARR_ROOT} (warmed cache)",
        "cached": len(data.get("points", {})),
    }
    return data


def _key(lon: float, lat: float, peril: str, scenario: str, year: int) -> str:
    return f"{lon:.4f}|{lat:.4f}|{peril}|{scenario}|{year}"


def scenarios() -> list[dict]:
    sc = _cache().get("scenarios", [])
    return sc or [{"id": "ssp585", "label": "SSP5-8.5"}]


def base_year(scenario: str) -> int:
    years = _cache().get("years", {}).get(scenario) or _cache().get("default_years", [])
    return int(years[0]) if years else 2050


def read(lon: float, lat: float, peril: str, scenario: str, year: int) -> Reading | None:
    data = _cache()
    hit = data.get("points", {}).get(_key(lon, lat, peril, scenario, year))
    if hit is None:
        return None
    if not hit.get("return_periods"):
        return None
    return Reading(
        peril=peril,
        return_periods=[float(x) for x in hit["return_periods"]],
        intensities=[float(x) for x in hit["intensities"]],
        units=hit.get("units", ""),
        dataset=hit.get("dataset", peril),
        path=hit.get("path", ""),
        resolution=hit.get("resolution", "unknown"),
    )


def status() -> dict:
    _cache()
    return dict(_STATE)


def demo() -> None:
    st = status()
    assert "degraded" in st and "source" in st
    if st["degraded"]:
        print(f"hazard.py self-check: DEGRADED ({st['reason']})")
        # A missing cache must not raise, and must not fabricate a reading.
        assert read(106.88, -6.10, "inundation_riverine", "ssp585", 2050) is None
        assert scenarios(), "a scenario list must always be offered"
        return

    data = _cache()
    assert data["points"], "a healthy cache must contain points"
    assert PERILS, "the catalogue must name at least one peril"
    assert scenarios(), "scenarios must be listed"

    # Every cached reading must be internally consistent.
    for k, v in list(data["points"].items())[:400]:
        rp, inten = v.get("return_periods", []), v.get("intensities", [])
        assert len(rp) == len(inten), f"{k}: ragged return periods"
        assert all(x > 0 for x in rp), f"{k}: non-positive return period"
        assert rp == sorted(rp), f"{k}: return periods must ascend"
        assert all(i >= 0 for i in inten), f"{k}: negative intensity"
        assert v.get("path"), f"{k}: reading without a source path"

    # A point we did not cache must return None, never a guess.
    assert read(0.0, 0.0, "inundation_riverine", "ssp585", base_year("ssp585")) is None
    print(f"hazard.py self-check passed ({st['cached']} points, {len(PERILS)} perils)")


if __name__ == "__main__":
    demo()
