"""Hazard raster tiles, served straight from the OS-Climate map pyramids.

The public store ships pre-rendered pyramids alongside the hazard arrays. Each
pyramid is a zarr group whose children are numbered zoom levels:

    <map_array>/1, <map_array>/2, ... <map_array>/N

Level L is an EPSG:3857 array of shape (index, 512 * 2^(L-1), 512 * 2^(L-1))
chunked at (1, 512, 512), origin -20037508.34 in both axes. So a level is
exactly 2^(L-1) chunks across, one chunk is one 512px slippy tile, and

    level = z + 1,  chunk = (x, y)

which means a tile is a single object read with no resampling and no stitching.
That is the whole trick; physrisk's own ImageCreator does the same thing
(`map_zoom=tile.z + 1`, then a 512-wide slice), which is where this came from.

The first axis is a return period or a threshold, not a scenario. Each layer
below pins one index and says so in its legend, because a map that silently
shows you the 1-in-1000 year flood while you read it as "the flood risk" is
worse than no map.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import s3fs
import zarr
from fastapi import APIRouter, HTTPException, Response
from PIL import Image

from . import portfolio

log = logging.getLogger("alphaclimate.tiles")

router = APIRouter()

BUCKET = "os-climate-physical-risk"
ROOT = f"{BUCKET}/hazard-indicators/hazard.zarr"
REGION = "us-east-1"
TILE_PX = 512

# Some arrays use float32-max rather than NaN as their fill. Both are nodata.
NODATA_ABOVE = 1e30

CITATION = (
    "OS-Climate hazard indicators, AWS Open Data Registry "
    "(registry.opendata.aws/os-climate-physrisk)"
)

# Sequential, light to dark, warm. Reads on the muted grey basemap and lands on
# the severe band colour at the top so the raster and the markers agree.
RAMP = ["#fde9c8", "#f9c98a", "#f0a05a", "#e06a3c", "#c0392b", "#7c1d17"]


@dataclass(frozen=True)
class Layer:
    """One pyramid, one pinned index, one set of class breaks."""

    label: str
    path: str
    units: str
    breaks: tuple[float, ...]   # six ascending lower bounds, one per ramp stop
    index: int                  # which slice of axis 0 to draw
    index_label: str            # what that slice means, in words
    max_zoom: int               # highest z with a pyramid level
    coverage: str
    source: str

    def level(self, z: int) -> str:
        return f"{ROOT}/{self.path}/{z + 1}"


# Verified against the store: every path resolves, every level listed exists,
# and every layer returns non-empty data somewhere in the demo portfolio's
# footprint. Coverage is stated because four of these are Europe-only and
# pretending otherwise would put a blank map in front of an Asian portfolio.
HAZARDS: dict[str, Layer] = {
    "inundation_riverine": Layer(
        label="Riverine flood depth",
        path="inundation/wri/v2/inunriver_historical_000000000WATCH_1980_map",
        units="m",
        breaks=(0.1, 0.5, 1.0, 2.0, 4.0, 8.0),
        index=8,
        index_label="1-in-1000 year return period, historical baseline",
        max_zoom=6,
        coverage="global",
        source="WRI Aqueduct Floods v2",
    ),
    "inundation_coastal": Layer(
        label="Coastal flood depth",
        path="inundation/wri/v2/inuncoast_historical_nosub_hist_0_map",
        units="m",
        breaks=(0.1, 0.5, 1.0, 2.0, 4.0, 8.0),
        index=8,
        index_label="1-in-1000 year return period, no subsidence, historical",
        max_zoom=6,
        coverage="global",
        source="WRI Aqueduct Floods v2",
    ),
    "wind": Layer(
        label="Cyclone wind speed",
        path="wind/iris/v1/max_speed_historical_2010_map",
        units="m/s",
        breaks=(20.0, 33.0, 43.0, 50.0, 58.0, 70.0),
        index=18,
        index_label="1-in-1000 year return period, historical baseline",
        max_zoom=3,
        coverage="global",
        source="IRIS tropical cyclone v1",
    ),
    "chronic_heat": Layer(
        label="Days above 30°C WBGT",
        path=(
            "maps/chronic_heat/osc/v2/"
            "days_wbgt_above_ACCESS-CM2_ssp585_2050_map"
        ),
        units="days/year",
        breaks=(1.0, 15.0, 45.0, 90.0, 180.0, 300.0),
        index=5,
        index_label="30°C wet-bulb globe temperature, SSP5-8.5 2050, ACCESS-CM2",
        max_zoom=1,
        coverage="global, coarse (two pyramid levels only)",
        source="OS-Climate heat indicators v2",
    ),
    "drought": Layer(
        label="Months in severe drought",
        path=(
            "maps/drought/osc/v2/"
            "months_spei12m_below_threshold_ACCESS-CM2_ssp585_2080_map"
        ),
        units="months/year",
        breaks=(0.5, 1.0, 2.0, 4.0, 7.0, 10.0),
        index=3,
        index_label="SPEI-12m below -2.0, SSP5-8.5 2080, ACCESS-CM2",
        max_zoom=1,
        coverage="global, coarse (two pyramid levels only)",
        source="OS-Climate drought indicators v2",
    ),
    "water_risk": Layer(
        label="Water stress",
        path="maps/water_risk/wri/v2/water_stress_historical_1999_map",
        units="withdrawal / supply",
        breaks=(0.1, 0.2, 0.4, 0.8, 1.2, 2.0),
        index=0,
        index_label="baseline water stress ratio, 1999",
        max_zoom=3,
        coverage="global",
        source="WRI Aqueduct Water Risk v2",
    ),
    "subsidence": Layer(
        label="Land subsidence rate",
        path="maps/subsidence/csm/v1/land_subsidence_rate_historical_2021_map",
        units="mm/year",
        breaks=(1.0, 3.0, 6.0, 12.0, 25.0, 50.0),
        index=0,
        index_label="observed rate to 2021",
        max_zoom=6,
        coverage="global, sparse (measured basins only)",
        source="CSM land subsidence v1",
    ),
    "fire": Layer(
        label="Wildfire probability",
        path="maps/fire/ECB_fire_risk_indicators/v1_1/historical_2010_map",
        units="annual probability",
        breaks=(0.01, 0.03, 0.07, 0.15, 0.3, 0.5),
        index=0,
        index_label="historical baseline, 2010",
        max_zoom=9,
        coverage="Europe only",
        source="ECB fire risk indicators v1.1",
    ),
    "landslide": Layer(
        label="Landslide susceptibility",
        path="maps/landslide/landslide_jrc/v1/susceptability_historical_2018_map",
        units="class",
        breaks=(0.5, 1.5, 2.5, 3.5, 4.5, 5.0),
        index=7,
        index_label="highest modelled class, 2018",
        max_zoom=8,
        coverage="Europe only",
        source="JRC ELSUS landslide susceptibility v1",
    ),
}

CACHE_SECONDS = 30 * 24 * 3600


# ------------------------------------------------------------------ store

@lru_cache(maxsize=1)
def _fs() -> s3fs.S3FileSystem:
    return s3fs.S3FileSystem(anon=True, client_kwargs={"region_name": REGION})


@lru_cache(maxsize=64)
def _array(store_path: str) -> zarr.Array:
    return zarr.open(s3fs.S3Map(store_path, s3=_fs(), check=False), mode="r")


# ------------------------------------------------------------------ pixels

def _rgba() -> np.ndarray:
    """Ramp as an (7, 4) uint8 table; row 0 is the transparent below-min class."""
    out = np.zeros((len(RAMP) + 1, 4), dtype=np.uint8)
    for i, hexcol in enumerate(RAMP, start=1):
        out[i] = (
            int(hexcol[1:3], 16),
            int(hexcol[3:5], 16),
            int(hexcol[5:7], 16),
            235,
        )
    return out


_RGBA = _rgba()


def colourise(data: np.ndarray, breaks: tuple[float, ...]) -> np.ndarray:
    """Class the values into the ramp. Nodata and sub-minimum go transparent.

    Below the first break is deliberately transparent, not pale: on a hazard
    map "too small to matter" and "nothing here" should both let the basemap
    through, and a wash of colour over every ocean pixel hides the signal.
    """
    values = np.asarray(data, dtype=np.float64)
    nodata = ~np.isfinite(values) | (np.abs(values) > NODATA_ABOVE)
    values = np.where(nodata, -np.inf, values)
    klass = np.searchsorted(np.asarray(breaks), values, side="right")
    klass[nodata] = 0
    return _RGBA[klass]


@lru_cache(maxsize=1)
def _blank() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (TILE_PX, TILE_PX), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@lru_cache(maxsize=4096)
def tile_png(hazard: str, z: int, x: int, y: int) -> bytes:
    """One tile. One chunk read. Memoised, because tiles are re-requested hard."""
    layer = HAZARDS[hazard]
    arr = _array(layer.level(z))
    y0, x0 = TILE_PX * y, TILE_PX * x
    data = arr[layer.index, y0 : y0 + TILE_PX, x0 : x0 + TILE_PX]
    if data.shape != (TILE_PX, TILE_PX):  # edge of a partial level
        return _blank()
    rgba = colourise(data, layer.breaks)
    if not rgba[..., 3].any():
        return _blank()
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------- endpoints

def _layer(hazard: str) -> Layer:
    layer = HAZARDS.get(hazard)
    if layer is None:
        raise HTTPException(404, f"No hazard layer '{hazard}'.")
    return layer


@router.get("/api/tiles")
def tile_layers() -> dict:
    """Everything the client needs to build the hazard selector."""
    return {
        "layers": [
            {
                "id": key,
                "label": layer.label,
                "units": layer.units,
                "max_zoom": layer.max_zoom,
                "coverage": layer.coverage,
                "source": layer.source,
                "index_label": layer.index_label,
            }
            for key, layer in HAZARDS.items()
        ],
        "citation": CITATION,
    }


@router.get("/api/tiles/{hazard}/legend")
def tile_legend(hazard: str) -> dict:
    """Value breaks and colours, so the UI never hardcodes a ramp."""
    layer = _layer(hazard)
    stops = [
        {
            "from": layer.breaks[i],
            "to": layer.breaks[i + 1] if i + 1 < len(layer.breaks) else None,
            "color": RAMP[i],
        }
        for i in range(len(RAMP))
    ]
    return {
        "id": hazard,
        "label": layer.label,
        "units": layer.units,
        "max_zoom": layer.max_zoom,
        "coverage": layer.coverage,
        "source": layer.source,
        "index_label": layer.index_label,
        "below_min": {"to": layer.breaks[0], "color": None},
        "stops": stops,
        "citation": CITATION,
    }


@router.get("/api/map/assets")
def map_assets() -> dict:
    """Coordinates for the portfolio. The summary carries the money, not the
    geometry, so the map joins the two on id rather than duplicating either."""
    return {
        "assets": [
            {"id": a.id, "name": a.name, "country": a.country,
             "lon": a.lon, "lat": a.lat}
            for a in portfolio.DEMO_ASSETS
        ]
    }


@router.get(
    "/api/tiles/{hazard}/{z}/{x}/{y}.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def tile(hazard: str, z: int, x: int, y: int) -> Response:
    layer = _layer(hazard)
    if z < 0 or z > layer.max_zoom:
        raise HTTPException(404, f"'{hazard}' has no pyramid level for zoom {z}.")
    span = 1 << z
    if not (0 <= x < span and 0 <= y < span):
        raise HTTPException(404, f"Tile {z}/{x}/{y} is off the world.")
    try:
        png = tile_png(hazard, z, x, y)
    except Exception as exc:  # noqa: BLE001 - the store is remote and fallible
        log.warning("tile %s/%s/%s/%s failed: %s", hazard, z, x, y, exc)
        raise HTTPException(502, "Hazard store did not answer.") from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}, immutable"},
    )


# -------------------------------------------------------------- self-check

def demo() -> None:
    """Checks the tile maths and the colour classing without touching S3."""

    def xyz(lon: float, lat: float, z: int) -> tuple[int, int]:
        n = 1 << z
        rad = math.radians(lat)
        return (
            int((lon + 180.0) / 360.0 * n),
            int((1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * n),
        )

    # Jakarta sits in a known tile; the pyramid level for it is z + 1.
    assert xyz(106.8806, -6.1045, 6) == (51, 33)
    assert HAZARDS["inundation_coastal"].level(6).endswith("_map/7")

    for key, layer in HAZARDS.items():
        assert len(layer.breaks) == len(RAMP), key
        assert list(layer.breaks) == sorted(layer.breaks), key
        assert layer.max_zoom >= 1, key
        assert layer.index >= 0, key

    breaks = HAZARDS["inundation_coastal"].breaks
    sample = np.array([[np.nan, 0.0, 0.05, 0.3, 1.5, 3.4e38, 99.0, 8.0]])
    out = colourise(sample, breaks)
    assert out.shape == (1, 8, 4)
    assert out[0, 0, 3] == 0, "NaN must be transparent"
    assert out[0, 1, 3] == 0, "zero is below the first break, transparent"
    assert out[0, 2, 3] == 0, "0.05 < 0.1, transparent"
    assert out[0, 3, 3] > 0, "0.3 is in class 1"
    assert out[0, 5, 3] == 0, "float32-max is a nodata sentinel, not a value"
    assert out[0, 6, 3] > 0 and out[0, 7, 3] > 0
    assert tuple(out[0, 6, :3]) == tuple(out[0, 7, :3]), "top class is saturating"
    assert tuple(out[0, 3, :3]) != tuple(out[0, 6, :3]), "ramp must vary"

    png = _blank()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(tile_legend("wind")["stops"]) == len(RAMP)
    print(f"tiles.py self-check passed ({len(HAZARDS)} hazard layers)")


if __name__ == "__main__":
    demo()
