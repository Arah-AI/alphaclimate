"""Point lookups against the public OS-Climate physrisk hazard zarr store.

    from probe_hazard import read_hazard
    idx, vals = read_hazard(106.8806, -6.1045,
                            "inundation/wri/v2/inuncoast_historical_nosub_hist_0")

`idx` is the third-axis coordinate (return period in years, temperature/SPEI
threshold, ...) and `vals` the stored indicator values at that pixel.

Coordinate transform copied from physrisk/data/zarr_reader.py (ZarrReader.
_get_coordinates + get_curves, interpolation="floor").

`read_sop` is a separate, smaller reader for the FLOPROS standard-of-protection
groups. Those are xarray-style: the group carries `latitude` / `longitude`
coordinate arrays and a (min_max, lat, lon) `sop` array, so we snap to the
nearest coordinate index instead of going through the affine.
"""

import os
from functools import lru_cache

import numpy as np
import s3fs
import zarr

BUCKET = "os-climate-physical-risk"
ZARR_PATH = "hazard-indicators/hazard.zarr"
NAN_LEGACY = -9999.0  # nodata sentinel in older arrays
# One decompressed chunk is up to ~36 MB (9 x 1000 x 1000 f4). Caching them turns
# a repeat/nearby lookup from ~2.5 s into ~0 s, which is what makes this usable
# behind a web request.
CACHE_BYTES = int(os.environ.get("HAZARD_CACHE_BYTES", 512 * 2**20))


@lru_cache(maxsize=1)
def _root():
    store = s3fs.S3Map(
        root=f"{BUCKET}/{ZARR_PATH}", s3=s3fs.S3FileSystem(anon=True), check=False
    )
    return zarr.open(zarr.LRUStoreCache(store, max_size=CACHE_BYTES), mode="r")


@lru_cache(maxsize=256)
def _array(path: str):
    """Open one array and pre-extract its attrs. Cached: the .zarray/.zattrs
    fetch is the bulk of a cold lookup."""
    z = _root()[path]
    a = z.attrs
    mat = np.array(a["transform_mat3x3"], dtype=float).reshape(3, 3)
    crs = a.get("crs", "epsg:4326")
    dim = a.get("dimensions", ["index"])[0]
    index_values = a.get(dim + "_values") or [0]
    return z, mat, np.linalg.inv(mat), crs, list(index_values), a.get("units", "unknown")


def read_hazard(lon: float, lat: float, path: str) -> tuple[list[float], list[float]]:
    """Read the hazard curve at (lon, lat) from `path` under the zarr root.

    Returns (index_values, values). Values are NaN if the point is off-grid.
    """
    z, mat, inv, crs, index_values, _ = _array(path)

    x, y = lon, lat
    if crs.lower().replace("+init=", "") != "epsg:4326":
        from pyproj import Transformer  # only needed for the projected datasets

        x, y = Transformer.from_crs(
            "epsg:4326", crs, always_xy=True
        ).transform(lon, lat)
    elif (mat @ [z.shape[2], z.shape[1], 1.0])[0] > 180 and (mat @ [0, 0, 1.0])[0] >= 0:
        x = lon % 360.0  # legacy [0, 360] longitude convention

    col, row = (inv @ [x, y, 1.0])[:2]
    if not (-0.5 <= col < z.shape[2] and -0.5 <= row < z.shape[1]):
        return index_values, [float("nan")] * len(index_values)

    ix, iy = int(np.floor(col)) % z.shape[2], int(np.floor(row))
    data = np.asarray(z[:, iy, ix], dtype=float)
    data[data == NAN_LEGACY] = np.nan
    return index_values, data.tolist()


def units(path: str) -> str:
    return _array(path)[5]


# --------------------------------------------------------------------------
# FLOPROS standard of protection
# --------------------------------------------------------------------------

SOP_GROUPS = {
    "coastal": "inundation/flopros_coastal/v1/flood_sop",
    "riverine": "inundation/flopros_riverine/v1/flood_sop",
}


@lru_cache(maxsize=4)
def _sop_group(kind: str):
    """(sop array, latitude, longitude). The coordinate arrays are one chunk
    each (~170 kB); the sop chunks are 16 MB, so we fetch coords once and reuse
    them for every point."""
    g = _root()[SOP_GROUPS[kind]]
    return g["sop"], np.asarray(g["latitude"]), np.asarray(g["longitude"])


def read_sop(lon: float, lat: float, kind: str) -> tuple[float, float] | None:
    """FLOPROS standard of protection at (lon, lat), in return-period years.

    Returns (min, max) of the FLOPROS range, or None where FLOPROS has no
    value for the location (off-grid, or an unprotected/unassessed cell).
    A missing value means "we do not know", never "protected to 0".
    """
    z, lats, lons = _sop_group(kind)
    iy = int(np.abs(lats - lat).argmin())
    ix = int(np.abs(lons - lon).argmin())
    # Guard against snapping across the whole globe when the point is off-grid.
    if abs(lats[iy] - lat) > 1.0 or abs(lons[ix] - lon) > 1.0:
        return None
    vals = np.asarray(z[:, iy, ix], dtype=float)
    vals[vals == NAN_LEGACY] = np.nan
    if np.all(np.isnan(vals)) or np.nanmax(vals) <= 0:
        return None
    lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    return lo, hi


def _demo():
    # Jakarta port, historical coastal flood: shallow but non-zero, rising with
    # return period, and plausibly under 10 m.
    idx, vals = read_hazard(
        106.8806, -6.1045, "inundation/wri/v2/inuncoast_historical_nosub_hist_0"
    )
    assert idx == [2, 5, 10, 25, 50, 100, 250, 500, 1000], idx
    assert max(vals) > 0.0, vals
    assert 0.0 <= min(vals) and max(vals) < 10.0, vals
    assert vals == sorted(vals), vals  # depth is monotone in return period

    # Off-grid (mid-Pacific for the Europe-only tudelft grid) must be NaN, not 0.
    _, vals = read_hazard(
        -140.0, 0.0, "inundation/river_tudelft/v2/flood_depth_historical_1985"
    )
    assert all(np.isnan(v) for v in vals), vals

    # FLOPROS: the Dutch delta is the most defended coast on earth and must come
    # back with a very high standard; the value is a return period in years.
    nl = read_sop(4.4034, 51.9244, "coastal")
    assert nl is not None, "Rotterdam must have a FLOPROS coastal SOP"
    assert nl[0] <= nl[1], nl
    assert 100.0 <= nl[1] <= 100_000.0, f"implausible Dutch coastal SOP: {nl}"

    # Mid-ocean has no protection standard, and that must read as unknown
    # (None), not as zero protection.
    assert read_sop(-140.0, 0.0, "coastal") is None

    riv = read_sop(4.4034, 51.9244, "riverine")
    assert riv is None or 1.0 <= riv[1] <= 100_000.0, riv
    print("ok")


if __name__ == "__main__":
    _demo()
