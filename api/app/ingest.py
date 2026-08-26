"""Portfolio ingest: turn somebody else's exposure file into assets we can price.

A fixed twelve-asset demo is a demo. This module is the difference between that
and a product: an insurer, a bank or a corporate uploads its own schedule of
values and gets the same engine pointed at its own sites.

Four decisions worth knowing about:

1. **OED, not a new schema.** The insurance market already has a location-file
   standard: Oasis Open Exposure Data. Its field names are read from the
   published spec (OasisLMF/ODS_OpenExposureData, OpenExposureData/
   OEDInputFields.csv) rather than guessed, and its occupancy code ranges from
   OccupancyValues.csv. A simple generic CSV is accepted too, because most
   corporates do not hold OED.

2. **Never invent a balance sheet.** OED carries insured values; it carries no
   revenue, no debt and no debt service. Those stay at zero and the asset is
   marked `has_financials: false`, so the finance layer reports "not supplied"
   rather than a plausible-looking number nobody can trace. The one
   substitution we do make (BITIV read as annual revenue) is named on the row
   that uses it, in the same spirit as the scenario substitution in
   scripts/warm_cache.py.

3. **Never silently drop a row.** Every input row comes back in the report as
   accepted, accepted-with-warnings, or rejected with a reason. A row that
   vanishes between the customer's spreadsheet and our portfolio total is the
   worst kind of error: the numbers stay internally consistent while describing
   a smaller company than the one that uploaded them.

4. **Cold hazard reads, warmed in the background.** An uploaded site is not in
   data/hazard_cache.json, so its hazard has to come live from the public
   zarr store at roughly 2-6 s for the first read of each array. That is a job,
   not a request: upload returns immediately with a portfolio id, and the
   client polls. The reads reuse scripts/warm_cache.point_read, which takes the
   asset's own pixel with a nearest-valid fallback. It is deliberately not a
   neighbourhood max: that bug handed every coastal asset the flood depth of
   the nearest tidal flat, and it is fixed, so we do not reintroduce it here.

Results are written per portfolio under data/portfolios/ in exactly the key
format data/hazard_cache.json uses, so wiring an uploaded portfolio into the
compute path is a dict update rather than a second code path. The portfolio id
is the SHA-256 of the uploaded bytes, so re-uploading the same file is instant.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

try:
    from . import curves as curve_lib
    from .portfolio import Asset, DEMO_ASSETS
except ImportError:  # run directly: python api/app/ingest.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import curves as curve_lib  # type: ignore[no-redef]
    from app.portfolio import Asset, DEMO_ASSETS  # type: ignore[no-redef]

# --------------------------------------------------------------------------
# live hazard access
# --------------------------------------------------------------------------
#
# scripts/warm_cache.py already holds the pixel-selection logic, the peril /
# scenario / model-variant catalogue and the FLOPROS reader. Importing it beats
# copying it: one place to fix when the catalogue moves. It lives outside the
# package, so the import is guarded and its absence degrades the service
# loudly rather than silently producing zeros.

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

try:  # pragma: no cover - exercised by whether the deploy image ships scripts/
    import warm_cache as _wc
    from probe_hazard import read_sop as _read_sop
    _LIVE_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - any import failure means no live reads
    _wc = None
    _read_sop = None
    _LIVE_ERROR = f"live hazard reads unavailable: {exc!r}"

STORE = os.environ.get(
    "ALPHACLIMATE_PORTFOLIOS", os.path.join(_ROOT, "data", "portfolios")
)

MAX_UPLOAD_BYTES = 8 * 2**20   # a location file, not a data lake
MAX_ROWS = 2000                # cold reads are ~40 per asset; be honest about the ceiling
WARM_WORKERS = 8               # concurrent S3 reads per asset; latency-bound, not CPU-bound

CURRENCY_DEFAULT = "USD"

FINANCIALS_NOTE = (
    "Financials are taken from the uploaded file. Rows that carry none are "
    "left at zero and flagged, never estimated."
)


# --------------------------------------------------------------------------
# OED location schema
# --------------------------------------------------------------------------
#
# Field names verified against the published spec, not from memory:
#   https://raw.githubusercontent.com/OasisLMF/ODS_OpenExposureData/main/
#   OpenExposureData/OEDInputFields.csv        (564 fields, 243 on the Loc file)
# Status letters below are that file's "Property field status" column.

OED_SPEC = (
    "OasisLMF/ODS_OpenExposureData, OpenExposureData/OEDInputFields.csv "
    "and OccupancyValues.csv"
)

# (field, status, what we do with it)
OED_LOC_FIELDS = {
    "PortNumber":       ("R", "portfolio number, used as the portfolio name if present"),
    "AccNumber":        ("R", "account number, kept in the asset id"),
    "LocNumber":        ("R", "location number, the asset id"),
    "LocName":          ("O", "asset name"),
    "CountryCode":      ("R", "ISO 3166-1 alpha-2, reported as-is"),
    "Latitude":         ("O", "decimal degrees, [-90, 90]"),
    "Longitude":        ("O", "decimal degrees, [-180, 180]"),
    "StreetAddress":    ("O", "fallback name"),
    "City":             ("O", "fallback name"),
    "PostalCode":       ("O", "reported only"),
    "AreaCode":         ("O", "reported only"),
    "OccupancyCode":    ("O", "int, mapped to a damage-curve occupancy class"),
    "ConstructionCode": ("O", "int, reported only; no construction-specific curves held"),
    "YearBuilt":        ("O", "reported only"),
    "NumberOfStoreys":  ("O", "reported only"),
    "FloorArea":        ("O", "reported only"),
    "LocPerilsCovered": ("R", "reported only; we price what the hazard store covers"),
    "LocCurrency":      ("R", "portfolio currency"),
    "BuildingTIV":      ("O", "summed into asset value"),
    "OtherTIV":         ("O", "summed into asset value"),
    "ContentsTIV":      ("O", "summed into asset value"),
    "BITIV":            ("O", "annualised BI value, read as annual revenue (disclosed)"),
}

# Any of these present means the file is OED rather than a generic CSV.
OED_MARKERS = ("locnumber", "buildingtiv", "occupancycode", "locperilscovered")

GENERIC_FIELDS = {
    "name": ("name", "asset name"),
    "lat": ("latitude", "decimal degrees"),
    "lon": ("longitude", "decimal degrees"),
    "value": ("value", "asset value in the portfolio currency"),
    "sector": ("sector", "free text, mapped to a damage-curve occupancy class"),
    "country": ("country", "free text, reported as-is"),
    "id": ("id", "optional; derived from the name when absent"),
    "annual_revenue": ("annual_revenue", "optional"),
    "debt": ("debt", "optional"),
    "annual_debt_service": ("annual_debt_service", "optional"),
}

# Column aliases for the generic CSV. Deliberately short: a file that needs
# more aliases than this should be exported as OED.
GENERIC_ALIASES = {
    "name": ("name", "asset", "asset_name", "site", "site_name"),
    "lat": ("lat", "latitude", "y"),
    "lon": ("lon", "long", "lng", "longitude", "x"),
    "value": ("value", "tiv", "asset_value", "insured_value", "replacement_cost"),
    "sector": ("sector", "occupancy", "industry", "use", "asset_type"),
    "country": ("country", "country_name", "countrycode", "country_code"),
    "id": ("id", "asset_id", "ref", "reference"),
    "annual_revenue": ("annual_revenue", "revenue", "turnover"),
    "debt": ("debt", "total_debt", "borrowings"),
    "annual_debt_service": ("annual_debt_service", "debt_service"),
}


# --------------------------------------------------------------------------
# occupancy: OED code -> the classes the curve library actually holds
# --------------------------------------------------------------------------
#
# The right-hand side is not free text. data/damage_curves.json holds flood and
# wind curves for exactly these occupancy strings:
#
#   Agriculture, Commercial, Education, Generic, Government, Industrial,
#   Infrastructure, PowerGeneratingAsset, Religion, Residential,
#   Single Family, Transport, Unknown
#
# Anything else resolves to no curve and the asset silently stops being priced,
# which is why the mapping is explicit and the unmapped case is a warning.

CURVE_OCCUPANCIES = (
    "Agriculture", "Commercial", "Education", "Generic", "Government",
    "Industrial", "Infrastructure", "PowerGeneratingAsset", "Religion",
    "Residential", "Single Family", "Transport", "Unknown",
)

# (low, high, occupancy, OED broad category as published)
OED_OCCUPANCY_RANGES: tuple[tuple[int, int, str, str], ...] = (
    (1000, 1000, "Unknown",        "Unknown"),
    (1050, 1099, "Residential",    "Residential"),
    (1100, 1149, "Commercial",     "Commercial"),
    (1150, 1199, "Industrial",     "Industrial"),
    (1200, 1209, "Religion",       "Religion and Nonprofit"),
    (1210, 1229, "Government",     "Government"),
    (1230, 1249, "Education",      "Education"),
    (1250, 1299, "Transport",      "Transportation"),
    (1300, 1350, "Infrastructure", "Utilities / flood control"),
    (1351, 1399, "Agriculture",    "Agriculture, forestry, greenhouse"),
    (1400, 1449, "Unknown",        "Marine Cargo"),
    (2000, 2000, "Unknown",        "IFM Unknown"),
    (2050, 2499, "Industrial",     "IFM fabrication, chemical, metal, tech, mining, refinery"),
    (2500, 2599, "Infrastructure", "IFM electric and water systems"),
    (2600, 2649, "Industrial",     "IFM gas processing"),
    (2650, 2699, "Infrastructure", "IFM communications"),
    (2700, 2749, "Agriculture",    "IFM agriculture"),
    (2750, 2799, "Transport",      "IFM transportation"),
    (3000, 3999, "Unknown",        "Offshore"),
)

# Codes that mean "we were told nothing useful". Mapped, but warned about,
# because the only Unknown flood curve in the library is a North American one.
OED_VAGUE_CODES = ((1000, 1000), (1400, 1449), (2000, 2000), (3000, 3999))

# Generic CSV sector text -> occupancy class. First keyword found wins, so the
# order matters: "power plant" must not be caught by "plant".
SECTOR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("power", "Infrastructure"), ("energy", "Infrastructure"),
    ("utility", "Infrastructure"), ("utilities", "Infrastructure"),
    ("grid", "Infrastructure"), ("pipeline", "Infrastructure"),
    ("telecom", "Infrastructure"), ("water", "Infrastructure"),
    ("infrastructure", "Infrastructure"),
    ("port", "Transport"), ("transport", "Transport"), ("rail", "Transport"),
    ("airport", "Transport"), ("shipping", "Transport"), ("terminal", "Transport"),
    ("refin", "Industrial"), ("chemical", "Industrial"), ("petrochem", "Industrial"),
    ("steel", "Industrial"), ("cement", "Industrial"), ("mining", "Industrial"),
    ("manufactur", "Industrial"), ("industr", "Industrial"), ("factory", "Industrial"),
    ("plant", "Industrial"), ("logistic", "Industrial"), ("warehouse", "Industrial"),
    ("distribution", "Industrial"), ("processing", "Industrial"),
    ("agricultur", "Agriculture"), ("farm", "Agriculture"),
    ("plantation", "Agriculture"), ("forestry", "Agriculture"),
    ("school", "Education"), ("universit", "Education"), ("education", "Education"),
    ("government", "Government"), ("municipal", "Government"),
    ("church", "Religion"), ("temple", "Religion"), ("mosque", "Religion"),
    ("residential", "Residential"), ("housing", "Residential"),
    ("apartment", "Residential"), ("dwelling", "Residential"),
    ("real estate", "Commercial"), ("office", "Commercial"), ("retail", "Commercial"),
    ("hotel", "Commercial"), ("mall", "Commercial"), ("commercial", "Commercial"),
)


def occupancy_for_oed(code: int) -> tuple[str, str, bool]:
    """(occupancy, OED broad category, is_vague) for an OED occupancy code."""
    for lo, hi, occ, cat in OED_OCCUPANCY_RANGES:
        if lo <= code <= hi:
            vague = any(a <= code <= b for a, b in OED_VAGUE_CODES)
            return occ, cat, vague
    return "Unknown", f"code {code} is outside every published OED range", True


def occupancy_for_sector(sector: str) -> tuple[str, bool]:
    """(occupancy, matched) for free-text sector from a generic CSV."""
    s = (sector or "").strip().lower()
    for kw, occ in SECTOR_KEYWORDS:
        if kw in s:
            return occ, True
    return "Unknown", False


# --------------------------------------------------------------------------
# region: continent from coordinates
# --------------------------------------------------------------------------
#
# The region selects the JRC continental damage function, so it has to be
# right for the big landmasses and it does not have to be right for the Aleutian
# Islands.
#
# ponytail: ordered bounding boxes, first hit wins. Wrong for a handful of edge
# cases (Papua New Guinea reads as Asia, north-west Morocco as Europe, Turkey as
# Europe). Upgrade path is a real point-in-polygon test against Natural Earth
# continents, worth doing the day a customer uploads a portfolio that straddles
# one of those boundaries.

# (lon_min, lon_max, lat_min, lat_max, region)
CONTINENT_BOXES: tuple[tuple[float, float, float, float, str], ...] = (
    (110.0, 180.0, -50.0, -9.0, "Oceania"),        # Australia, NZ, Melanesia
    (-180.0, -130.0, -50.0, 0.0, "Oceania"),       # south Pacific
    (-32.0, 45.0, 35.5, 82.0, "Europe"),
    (-20.0, 52.0, -36.0, 37.5, "Africa"),
    (25.0, 180.0, -12.0, 82.0, "Asia"),
    (-95.0, -30.0, -60.0, 14.0, "South America"),
    (-180.0, -50.0, 5.0, 85.0, "North America"),
)

REGION_FALLBACK = "Global"


def region_for(lon: float, lat: float) -> str:
    """Continent for the JRC regional curve, or 'Global' where no box matches.

    'Global' is a real option in the curve library, so an unclassifiable point
    gets the global curve rather than a guessed continent.
    """
    for lo_lon, hi_lon, lo_lat, hi_lat, region in CONTINENT_BOXES:
        if lo_lon <= lon <= hi_lon and lo_lat <= lat <= hi_lat:
            return region
    return REGION_FALLBACK


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

@dataclass
class RowReport:
    """One input row's fate. Every row gets one of these; none are dropped."""
    row: int                          # 1-based data row, header excluded
    status: str                       # accepted | warning | rejected
    id: str = ""
    name: str = ""
    reason: str = ""                  # populated only when rejected
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Parsed:
    source_format: str                # oed | generic
    name: str
    currency: str
    assets: list[dict]                # Asset.as_dict() plus provenance keys
    rows: list[RowReport]

    @property
    def counts(self) -> dict:
        return {
            "total": len(self.rows),
            "accepted": sum(1 for r in self.rows if r.status == "accepted"),
            "warning": sum(1 for r in self.rows if r.status == "warning"),
            "rejected": sum(1 for r in self.rows if r.status == "rejected"),
        }


