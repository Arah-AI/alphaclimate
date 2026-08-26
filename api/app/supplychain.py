"""Indirect loss: what the asset's customers lose when the asset stops.

Direct damage is the part everyone models. It is also the smaller part. When a
container terminal floods, the terminal loses a quarter of a year of throughput;
the factories that ship through it lose the same quarter whether or not a drop
of water reached them. The disaster-IO literature (Hallegatte 2008; Koks &
Thissen 2016) puts the indirect share at anywhere from a third to several times
the direct loss, and almost no commercial climate-risk product prices it.

The machinery is a multi-regional input-output table. Each asset is mapped onto
a (region, sector) cell; the direct production loss is placed in that cell; the
loss is propagated to the cells that BUY from it.

Direction matters and is easy to get backwards. The Leontief inverse
L = (I - A)^-1 propagates a demand shock BACKWARD to suppliers. A stopped port
is a SUPPLY shock and travels FORWARD to customers, which is the Ghosh inverse
G = (I - B)^-1 with B = x^-1 Z. The two are the same object seen from two
sides: G = x^-1 L x exactly (verified numerically in demo() to 1e-15). So this
module does build the Leontief inverse, and then reads it in the forward
direction rather than pretending a backward multiplier answers a forward
question.

WHAT THIS RUNS ON. pymrio ships a small test MRIO, six anonymous regions by
eight sectors, that loads in 25 ms with no download. EXIOBASE 3 proper is
multi-gigabyte and is NOT downloaded here. The test system's regions carry no
geography at all, so no asset can be honestly said to sit in one: every region
match below is reported as a placeholder, and the API payload says so on every
response. The multipliers the test system produces (1.00 to 2.22) are a property
of toy data, not an economic finding. Point ALPHACLIMATE_MRIO at a real system
and the same code answers for real.

WHY IT IS AN UPPER BOUND. A static inverse assumes fixed technical
coefficients: no substituting a different port, no drawing down inventory, no
spare capacity anywhere, and no rationing of the shortfall. Every one of those
lowers the true indirect loss. This is a ceiling, and the payload says so
rather than leaving it in a comment nobody reads.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, asdict, field
from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException, Query

if __package__ in (None, ""):  # `python api/app/supplychain.py`
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import compute, finance, hazard as hz, portfolio as pf
else:
    from . import compute, finance, hazard as hz, portfolio as pf

log = logging.getLogger("alphaclimate.supplychain")

router = APIRouter()


class MrioUnavailable(RuntimeError):
    """The input-output table could not be loaded. Never silently substituted."""


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

MRIO_SOURCE = os.environ.get("ALPHACLIMATE_MRIO", "pymrio_test")
EXIOBASE_PATH = os.environ.get("ALPHACLIMATE_EXIOBASE_PATH", "")

# Roughly how large a real EXIOBASE 3 year is on disk, so the failure message
# can tell the operator what they are signing up for instead of just failing.
EXIOBASE_DOWNLOAD_NOTE = (
    "EXIOBASE 3 is not bundled and is not downloaded on demand: one year of the "
    "product-by-product monetary table is several GB compressed. Fetch it once "
    "with pymrio.download_exiobase3(storage_folder=..., years=[2022], "
    "system='pxp'), then set ALPHACLIMATE_EXIOBASE_PATH to the .zip and "
    "ALPHACLIMATE_MRIO=exiobase3."
)


def _load_pymrio_test():
    import pymrio

    return pymrio.load_test().calc_all()


def _load_exiobase3():
    import pymrio

    if not EXIOBASE_PATH or not os.path.exists(EXIOBASE_PATH):
        raise MrioUnavailable(
            f"ALPHACLIMATE_MRIO=exiobase3 but no table at "
            f"'{EXIOBASE_PATH or '<unset ALPHACLIMATE_EXIOBASE_PATH>'}'. "
            + EXIOBASE_DOWNLOAD_NOTE
        )
    return pymrio.parse_exiobase3(path=EXIOBASE_PATH).calc_all()


# Swappable by config. Each entry says what it is so the payload never has to
# guess whether the numbers came from a toy or from a real table.
MRIO_SOURCES: dict[str, dict] = {
    "pymrio_test": {
        "loader": _load_pymrio_test,
        "label": "pymrio built-in test MRIO",
        "real_geography": False,
        "citation": (
            "Stadler, K. (2021). Pymrio: A Python Based Multi-Regional "
            "Input-Output Analysis Toolbox. Journal of Open Research Software, "
            "9(1), 8. Test system shipped inside the package."
        ),
        "caveat": (
            "This is pymrio's TEST system, not EXIOBASE. Six anonymous regions "
            "(reg1..reg6) by eight sectors, present so the package can be unit "
            "tested. Its regions carry no geography, so no asset can be placed "
            "in one; its multipliers are a property of synthetic data. Use it to "
            "read the mechanism, never to size a real indirect loss."
        ),
    },
    "exiobase3": {
        "loader": _load_exiobase3,
        "label": "EXIOBASE 3 (operator-supplied)",
        "real_geography": True,
        "citation": (
            "Stadler et al. (2018). EXIOBASE 3: Developing a Time Series of "
            "Detailed Environmentally Extended Multi-Regional Input-Output "
            "Tables. Journal of Industrial Ecology 22(3), 502-515."
        ),
        "caveat": EXIOBASE_DOWNLOAD_NOTE,
    },
}


@dataclass
class Table:
    """The bits of an MRIO this module actually uses."""

    source: str
    label: str
    citation: str
    caveat: str
    real_geography: bool
    regions: list[str]
    sectors: list[str]
    cells: list[tuple[str, str]]        # row order of L / G
    ghosh: np.ndarray                   # forward (customer-side) inverse
    leontief: np.ndarray                # backward (supplier-side) inverse

    def index_of(self, region: str, sector: str) -> int:
        return self.cells.index((region, sector))


@lru_cache(maxsize=2)
def table(source: str = "") -> Table:
    """Load and invert the MRIO once per process. Raises, never fabricates."""
    src = source or MRIO_SOURCE
    spec = MRIO_SOURCES.get(src)
    if spec is None:
        raise MrioUnavailable(
            f"Unknown ALPHACLIMATE_MRIO='{src}'. Known: {sorted(MRIO_SOURCES)}."
        )
    try:
        io = spec["loader"]()
    except MrioUnavailable:
        raise
    except ImportError as exc:
        raise MrioUnavailable(f"pymrio is not installed: {exc}") from exc
    except Exception as exc:  # a broken table is a failure, not a zero
        raise MrioUnavailable(f"could not load MRIO '{src}': {exc}") from exc

    L = np.asarray(io.L.values, dtype=float)
    x = np.asarray(io.x["indout"].values, dtype=float).ravel()
    if L.shape[0] != L.shape[1] or L.shape[0] != x.size:
        raise MrioUnavailable(
            f"MRIO '{src}' is malformed: L is {L.shape}, x is {x.shape}"
        )

    # G = x^-1 L x. Exact, not an approximation: see the module docstring.
    # Zero-output cells cannot receive or pass a shock; guarding the divisor at
    # 1.0 leaves their row and column at the identity rather than at infinity.
    xh = np.where(x > 0, x, 1.0)
    G = (L * xh[None, :]) / xh[:, None]

    cells = [(str(r), str(s)) for r, s in io.L.index]
    return Table(
        source=src,
        label=spec["label"],
        citation=spec["citation"],
        caveat=spec["caveat"],
        real_geography=bool(spec["real_geography"]),
        regions=[str(r) for r in io.get_regions()],
        sectors=[str(s) for s in io.get_sectors()],
        cells=cells,
        ghosh=G,
        leontief=L,
    )


# --------------------------------------------------------------------------
# mapping assets onto the table
# --------------------------------------------------------------------------
#
# Two halves, kept separate because they fail differently. A sector can be
# matched on meaning. A region can only be matched if the table has real
# geography, and the test system does not.

# Portfolio sector -> (canonical MRIO concept, quality if that concept resolves).
# "exact" means the portfolio label and the MRIO sector are the same activity.
# "approximate" means the MRIO is coarser than the asset and the asset has been
# folded into the nearest sector that exists. Nothing is ever mapped silently.
SECTOR_MAP: dict[str, tuple[str, str]] = {
    "Transport":      ("transport", "exact"),
    "Logistics":      ("transport", "approximate"),   # warehousing folded in
    "Power":          ("electricity", "exact"),
    "Manufacturing":  ("manufacturing", "exact"),
    "Heavy industry": ("manufacturing", "approximate"),
    "Chemicals":      ("manufacturing", "approximate"),
    "Refining":       ("manufacturing", "approximate"),
    "Real estate":    ("services", "approximate"),
}

# Canonical concept -> lowercase substrings to look for in the table's own
# sector names, best first. The test system spells manufacturing
# "manufactoring"; EXIOBASE spells it a hundred different ways. Substring
# matching against the table's real sector list is what makes the same mapping
# survive a source swap.
SECTOR_ALIASES: dict[str, tuple[str, ...]] = {
    "transport": ("transport", "storage", "shipping"),
    "electricity": ("electricity", "power", "electric"),
    "manufacturing": ("manufactoring", "manufactur", "machinery", "chemical", "metal"),
    "services": ("other", "real estate", "service"),
}

# Country -> ISO2, the region code a real MRIO uses. Only the demo portfolio's
# countries are listed; an unlisted country is reported unmapped, not guessed.
COUNTRY_ISO: dict[str, str] = {
    "Indonesia": "ID",
    "Vietnam": "VN",
    "Thailand": "TH",
    "Malaysia": "MY",
    "Philippines": "PH",
    "India": "IN",
    "Japan": "JP",
    "Netherlands": "NL",
    "Germany": "DE",
    "United States": "US",
}


@dataclass
class Mapping:
    """Where one asset landed in the table, and how good the landing was."""

    asset_id: str
    country: str
    sector: str
    mrio_region: str | None
    mrio_sector: str | None
    region_match: str            # exact | placeholder | unmapped
    sector_match: str            # exact | approximate | unmapped
    note: str

    @property
    def usable(self) -> bool:
        return self.mrio_region is not None and self.mrio_sector is not None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["usable"] = self.usable
        return d


def _match_sector(t: Table, portfolio_sector: str) -> tuple[str | None, str, str]:
    concept, quality = SECTOR_MAP.get(portfolio_sector, (None, "unmapped"))
    if concept is None:
        return None, "unmapped", (
            f"no entry for sector '{portfolio_sector}' in SECTOR_MAP; refusing "
            f"to guess a sector for it"
        )
    lowered = {s.lower(): s for s in t.sectors}
    for alias in SECTOR_ALIASES.get(concept, ()):
        for low, orig in lowered.items():
            if alias in low:
                note = (
                    f"'{portfolio_sector}' -> {concept} -> '{orig}'"
                    + ("" if quality == "exact" else
                       f"; the table is coarser than the asset, so this is the "
                       f"nearest available activity")
                )
                return orig, quality, note
    return None, "unmapped", (
        f"'{portfolio_sector}' maps to concept '{concept}' but the table has no "
        f"sector matching {SECTOR_ALIASES.get(concept, ())}"
    )


def _match_region(t: Table, country: str) -> tuple[str | None, str, str]:
    iso = COUNTRY_ISO.get(country)
    by_low = {r.lower(): r for r in t.regions}
    for cand in (iso, country):
        if cand and cand.lower() in by_low:
            return by_low[cand.lower()], "exact", f"'{country}' is region '{by_low[cand.lower()]}'"
    if t.real_geography:
        return None, "unmapped", (
            f"'{country}' (ISO {iso or '?'}) is not a region of {t.label}; the "
            f"asset sits in a rest-of-world aggregate this code will not guess at"
        )
    # No geography to match against. Assign deterministically so the answer is
    # reproducible, and label it a placeholder so nobody reads it as a location.
    # md5 rather than hash() because hash() is salted per process.
    digest = hashlib.md5(country.encode()).hexdigest()
    region = t.regions[int(digest, 16) % len(t.regions)]
    return region, "placeholder", (
        f"{t.label} has anonymous regions, so '{country}' cannot be matched to "
        f"one. Assigned '{region}' by a stable hash purely so the run is "
        f"reproducible. This is NOT a claim about where the asset is."
    )


def mapping_for(asset: pf.Asset, t: Table | None = None) -> Mapping:
    t = t or table()
    sector, sq, snote = _match_sector(t, asset.sector)
    region, rq, rnote = _match_region(t, asset.country)
    return Mapping(
        asset_id=asset.id,
        country=asset.country,
        sector=asset.sector,
        mrio_region=region,
        mrio_sector=sector,
        region_match=rq,
        sector_match=sq,
        note=f"{rnote}. {snote}.",
    )


# --------------------------------------------------------------------------
# propagation
# --------------------------------------------------------------------------

@dataclass
class Propagation:
    """One shock, pushed forward through the table."""

    direct: float
    indirect: float
    multiplier: float
    downstream: list[dict] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.direct + self.indirect

    def as_dict(self) -> dict:
        d = asdict(self)
        d["total"] = round(self.total, 2)
        d["direct"] = round(self.direct, 2)
        d["indirect"] = round(self.indirect, 2)
        d["multiplier"] = round(self.multiplier, 4)
        return d


def propagate(t: Table, region: str, sector: str, direct_loss: float,
              top_n: int = 6) -> Propagation:
    """Push a production loss forward to the cells that buy from this one.

    The MRIO supplies the dimensionless multiplier structure only. Money comes
    from the asset, so the table's own currency and base year never enter the
    answer.
    """
    i = t.index_of(region, sector)
    row = t.ghosh[i] * float(direct_loss)
    # G[i, i] >= 1: it carries the shock itself plus the loop that comes back
    # round to the same cell. Removing exactly the direct loss keeps that loop
    # in the indirect term, where it belongs.
    row = row.copy()
    row[i] -= float(direct_loss)
    indirect = float(row.sum())
    multiplier = float(t.ghosh[i].sum())

    order = np.argsort(row)[::-1][:top_n]
    downstream = [
        {
            "region": t.cells[j][0],
            "sector": t.cells[j][1],
            "loss": round(float(row[j]), 2),
            "share_of_indirect": (
                round(float(row[j]) / indirect, 4) if indirect > 0 else 0.0
            ),
            "self_cell": bool(j == i),
        }
        for j in order
        if row[j] > 0
    ]
    return Propagation(float(direct_loss), indirect, multiplier, downstream)


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------

METHOD_NOTE = {
    "what_this_is": (
        "Indirect (higher-order) production loss: the output that the asset's "
        "customers cannot produce because the asset stopped. It is additional "
        "to the direct damage in /api/asset, not a re-slicing of it."
    ),
    "propagation": (
        "Leontief inverse L = (I - A)^-1 of the configured MRIO, read forward "
        "as the Ghosh inverse G = x^-1 L x. Forward is the correct direction "
        "for a supply outage: L on its own answers the supplier-side question."
    ),
    "upper_bound": (
        "UPPER BOUND. A static inverse holds technical coefficients fixed: no "
        "substituting a different supplier, no inventory drawdown, no spare "
        "capacity, no rationing of the shortfall, no post-event rebuild demand. "
        "Every one of those reduces the real indirect loss. Treat the number as "
        "a ceiling, not a central estimate."
    ),
    "money": (
        "The MRIO contributes the dimensionless multiplier only. All currency "
        "amounts are the asset's own annual output, so the table's base year "
        "and currency do not enter the answer."
    ),
    "outage": (
        "Direct production loss = annual output x outage share, where the "
        "outage share is the engine's damage fraction scaled by "
        "finance.Assumptions.downtime_days_per_damage_unit "
        f"({finance.Assumptions().downtime_days_per_damage_unit:.0f} days at "
        "total damage) and capped at one year."
    ),
}


@dataclass
class AssetIndirect:
    asset_id: str
    name: str
    country: str
    sector: str
    annual_output: float
    damage_fraction: float
    outage_days: float
    prop: Propagation | None
    mapping: Mapping
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.asset_id,
            "name": self.name,
            "country": self.country,
            "sector": self.sector,
            "annual_output": round(self.annual_output, 2),
            "damage_fraction": round(self.damage_fraction, 6),
            "outage_days": round(self.outage_days, 1),
            "direct_loss": round(self.prop.direct, 2) if self.prop else 0.0,
            "indirect_loss": round(self.prop.indirect, 2) if self.prop else 0.0,
            "total_loss": round(self.prop.total, 2) if self.prop else 0.0,
            "multiplier": round(self.prop.multiplier, 4) if self.prop else None,
            "top_downstream": self.prop.downstream if self.prop else [],
            "mapping": self.mapping.as_dict(),
            "reason": self.reason,
        }


def _damage_fraction(asset: pf.Asset, scenario: str) -> float:
    """Peak damage fraction across the asset's perils, from the risk engine.

    Uses compute._asset_perils rather than compute.asset_detail because the
    latter also runs the full uncertainty sweep, which this endpoint does not
    need and would pay for twelve times over.
    """
    results = compute._asset_perils(asset, scenario)
    return max((r.mean_damage_fraction for r in results), default=0.0)


def for_asset(asset: pf.Asset, scenario: str = "ssp585",
              t: Table | None = None) -> AssetIndirect:
    t = t or table()
    m = mapping_for(asset, t)
    a = finance.Assumptions()
    frac = _damage_fraction(asset, scenario)
    outage_days = min(365.0, frac * a.downtime_days_per_damage_unit)
    direct = asset.annual_revenue * (outage_days / 365.0)

    if not m.usable:
        return AssetIndirect(
            asset.id, asset.name, asset.country, asset.sector,
            asset.annual_revenue, frac, outage_days, None, m,
            reason="not mapped onto the table; no indirect loss computed",
        )
    prop = propagate(t, m.mrio_region, m.mrio_sector, direct)
    reason = "" if direct > 0 else (
        "no modelled outage at this asset, so nothing propagates"
        if not hz.status()["degraded"]
        else "hazard cache is degraded, so no damage fraction is available"
    )
    return AssetIndirect(
        asset.id, asset.name, asset.country, asset.sector,
        asset.annual_revenue, frac, outage_days, prop, m, reason=reason,
    )


def _table_block(t: Table) -> dict:
    return {
        "source": t.source,
        "label": t.label,
        "citation": t.citation,
        "caveat": t.caveat,
        "is_exiobase": t.source == "exiobase3",
        "real_geography": t.real_geography,
        "regions": len(t.regions),
        "sectors": len(t.sectors),
        "region_names": t.regions,
        "sector_names": t.sectors,
    }


def asset_indirect(asset_id: str, scenario: str = "ssp585") -> dict | None:
    asset = pf.by_id(asset_id)
    if asset is None:
        return None
    t = table()
    row = for_asset(asset, scenario, t)
    return {
        "asset": asset.as_dict(),
        "scenario": scenario,
        "indirect": row.as_dict(),
        "mrio": _table_block(t),
        "method": METHOD_NOTE,
        "hazard_degraded": hz.status()["degraded"],
    }


def portfolio_indirect(scenario: str = "ssp585") -> dict:
    t = table()
    rows = [for_asset(a, scenario, t) for a in pf.DEMO_ASSETS]
    direct = sum(r.prop.direct for r in rows if r.prop)
    indirect = sum(r.prop.indirect for r in rows if r.prop)

    quality = {"exact": 0, "approximate": 0, "placeholder": 0, "unmapped": 0}
    for r in rows:
        quality[r.mapping.sector_match] = quality.get(r.mapping.sector_match, 0) + 1
    region_quality: dict[str, int] = {}
    for r in rows:
        region_quality[r.mapping.region_match] = (
            region_quality.get(r.mapping.region_match, 0) + 1
        )

    out = [r.as_dict() for r in rows]
    out.sort(key=lambda r: r["indirect_loss"], reverse=True)
    return {
        "portfolio": {
            "id": pf.DEMO_PORTFOLIO["id"],
            "name": pf.DEMO_PORTFOLIO["name"],
            "currency": pf.DEMO_PORTFOLIO["currency"],
        },
        "scenario": scenario,
        "headline": {
            "direct_production_loss": round(direct, 2),
            "indirect_production_loss": round(indirect, 2),
            "total_production_loss": round(direct + indirect, 2),
            "portfolio_multiplier": round((direct + indirect) / direct, 4) if direct else None,
            "indirect_share": round(indirect / (direct + indirect), 4) if direct + indirect else 0.0,
            "assets_mapped": sum(1 for r in rows if r.mapping.usable),
            "asset_count": len(rows),
        },
        "mapping_quality": {"sector": quality, "region": region_quality},
        "assets": out,
        "mrio": _table_block(t),
        "method": METHOD_NOTE,
        "hazard_degraded": hz.status()["degraded"],
    }


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def _scenario_or_400(scenario: str) -> str:
    if scenario not in {s["id"] for s in hz.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{scenario}'.")
    return scenario


@router.get("/api/supplychain")
def supplychain_portfolio(scenario: str = Query("ssp585")) -> dict:
    """Indirect production loss for every asset, with the mapping quality."""
    _scenario_or_400(scenario)
    try:
        return portfolio_indirect(scenario)
    except MrioUnavailable as exc:
        raise HTTPException(503, str(exc))


@router.get("/api/supplychain/{asset_id}")
def supplychain_asset(asset_id: str, scenario: str = Query("ssp585")) -> dict:
    """Indirect production loss for one asset, with its downstream cells."""
    _scenario_or_400(scenario)
    try:
        detail = asset_indirect(asset_id, scenario)
    except MrioUnavailable as exc:
        raise HTTPException(503, str(exc))
    if detail is None:
        raise HTTPException(404, f"No asset '{asset_id}'.")
    return detail


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    try:
        t = table()
    except MrioUnavailable as exc:
        print(f"supplychain.py self-check: DEGRADED ({exc})")
        return

    # The table loaded and is square.
    n = len(t.cells)
    assert n == len(t.regions) * len(t.sectors), "cells must be regions x sectors"
    assert t.ghosh.shape == (n, n) and t.leontief.shape == (n, n)

    # The identity the whole module rests on: G = x^-1 L x. If this ever fails,
    # the forward reading of the Leontief inverse is not a Ghosh inverse and
    # every indirect number below is answering the wrong question.
    import pymrio

    io = pymrio.load_test().calc_all()
    Z = np.asarray(io.Z.values, dtype=float)
    x = np.asarray(io.x["indout"].values, dtype=float).ravel()
    B = Z / np.where(x > 0, x, 1.0)[:, None]
    G_direct = np.linalg.inv(np.eye(n) - B)
    err = float(np.abs(G_direct - t.ghosh).max())
    assert err < 1e-9, f"G = x^-1 L x does not hold, max error {err:.2e}"

    # Multipliers are bounded below by 1: a cell always loses at least its own
    # output, and can never gain from its own outage.
    mult = t.ghosh.sum(axis=1)
    assert mult.min() >= 1.0 - 1e-9, f"a multiplier below 1 is impossible: {mult.min()}"
    assert np.isfinite(mult).all(), "zero-output cells must not produce infinities"

    # --- mapping ---------------------------------------------------------
    maps = [mapping_for(a, t) for a in pf.DEMO_ASSETS]
    assert all(m.mrio_sector in t.sectors for m in maps if m.mrio_sector), \
        "a mapped sector must exist in the table"
    assert all(m.mrio_region in t.regions for m in maps if m.mrio_region), \
        "a mapped region must exist in the table"
    assert {m.sector_match for m in maps} <= {"exact", "approximate", "unmapped"}
    assert any(m.sector_match == "exact" for m in maps), "some sectors match exactly"
    assert any(m.sector_match == "approximate" for m in maps), \
        "folding a refinery into 'manufactoring' must be reported as approximate"

    # The test system has anonymous regions, so NO asset may claim an exact
    # region match. This is the assertion that stops a silent mismatch.
    assert all(m.region_match == "placeholder" for m in maps), \
        "the test MRIO has no geography; no asset may claim an exact region"
    assert all("NOT a claim about where the asset is" in m.note for m in maps)

    # Deterministic: the same country lands in the same region every run.
    again = [mapping_for(a, t) for a in pf.DEMO_ASSETS]
    assert [m.mrio_region for m in maps] == [m.mrio_region for m in again]
    tj = mapping_for(pf.by_id("tj-priok"), t)
    cat = mapping_for(pf.by_id("cat-lai"), t)
    assert tj.mrio_sector == cat.mrio_sector == "transport"
    assert tj.sector_match == "exact", "a port terminal IS transport"
    assert mapping_for(pf.by_id("houston-ref"), t).sector_match == "approximate"

    # An unknown sector is refused, not guessed into the nearest bucket.
    ghost = pf.Asset("ghost", "Ghost", "Indonesia", 0, 0, "Cryptomining",
                     "Industrial", "Asia", 1, 1, 0, 1)
    gm = mapping_for(ghost, t)
    assert gm.sector_match == "unmapped" and gm.mrio_sector is None
    assert not gm.usable

    # An unknown country still resolves under a placeholder table, because the
    # placeholder is honest about meaning nothing.
    nowhere = pf.Asset("nw", "Nowhere", "Freedonia", 0, 0, "Transport",
                       "Transport", "Asia", 1, 1, 0, 1)
    assert mapping_for(nowhere, t).region_match == "placeholder"

    # --- propagation -----------------------------------------------------
    i_region, i_sector = "reg1", "electricity"
    p = propagate(t, i_region, i_sector, 1_000_000.0)
    assert p.direct == 1_000_000.0
    assert p.indirect > 0, "electricity in the test system has real customers"
    assert abs(p.total - (p.direct + p.indirect)) < 1e-6
    assert p.multiplier > 1.0
    assert abs(p.multiplier - p.total / p.direct) < 1e-9, \
        "the multiplier must be the total over the direct, not a second number"

    # Every downstream cell adds up to the indirect loss, with nothing hidden.
    idx = t.index_of(i_region, i_sector)
    row = t.ghosh[idx] * 1_000_000.0
    row[idx] -= 1_000_000.0
    assert abs(row.sum() - p.indirect) < 1e-6
    assert p.downstream == sorted(p.downstream, key=lambda d: d["loss"], reverse=True)
    assert all(d["loss"] > 0 for d in p.downstream)
    assert sum(d["loss"] for d in p.downstream) <= p.indirect + 1e-6, \
        "the top-N slice cannot exceed the total it is a slice of"

    # Linear in the shock: doubling the outage doubles the indirect loss.
    p2 = propagate(t, i_region, i_sector, 2_000_000.0)
    assert abs(p2.indirect - 2 * p.indirect) < 1e-6
    assert abs(p2.multiplier - p.multiplier) < 1e-12, "the multiplier is a property " \
        "of the table, not of the shock"

    # No outage, no indirect loss. Never a small positive one.
    zero = propagate(t, i_region, i_sector, 0.0)
    assert zero.direct == 0.0 and zero.indirect == 0.0 and zero.downstream == []

    # --- assets ----------------------------------------------------------
    degraded = hz.status()["degraded"]
    port = portfolio_indirect("ssp585")
    h = port["headline"]
    assert h["asset_count"] == len(pf.DEMO_ASSETS)
    assert h["assets_mapped"] == len(pf.DEMO_ASSETS), "every demo asset must map"
    assert port["mrio"]["is_exiobase"] is False, "the demo does not run on EXIOBASE"
    assert "TEST system, not EXIOBASE" in port["mrio"]["caveat"]
    assert "UPPER BOUND" in port["method"]["upper_bound"]
    assert port["assets"] == sorted(
        port["assets"], key=lambda r: r["indirect_loss"], reverse=True
    )
    for r in port["assets"]:
        assert r["indirect_loss"] >= 0
        assert abs(r["total_loss"] - (r["direct_loss"] + r["indirect_loss"])) < 1.0
        assert r["outage_days"] <= 365.0, "an asset cannot be down more than a year"
        assert r["direct_loss"] <= r["annual_output"] + 1.0, \
            "a year of lost output is the ceiling on a year of lost output"
        if r["multiplier"] is not None:
            assert r["multiplier"] >= 1.0

    if degraded:
        assert h["direct_production_loss"] == 0.0, \
            "a degraded hazard cache must give zero, not an invented outage"
        print("supplychain.py self-check passed (hazard cache DEGRADED, "
              "mapping and propagation checked, no losses to report)")
        return

    assert h["direct_production_loss"] > 0, \
        "a warm hazard cache and twelve flood-exposed assets must produce an outage"
    assert h["indirect_production_loss"] > 0
    assert h["portfolio_multiplier"] > 1.0
    assert 0.0 < h["indirect_share"] < 1.0

    # A single asset endpoint payload agrees with its row in the portfolio one.
    one = asset_indirect("tj-priok", "ssp585")
    match = [r for r in port["assets"] if r["id"] == "tj-priok"][0]
    assert abs(one["indirect"]["indirect_loss"] - match["indirect_loss"]) < 1.0, \
        "the two endpoints must not disagree about the same asset"
    assert asset_indirect("does-not-exist") is None

    # A calmer scenario cannot produce a larger outage.
    calm = portfolio_indirect("ssp126")
    assert calm["headline"]["direct_production_loss"] <= h["direct_production_loss"] + 1.0, \
        "SSP1-2.6 must not flood harder than SSP5-8.5"

    print(
        f"supplychain.py self-check passed "
        f"({t.label}, {len(t.regions)}x{len(t.sectors)} cells, "
        f"direct ${h['direct_production_loss']/1e6:.1f}m, "
        f"indirect ${h['indirect_production_loss']/1e6:.1f}m, "
        f"portfolio multiplier {h['portfolio_multiplier']:.3f}, "
        f"multiplier range {mult.min():.3f}-{mult.max():.3f})"
    )


if __name__ == "__main__":
    demo()
