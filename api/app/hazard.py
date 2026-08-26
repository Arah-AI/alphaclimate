"""Hazard access: cached point reads from the OS-Climate public hazard store.

The API serves from a warmed on-disk cache built by scripts/warm_cache.py. It is
real data, fetched from the real store, keyed by point, scenario and model
variant, and stamped with the source path it came from.

If the cache is missing the service reports itself degraded rather than
returning a plausible-looking zero. A silent zero is indistinguishable from
"this asset is safe", which is the worst failure this system can have.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("alphaclimate.hazard")

CACHE_PATH = os.environ.get(
    "ALPHACLIMATE_HAZARD_CACHE",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "hazard_cache.json"),
)

CITATION = (
    "OS-Climate hazard indicators, AWS Open Data Registry "
    "(registry.opendata.aws/os-climate-physrisk)"
)

_EMPTY = {
    "points": {}, "perils": [], "unpriced_perils": [], "variants": {},
    "scenarios": [], "scenario_substitution": {}, "aggregation": {},
}


@dataclass
class Reading:
    peril: str
    return_periods: list[float]
    intensities: list[float]
    units: str
    dataset: str
    path: str
    resolution: str
    variant: str = ""
    citation: str = CITATION


_STATE: dict = {"degraded": True, "reason": "cache not loaded", "source": "none"}

# Only a successful load is memoised. Caching a failure would leave the service
# permanently degraded if the cache file lands after start-up, which is exactly
# what happens during a warm run in development.
_LOADED: dict | None = None


def _cache() -> dict:
    global _STATE, _LOADED
    if _LOADED is not None:
        return _LOADED
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
        return dict(_EMPTY)

    if not data.get("points"):
        _STATE = {
            "degraded": True,
            "reason": "hazard cache contains no points",
            "source": "none",
            "cached": 0,
        }
        return dict(_EMPTY)

    _STATE = {
        "degraded": False,
        "reason": None,
        "source": f"{data.get('source_bucket', '?')}/{data.get('zarr_root', '?')}",
        "cached": len(data["points"]),
        "generated": data.get("generated"),
    }
    _LOADED = data
    return data


def PERILS_priced() -> list[str]:
    return list(_cache().get("perils", []))


def PERILS_unpriced() -> list[str]:
    return list(_cache().get("unpriced_perils", []))


# compute.py iterates this. Kept as a function-backed module attribute so the
# cache is loaded lazily rather than at import time.
class _PerilList(list):
    def __iter__(self):
        return iter(PERILS_priced())

    def __len__(self):
        return len(PERILS_priced())

    def __contains__(self, item):
        return item in PERILS_priced()


PERILS = _PerilList()


def variants(peril: str) -> list[str]:
    return list(_cache().get("variants", {}).get(peril, []))


def scenarios() -> list[dict]:
    return _cache().get("scenarios") or [{"id": "ssp585", "label": "SSP5-8.5"}]


def scenario_substitution() -> dict:
    return _cache().get("scenario_substitution", {})


def aggregation() -> dict:
    return _cache().get("aggregation", {})


def _key(lon: float, lat: float, peril: str, scenario: str, variant: str) -> str:
    return f"{lon:.4f}|{lat:.4f}|{peril}|{scenario}|{variant}"


def read(
    lon: float,
    lat: float,
    peril: str,
    scenario: str,
    variant_rank: int = 0,
) -> Reading | None:
    """Cached reading, or None. Never fabricates a value.

    `variant_rank` selects among the model variants available for the peril
    (e.g. the five riverine GCMs). Out-of-range ranks clamp to the last variant
    so a sweep never silently drops a peril.
    """
    data = _cache()
    vs = variants(peril)
    if not vs:
        return None

    # Try the requested variant, then every other one in order. Different
    # providers cover different parts of the world (IRIS is tropical cyclone
    # only, WISC is European windstorm only), so a miss on the preferred
    # variant must not drop the peril for a location another provider covers.
    start = min(variant_rank, len(vs) - 1)
    order = [vs[start]] + [v for i, v in enumerate(vs) if i != start]

    hit = None
    variant = vs[start]
    for cand in order:
        got = data["points"].get(_key(lon, lat, peril, scenario, cand))
        if got and got.get("return_periods"):
            hit, variant = got, cand
            break
    if hit is None:
        return None

    return Reading(
        peril=peril,
        return_periods=[float(x) for x in hit["return_periods"]],
        intensities=[float(x) for x in hit["intensities"]],
        units=hit.get("units", ""),
        dataset=hit.get("dataset", peril),
        path=hit.get("path", ""),
        resolution=hit.get("resolution", "unknown"),
        variant=hit.get("variant", variant),
    )


def status() -> dict:
    _cache()
    return dict(_STATE)


def demo() -> None:
    st = status()
    assert "degraded" in st and "source" in st
    if st["degraded"]:
        print(f"hazard.py self-check: DEGRADED ({st['reason']})")
        assert read(106.88, -6.10, "inundation_coastal", "ssp585") is None
        assert scenarios(), "a scenario list must always be offered"
        assert list(PERILS) == []
        return

    data = _cache()
    assert data["points"], "a healthy cache must contain points"
    assert list(PERILS), "the catalogue must name at least one priced peril"
    assert scenarios(), "scenarios must be listed"
    assert aggregation().get("method"), "the aggregation choice must be recorded"
    assert scenario_substitution(), "scenario substitution must be disclosed"

    priced = set(PERILS_priced())
    for k, v in data["points"].items():
        peril = k.split("|")[2]
        axis, inten = v.get("return_periods", []), v.get("intensities", [])
        assert len(axis) == len(inten), f"{k}: ragged index axis"
        assert all(i >= 0 for i in inten), f"{k}: negative intensity"
        assert v.get("path"), f"{k}: reading without a source path"
        assert v.get("units"), f"{k}: reading without units"
        if peril in priced:
            # Only the priced perils index on return period. Drought indexes on
            # an SPEI threshold (negative, descending) and heat on a temperature
            # threshold, so the ordering rules below do not apply to them.
            assert all(x > 0 for x in axis), f"{k}: non-positive return period"
            assert axis == sorted(axis), f"{k}: return periods must ascend"

    # A point we never cached must return None, never a guess.
    assert read(0.0, 0.0, "inundation_coastal", "ssp585") is None

    # Provider fallback: European ports have no IRIS tropical cyclone data but
    # do have WISC windstorm data, and must not silently lose the peril.
    rot = read(4.4034, 51.9244, "wind", "ssp585")
    if "wind" in list(PERILS):
        assert rot is not None, "Rotterdam must resolve wind via the WISC fallback"
        assert rot.variant == "wisc", f"expected wisc, got {rot.variant}"
        assert max(rot.intensities) > 20, "European windstorm should exceed 20 m/s"

    # Flood depth must be physically plausible where we do have a reading.
    for k, v in data["points"].items():
        if "|inundation" in k:
            assert max(v["intensities"]) < 25.0, f"{k}: implausible flood depth"

    # A high-emissions scenario must not be uniformly milder than the baseline.
    worse = 0
    for k, v in data["points"].items():
        if "|inundation_coastal|ssp585|" not in k:
            continue
        base = data["points"].get(k.replace("|ssp585|", "|historical|"))
        if base and max(v["intensities"]) >= max(base["intensities"]):
            worse += 1
    assert worse > 0, "SSP5-8.5 coastal flood should exceed baseline somewhere"

    print(
        f"hazard.py self-check passed ({st['cached']} points, "
        f"{len(list(PERILS))} priced perils, {len(PERILS_unpriced())} monitored)"
    )


if __name__ == "__main__":
    demo()