def _num(raw: str | None) -> float | None:
    """Money and coordinates as written by spreadsheets: 1,234.5 / (100) / $12."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "").replace("_", "")
    if not s or s.lower() in ("na", "n/a", "null", "none", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _read_csv(blob: bytes) -> tuple[list[str], list[dict]]:
    text = blob.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel                     # a single-column file is still valid
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    return headers, rows


def _picker(headers: list[str]):
    """Case-insensitive column getter. OED is CamelCase, real files are not."""
    lookup = {h.lower().replace(" ", "").replace("_", ""): h for h in headers}

    def get(row: dict, *names: str) -> str | None:
        for n in names:
            key = lookup.get(n.lower().replace(" ", "").replace("_", ""))
            if key is not None:
                v = row.get(key)
                if v is not None and str(v).strip() != "":
                    return str(v).strip()
        return None

    return get, lookup


def detect_format(headers: list[str]) -> str:
    flat = {h.lower().replace(" ", "").replace("_", "") for h in headers}
    return "oed" if flat & set(OED_MARKERS) else "generic"


def parse(blob: bytes, filename: str = "upload.csv") -> Parsed:
    """Parse an uploaded file into assets plus a per-row report.

    Raises HTTPException for a file that is not usable at all. Anything a
    single row got wrong is reported on that row, never raised.
    """
    if not blob.strip():
        raise HTTPException(400, "The uploaded file is empty.")
    headers, raw_rows = _read_csv(blob)
    if not headers:
        raise HTTPException(400, "No header row found. CSV or OED CSV expected.")
    if not raw_rows:
        raise HTTPException(400, "The file has a header but no data rows.")
    if len(raw_rows) > MAX_ROWS:
        raise HTTPException(
            413,
            f"{len(raw_rows)} rows; the ingest ceiling is {MAX_ROWS}. Each asset "
            f"needs about 40 live hazard reads, so a larger file needs a batch "
            f"run rather than an upload.",
        )

    fmt = detect_format(headers)
    get, _ = _picker(headers)

    if fmt == "generic":
        # Header presence, not first-row value: an empty first cell is a row
        # problem, not a file problem.
        flat = {h.lower().replace(" ", "").replace("_", "") for h in headers}
        missing = [k for k in ("lat", "lon")
                   if not flat & {a.replace("_", "") for a in GENERIC_ALIASES[k]}]
        if missing:
            raise HTTPException(
                400,
                f"Not an OED location file, and the generic CSV columns "
                f"{', '.join(missing)} are missing. Expected either OED "
                f"(LocNumber, CountryCode, Latitude, Longitude, ...) or a CSV "
                f"with name, lat, lon, value, sector, country.",
            )

    assets: list[dict] = []
    reports: list[RowReport] = []
    seen_ids: dict[str, int] = {}

    for i, raw in enumerate(raw_rows, start=1):
        if not any(str(v or "").strip() for v in raw.values()):
            reports.append(RowReport(i, "rejected", reason="blank row"))
            continue
        rep, asset = (_map_oed if fmt == "oed" else _map_generic)(i, raw, get)
        reports.append(rep)
        if asset is None:
            continue

        # Duplicate ids get suffixed rather than dropped: two rows are two
        # buildings until the customer says otherwise.
        base = asset["id"]
        if base in seen_ids:
            seen_ids[base] += 1
            asset["id"] = f"{base}-{seen_ids[base]}"
            rep.warnings.append(
                f"duplicate id '{base}'; kept as '{asset['id']}' rather than dropped"
            )
        else:
            seen_ids[base] = 1
        rep.id = asset["id"]

        # A row whose (region, occupancy) has no vulnerability function will
        # read as zero loss. Say so now, not by omission later.
        priced = {
            p: curve_lib.best(p, asset["region"], asset["occupancy"])
            for p in ("inundation_riverine", "inundation_coastal", "wind")
        }
        unpriced = [p for p, c in priced.items() if c is None]
        if unpriced:
            rep.warnings.append(
                f"no damage curve for {asset['region']}/{asset['occupancy']} for "
                f"{', '.join(unpriced)}; those perils will not be priced"
            )
        used = next((c for c in priced.values() if c), None)
        if used and used.get("region") not in (asset["region"], "Global", "Generic"):
            rep.warnings.append(
                f"nearest available curve is {used['id']} ({used.get('region')}), "
                f"applied outside its region"
            )
        asset["curves"] = {p: (c["id"] if c else None) for p, c in priced.items()}

        if rep.warnings and rep.status == "accepted":
            rep.status = "warning"
        asset["warnings"] = list(rep.warnings)
        assets.append(asset)

    name = get(raw_rows[0], "PortNumber") or os.path.basename(filename)
    currency = (get(raw_rows[0], "LocCurrency") or CURRENCY_DEFAULT).upper()
    return Parsed(fmt, str(name), currency, assets, reports)


def _coords(rep: RowReport, lat: float | None, lon: float | None) -> bool:
    """Coordinate validation. Returns True if the row can be kept."""
    if lat is None or lon is None:
        rep.status = "rejected"
        rep.reason = "missing or non-numeric latitude/longitude"
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        rep.status = "rejected"
        rep.reason = (
            f"coordinates out of range (lat {lat}, lon {lon}); "
            f"latitude must be [-90, 90] and longitude [-180, 180]"
        )
        return False
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        rep.status = "rejected"
        rep.reason = "coordinates are 0,0 (Null Island): an unset value, not a site"
        return False
    return True


def _financials(rep: RowReport, value: float | None, revenue: float,
                debt: float, service: float, sources: dict) -> tuple[float, bool]:
    """Value plus a has_financials flag. Absent means zero and flagged."""
    if value is None or value <= 0:
        rep.warnings.append(
            "no asset value supplied; loss cannot be expressed in money for this row"
        )
        value = 0.0
    has = bool(revenue > 0 and debt > 0 and service > 0)
    if not has:
        absent = [k for k, v in (("annual_revenue", revenue), ("debt", debt),
                                 ("annual_debt_service", service)) if v <= 0]
        rep.warnings.append(
            f"no financial data for {', '.join(absent)}; left at zero, not estimated"
        )
    sources.setdefault("value", "supplied" if value > 0 else "absent")
    return value, has


def _map_oed(i: int, raw: dict, get) -> tuple[RowReport, dict | None]:
    loc = get(raw, "LocNumber")
    acc = get(raw, "AccNumber")
    name = (get(raw, "LocName") or get(raw, "StreetAddress")
            or get(raw, "City") or (f"Location {loc}" if loc else ""))
    rep = RowReport(i, "accepted", id=str(loc or ""), name=name)

    if not loc:
        rep.status = "rejected"
        rep.reason = "LocNumber is required by OED and is missing"
        return rep, None

    raw_lat, raw_lon = get(raw, "Latitude"), get(raw, "Longitude")
    lat, lon = _num(raw_lat), _num(raw_lon)
    if raw_lat is None and raw_lon is None:
        # OED makes Latitude/Longitude optional because the market geocodes
        # from the address. We do not geocode, so the row cannot be located.
        rep.status = "rejected"
        rep.reason = (
            "no Latitude/Longitude; address geocoding is not supported, so "
            "the site cannot be located"
        )
        return rep, None
    if not _coords(rep, lat, lon):
        return rep, None
    assert lat is not None and lon is not None

    country = (get(raw, "CountryCode") or "").upper()
    if not country:
        rep.warnings.append("CountryCode is required by OED and is missing")

    sources: dict[str, str] = {}
    code_raw = get(raw, "OccupancyCode")
    code = _num(code_raw)
    if code is None:
        # OED's published default for OccupancyCode is 1000 (Unknown). Using
        # the standard's own default is not an invention; saying nothing is.
        occ, cat, vague = "Unknown", "Unknown (OED default 1000)", True
        rep.warnings.append("no OccupancyCode; OED default 1000 (Unknown) applied")
    else:
        occ, cat, vague = occupancy_for_oed(int(code))
        if vague:
            rep.warnings.append(
                f"OccupancyCode {int(code)} ({cat}) carries no building "
                f"vulnerability class; mapped to Unknown"
            )
    sources["occupancy"] = f"OccupancyCode {code_raw or '1000 (default)'} -> {cat}"

    bldg = _num(get(raw, "BuildingTIV")) or 0.0
    other = _num(get(raw, "OtherTIV")) or 0.0
    contents = _num(get(raw, "ContentsTIV")) or 0.0
    bi = _num(get(raw, "BITIV")) or 0.0
    value = bldg + other + contents
    if bi > 0:
        rep.warnings.append(
            "annual revenue taken from BITIV (OED annualised business-"
            "interruption value); OED carries no revenue field"
        )
        sources["annual_revenue"] = "BITIV, substituted and disclosed"
    value, has_fin = _financials(rep, value or None, bi, 0.0, 0.0, sources)
    sources["value"] = "BuildingTIV + OtherTIV + ContentsTIV"

    region = region_for(lon, lat)
    asset = Asset(
        id=f"{acc}-{loc}" if acc else str(loc),
        name=name or str(loc),
        country=country,
        lon=lon, lat=lat,
        sector=cat,
        occupancy=occ,
        region=region,
        value=value,
        annual_revenue=bi,
        debt=0.0,
        annual_debt_service=0.0,
    ).as_dict()
    asset.update(
        has_financials=has_fin,
        field_sources=sources,
        oed={
            k: get(raw, k)
            for k in ("ConstructionCode", "YearBuilt", "NumberOfStoreys",
                      "FloorArea", "LocPerilsCovered", "PostalCode", "AreaCode")
            if get(raw, k)
        },
    )
    rep.name = asset["name"]
    return rep, asset


def _slug(text: str, fallback: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:48] or fallback


def _map_generic(i: int, raw: dict, get) -> tuple[RowReport, dict | None]:
    a = GENERIC_ALIASES
    name = get(raw, *a["name"]) or ""
    rep = RowReport(i, "accepted", name=name)

    lat, lon = _num(get(raw, *a["lat"])), _num(get(raw, *a["lon"]))
    if not _coords(rep, lat, lon):
        return rep, None
    assert lat is not None and lon is not None

    if not name:
        rep.warnings.append("no name column value; named from its coordinates")
        name = f"Site {lat:.4f},{lon:.4f}"

    sector = get(raw, *a["sector"]) or ""
    occ, matched = occupancy_for_sector(sector)
    if not matched:
        rep.warnings.append(
            f"sector {sector!r} maps to no damage-curve occupancy class; "
            f"mapped to Unknown" if sector
            else "no sector supplied; occupancy mapped to Unknown"
        )

    sources = {"occupancy": f"sector {sector!r} -> {occ}"}
    revenue = _num(get(raw, *a["annual_revenue"])) or 0.0
    debt = _num(get(raw, *a["debt"])) or 0.0
    service = _num(get(raw, *a["annual_debt_service"])) or 0.0
    value, has_fin = _financials(rep, _num(get(raw, *a["value"])), revenue,
                                 debt, service, sources)

    asset = Asset(
        id=get(raw, *a["id"]) or _slug(name, f"row-{i}"),
        name=name,
        country=get(raw, *a["country"]) or "",
        lon=lon, lat=lat,
        sector=sector or "unspecified",
        occupancy=occ,
        region=region_for(lon, lat),
        value=value,
        annual_revenue=revenue,
        debt=debt,
        annual_debt_service=service,
    ).as_dict()
    asset.update(has_financials=has_fin, field_sources=sources, oed={})
    rep.name = name
    return rep, asset


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
#
# ponytail: JSON file per portfolio plus one process-wide lock. Correct for a
# single uvicorn worker, which is what we run. Upgrade path is sqlite with a
# WAL, worth it the day the API is scaled out.

_LOCK = threading.Lock()


def _path(pid: str) -> str:
    return os.path.join(STORE, f"{pid}.json")


def save(rec: dict) -> None:
    os.makedirs(STORE, exist_ok=True)
    tmp = _path(rec["id"]) + ".tmp"
    with _LOCK:
        with open(tmp, "w") as fh:
            json.dump(rec, fh, separators=(",", ":"))
        os.replace(tmp, _path(rec["id"]))


def load(pid: str) -> dict | None:
    try:
        with open(_path(pid), "r") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def listing() -> list[dict]:
    if not os.path.isdir(STORE):
        return []
    out = []
    for fn in os.listdir(STORE):
        if not fn.endswith(".json"):
            continue
        rec = load(fn[:-5])
        if rec:
            out.append({
                "id": rec["id"],
                "name": rec["name"],
                "source_format": rec["source_format"],
                "uploaded": rec["uploaded"],
                "assets": len(rec["assets"]),
                "total_value": sum(a["value"] for a in rec["assets"]),
                "currency": rec["currency"],
                "hazard": rec["hazard"]["status"],
            })
    return sorted(out, key=lambda r: r["uploaded"], reverse=True)


# --------------------------------------------------------------------------
# cold hazard warming
# --------------------------------------------------------------------------

_THREADS: dict[str, threading.Thread] = {}


def _warm_asset(asset: dict) -> tuple[dict, dict, list[str], int]:
    """Live-read every peril/scenario/variant for one asset.

    Returns (points, protection, misses, max_pixel_offset_rings), keyed exactly
    as data/hazard_cache.json so the result is a drop-in merge.
    """
    assert _wc is not None and _read_sop is not None
    lon, lat = asset["lon"], asset["lat"]
    points: dict[str, dict] = {}
    protection: dict[str, dict] = {}
    misses: list[str] = []
    max_rings = 0

    jobs = [
        (peril, sc["id"], variant, path, units, res)
        for peril in _wc.PRICED + _wc.UNPRICED
        for sc in _wc.SCENARIOS
        for variant, path, units, res in _wc.paths_for(peril, sc["id"])
    ]

    # Forty S3 round trips per asset, each dominated by latency rather than by
    # anything we compute. Run sequentially the first asset costs ~8 minutes;
    # eight at a time it costs under one. zarr's LRUStoreCache and the lru_cache
    # around the array handles are both locked, so the workers share them safely.
    # ponytail: fixed pool of 8, which is where the public store stops getting
    # faster. Per-asset concurrency on top of this is the next step if a 500-row
    # upload ever needs to finish in minutes rather than hours.
    with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
        results = list(pool.map(lambda j: _wc.point_read(lon, lat, j[3]), jobs))

    for (peril, scenario, variant, path, units, res), got in zip(jobs, results):
        key = f"{lon:.4f}|{lat:.4f}|{peril}|{scenario}|{variant}"
        if got is None:
            misses.append(f"{peril}/{scenario}/{variant}")
            continue
        idx, vals, rings = got
        max_rings = max(max_rings, rings)
        points[key] = {
            "return_periods": [float(x) for x in idx],
            "intensities": [round(float(x), 5) for x in vals],
            "units": units,
            "dataset": "/".join(path.split("/")[:2]),
            "path": path,
            "resolution": res,
            "variant": variant,
            "pixel_offset_rings": rings,
        }

    for peril, kind in _wc.SOP_KIND.items():
        key = f"{lon:.4f}|{lat:.4f}|{peril}"
        got = _read_sop(lon, lat, kind)
        if got is None:
            protection[key] = {
                "sop_years": None, "source": _wc.FLOPROS_CITE[kind],
                "note": "no FLOPROS value; asset treated as undefended",
            }
        else:
            protection[key] = {
                "sop_years": got[0], "sop_range_years": [got[0], got[1]],
                "basis": "lower bound of the FLOPROS min/max range",
                "source": _wc.FLOPROS_CITE[kind],
            }
    return points, protection, misses, max_rings


def _warm(pid: str) -> None:
    """Background job. Writes progress into the record after every asset."""
    rec = load(pid)
    if rec is None:
        return
    rec["hazard"].update(status="warming", started=_now(), done=0)
    save(rec)

    by_row = {r["id"]: r for r in rec["report"]["rows"] if r["id"]}
    t0 = time.time()
    try:
        for n, asset in enumerate(rec["assets"], start=1):
            points, protection, misses, rings = _warm_asset(asset)
            rec["points"].update(points)
            rec["protection"].update(protection)

            flood = [k for k in points if "|inundation" in k]
            rep = by_row.get(asset["id"])
            if not flood:
                note = (
                    "no flood pixel within ~2 km of these coordinates: the "
                    "hazard grids are land-only, so the site is most likely in "
                    "open water"
                )
                asset.setdefault("warnings", []).append(note)
                if rep:
                    rep["warnings"].append(note)
                    rep["status"] = "warning" if rep["status"] == "accepted" else rep["status"]
                rec["hazard"]["open_water"].append(asset["id"])
            elif rings > 0:
                note = (
                    f"own pixel is nodata; nearest valid hazard pixel is "
                    f"{rings} pixel(s) (~{rings * _wc.PIXEL_DEG * 111.32:.1f} km) away"
                )
                asset.setdefault("warnings", []).append(note)
                if rep:
                    rep["warnings"].append(note)
                    rep["status"] = "warning" if rep["status"] == "accepted" else rep["status"]
            if misses:
                rec["hazard"]["misses"].extend(f"{asset['id']} {m}" for m in misses)

            rec["hazard"]["done"] = n
            rec["hazard"]["elapsed_s"] = round(time.time() - t0, 1)
            save(rec)

        rec["hazard"].update(status="ready", finished=_now(),
                             elapsed_s=round(time.time() - t0, 1))
    except Exception as exc:  # noqa: BLE001 - a failed job must say why, not vanish
        rec["hazard"].update(status="failed", error=repr(exc), finished=_now())
    rec["report"]["counts"] = _recount(rec["report"]["rows"])
    save(rec)


def _recount(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "accepted": sum(1 for r in rows if r["status"] == "accepted"),
        "warning": sum(1 for r in rows if r["status"] == "warning"),
        "rejected": sum(1 for r in rows if r["status"] == "rejected"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest(blob: bytes, filename: str, warm: bool = True) -> dict:
    """Parse, persist, and kick off the hazard warm. Returns the stored record.

    The portfolio id is the hash of the uploaded bytes, so re-uploading the same
    file returns the already-warmed portfolio instantly instead of paying for
    the cold reads twice.
    """
    pid = "pf_" + hashlib.sha256(blob).hexdigest()[:12]
    existing = load(pid)
    if existing is not None:
        existing["cached"] = True
        return existing

    p = parse(blob, filename)
    rec = {
        "id": pid,
        "name": p.name,
        "currency": p.currency,
        "source_format": p.source_format,
        "source_spec": OED_SPEC if p.source_format == "oed" else "generic CSV",
        "filename": filename,
        "uploaded": _now(),
        "note": FINANCIALS_NOTE,
        "cached": False,
        "assets": p.assets,
        "report": {"counts": p.counts, "rows": [r.as_dict() for r in p.rows]},
        "points": {},
        "protection": {},
        "hazard": {
            "status": "pending",
            "done": 0,
            "total": len(p.assets),
            "misses": [],
            "open_water": [],
            "error": _LIVE_ERROR,
            "aggregation": (
                "asset pixel, nearest valid pixel on nodata, at most "
                "2 pixels (~1.9 km); never a neighbourhood max"
            ),
        },
    }
    if _LIVE_ERROR is not None:
        rec["hazard"]["status"] = "failed"
    elif not p.assets or not warm:
        # Nothing to fetch, or the caller only wanted the file validated. Either
        # way "pending" would be a lie about a job that will never run.
        rec["hazard"]["status"] = "ready" if not p.assets else "not_requested"
    save(rec)

    if warm and _LIVE_ERROR is None and p.assets:
        t = threading.Thread(target=_warm, args=(pid,), daemon=True,
                             name=f"warm-{pid}")
        _THREADS[pid] = t
        t.start()
    return rec


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

router = APIRouter()


def _public(rec: dict) -> dict:
    """The record minus the hazard point blob, which is large and internal."""
    return {k: v for k, v in rec.items() if k not in ("points", "protection")}


@router.post("/api/portfolios/upload")
async def upload(file: UploadFile = File(...),
                 warm: bool = Form(True)) -> dict:
    blob = await file.read()
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // 2**20} MB.")
    return _public(ingest(blob, file.filename or "upload.csv", warm))


@router.get("/api/portfolios")
def portfolios() -> dict:
    return {"portfolios": listing()}


@router.get("/api/portfolios/{pid}/status")
def status(pid: str) -> dict:
    rec = load(pid)
    if rec is None:
        raise HTTPException(404, f"No uploaded portfolio '{pid}'.")
    return _public(rec)


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

_GOOD_GENERIC = (
    "name,lat,lon,value,sector,country\n"
    "Tanjung Priok Container Terminal,-6.1045,106.8806,420000000,Transport,Indonesia\n"
    "Botlek Chemical Terminal,51.9244,4.4034,740000000,Chemicals,Netherlands\n"
    "Houston Ship Channel Refinery,29.7355,-95.2637,980000000,Refining,United States\n"
)

_GOOD_OED = (
    "PortNumber,AccNumber,LocNumber,LocName,CountryCode,Latitude,Longitude,"
    "OccupancyCode,ConstructionCode,LocPerilsCovered,LocCurrency,"
    "BuildingTIV,OtherTIV,ContentsTIV,BITIV\n"
    "P1,A1,1,Cilegon Steel Complex,ID,-5.98,106.064,2250,5000,WW1,USD,"
    "500000000,20000000,160000000,310000000\n"
    "P1,A1,2,Limay Power Station,PH,14.525,120.6083,2510,5000,WW1,USD,"
    "480000000,0,40000000,143000000\n"
    "P1,A1,3,Hamburg Distribution Centre,DE,53.5511,9.9937,1131,5000,WW1,USD,"
    "200000000,0,60000000,0\n"
)

_BROKEN = (
    "PortNumber,AccNumber,LocNumber,LocName,CountryCode,Latitude,Longitude,"
    "OccupancyCode,LocPerilsCovered,LocCurrency,BuildingTIV,ContentsTIV,BITIV\n"
    # 1: latitude beyond the pole
    "P9,A9,101,Impossible Site,ID,-96.4,106.8,1151,WW1,USD,1000000,0,0\n"
    # 2: longitude past the antimeridian
    "P9,A9,102,Wrapped Longitude,VN,10.77,206.78,1151,WW1,USD,1000000,0,0\n"
    # 3: unset coordinates
    "P9,A9,103,Null Island,XX,0,0,1151,WW1,USD,1000000,0,0\n"
    # 4: address only, no coordinates
    "P9,A9,104,Geocode Me,SG,,,1151,WW1,USD,1000000,0,0\n"
    # 5: occupancy code that carries no building class
    "P9,A9,106,Offshore Platform,MY,3.0,101.39,3005,WW1,USD,50000000,0,0\n"
    # 6: duplicate LocNumber of an accepted row
    "P9,A9,106,Duplicate Of 106,ID,-6.1045,106.8806,1151,WW1,USD,1000000,0,0\n"
    # 7: no TIV at all
    "P9,A9,107,Valueless,TH,13.68,100.61,1151,WW1,USD,0,0,0\n"
    # 8: blank row
    ",,,,,,,,,,,,\n"
    # 9: mid-Pacific, valid coordinates, no land hazard pixel
    "P9,A9,109,Open Water,XX,0.0,-140.0,1151,WW1,USD,1000000,0,0\n"
    # 10: garbage in the coordinate columns
    "P9,A9,110,Not A Number,ID,north,east,1151,WW1,USD,1000000,0,0\n"
)


def _by_row(p: Parsed) -> dict[int, RowReport]:
    return {r.row: r for r in p.rows}


def demo() -> None:
    # -- region derivation must reproduce the hand-assigned demo regions ------
    for a in DEMO_ASSETS:
        got = region_for(a.lon, a.lat)
        assert got == a.region, f"{a.id}: derived {got}, portfolio says {a.region}"

    # -- occupancy mapping must land on classes the curve library holds -------
    for _, _, occ, _ in OED_OCCUPANCY_RANGES:
        assert occ in CURVE_OCCUPANCIES, f"{occ} is not a curve-library occupancy"
    for _, occ in SECTOR_KEYWORDS:
        assert occ in CURVE_OCCUPANCIES, f"{occ} is not a curve-library occupancy"
    # and the ranges must not overlap or the first match would be arbitrary
    seen: list[tuple[int, int]] = []
    for lo, hi, _, _ in OED_OCCUPANCY_RANGES:
        assert lo <= hi
        for a, b in seen:
            assert hi < a or lo > b, f"OED ranges {lo}-{hi} and {a}-{b} overlap"
        seen.append((lo, hi))
    assert occupancy_for_oed(1151)[0] == "Industrial"
    assert occupancy_for_oed(2760)[0] == "Transport"     # IFM Port Systems
    assert occupancy_for_oed(2520)[0] == "Infrastructure"  # nuclear power
    assert occupancy_for_oed(1231)[0] == "Education"
    assert occupancy_for_oed(3005)[2] is True, "offshore must be flagged vague"
    assert occupancy_for_oed(9999)[2] is True, "an unpublished code must be flagged"
    assert occupancy_for_sector("power plant")[0] == "Infrastructure", \
        "'power plant' must not be caught by the 'plant' keyword"
    assert occupancy_for_sector("Heavy industry")[0] == "Industrial"
    assert occupancy_for_sector("basket weaving") == ("Unknown", False)

    # every demo sector must reach a class with a real flood curve
    for a in DEMO_ASSETS:
        occ, matched = occupancy_for_sector(a.sector)
        assert matched, f"demo sector {a.sector!r} maps to nothing"
        assert curve_lib.best("inundation_riverine", a.region, occ) is not None

    # -- format detection ----------------------------------------------------
    assert detect_format(["LocNumber", "CountryCode"]) == "oed"
    assert detect_format(["name", "lat", "lon"]) == "generic"

    # -- the good generic file -----------------------------------------------
    g = parse(_GOOD_GENERIC.encode(), "generic.csv")
    assert g.source_format == "generic"
    assert len(g.assets) == 3 and g.counts["rejected"] == 0
    regions = {a["name"]: a["region"] for a in g.assets}
    assert regions["Botlek Chemical Terminal"] == "Europe"
    assert regions["Houston Ship Channel Refinery"] == "North America"
    assert regions["Tanjung Priok Container Terminal"] == "Asia"
    assert all(a["has_financials"] is False for a in g.assets), \
        "a CSV with no debt columns must not be credited with financials"
    assert all(a["debt"] == 0.0 and a["annual_debt_service"] == 0.0 for a in g.assets), \
        "absent financials must be zero, never estimated"
    assert all(any("no financial data" in w for w in a["warnings"]) for a in g.assets)

    # -- the good OED file ---------------------------------------------------
    o = parse(_GOOD_OED.encode(), "oed.csv")
    assert o.source_format == "oed"
    assert o.name == "P1" and o.currency == "USD"
    assert len(o.assets) == 3 and o.counts["rejected"] == 0
    steel, power, dc = o.assets
    assert steel["id"] == "A1-1", steel["id"]
    assert steel["occupancy"] == "Industrial"      # 2250, IFM Metal Processing
    assert steel["value"] == 680_000_000.0, "value is Building + Other + Contents"
    assert steel["annual_revenue"] == 310_000_000.0
    assert any("BITIV" in w for w in steel["warnings"]), "BITIV use must be disclosed"
    assert power["occupancy"] == "Infrastructure"  # 2510, IFM thermo-electric
    assert dc["occupancy"] == "Commercial"         # 1131, Commercial range
    assert dc["annual_revenue"] == 0.0 and dc["has_financials"] is False
    assert steel["has_financials"] is False, "revenue alone is not a balance sheet"

    # -- the deliberately broken file ---------------------------------------
    b = parse(_BROKEN.encode(), "broken.csv")
    rows = _by_row(b)
    assert b.counts["total"] == 10, b.counts
    assert rows[1].status == "rejected" and "out of range" in rows[1].reason
    assert rows[2].status == "rejected" and "out of range" in rows[2].reason
    assert rows[3].status == "rejected" and "Null Island" in rows[3].reason
    assert rows[4].status == "rejected" and "geocoding" in rows[4].reason
    assert rows[5].status == "warning"
    assert any("Offshore" in w for w in rows[5].warnings)
    assert rows[6].status == "warning", "a duplicate id is kept, not dropped"
    assert any("duplicate id" in w for w in rows[6].warnings)
    assert rows[7].status == "warning"
    assert any("no asset value" in w for w in rows[7].warnings)
    assert rows[8].status == "rejected" and rows[8].reason == "blank row"
    assert rows[10].status == "rejected" and "non-numeric" in rows[10].reason
    assert b.counts["rejected"] == 6 and b.counts["warning"] == 4, b.counts
    assert len(b.assets) == 4, "every non-rejected row must produce an asset"
    ids = [a["id"] for a in b.assets]
    assert len(ids) == len(set(ids)), f"ids must be unique after de-duping: {ids}"
    assert "A9-106-2" in ids, ids
    # nothing vanished: rejected + assets == input rows
    assert b.counts["rejected"] + len(b.assets) == b.counts["total"]

    # -- a file we cannot use at all must be refused, not half-parsed --------
    for bad, why in ((b"", "empty"), (b"a,b,c\n", "no data rows"),
                     (b"foo,bar\n1,2\n", "no coordinates")):
        try:
            parse(bad, "bad.csv")
            raise AssertionError(f"{why}: should have been refused")
        except HTTPException:
            pass

    print(f"ingest.py parse self-check passed "
          f"({b.counts['rejected']} rejected, {b.counts['warning']} warned, "
          f"{len(b.assets)} assets kept from {b.counts['total']} rows)")

    # -- live cold read ------------------------------------------------------
    if _LIVE_ERROR is not None:
        print(f"ingest.py live self-check: DEGRADED ({_LIVE_ERROR})")
        return
    try:
        # Singapore: real land, real WRI coverage, not in the warmed cache.
        t0 = time.time()
        got = _wc.point_read(103.8198, 1.3521,
                             "inundation/wri/v2/inunriver_historical_000000000WATCH_1980")
        cold = time.time() - t0
        assert got is not None, "a land point must resolve against the WRI grid"
        idx, vals, rings = got
        assert len(idx) == len(vals) and all(v >= 0 for v in vals)
        assert rings <= _wc.SEARCH_RINGS

        # Mid-Pacific: valid coordinates, no land pixel. Must miss, not return 0.
        assert _wc.point_read(-140.0, 0.0,
                              "inundation/wri/v2/inunriver_historical_000000000WATCH_1980") is None
        assert _read_sop(-140.0, 0.0, "coastal") is None, \
            "open water must read as no protection standard, not zero protection"

        # A repeat read of the same pixel must come from the chunk cache.
        t0 = time.time()
        _wc.point_read(103.8198, 1.3521,
                       "inundation/wri/v2/inunriver_historical_000000000WATCH_1980")
        warm_s = time.time() - t0
        assert warm_s < cold, f"cache did not help: cold {cold:.2f}s, warm {warm_s:.2f}s"
        print(f"ingest.py live self-check passed (cold read {cold:.2f}s, "
              f"cached re-read {warm_s:.3f}s, offset {rings} pixel(s))")
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - offline is not a code failure
        print(f"ingest.py live self-check: SKIPPED (no network? {exc!r})")


if __name__ == "__main__":
    demo()
