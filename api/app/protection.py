"""Flood defences: FLOPROS standard of protection, served from the hazard cache.

The WRI inundation layers are undefended. Read straight, they charge a port
protected to a 1-in-100 standard for every 1-in-2 event it has never seen, and
because expected annual loss is dominated by high-frequency events that single
omission is worth roughly an order of magnitude.

FLOPROS (Scussolini et al. 2016, NHESS) gives a protection standard in
return-period years per location. scripts/warm_cache.py reads it once per asset
and writes it into data/hazard_cache.json; this module hands it to the engine.

Absence is not a default. Where FLOPROS has no value for a location the asset
is reported undefended and the raw undefended hazard is used, because inventing
a protection standard is the same failure in the opposite direction.
"""

from __future__ import annotations

from . import hazard as hz

# Only the flood perils are defended. Wind has no equivalent standard.
DEFENDED_PERILS = ("inundation_coastal", "inundation_riverine")


def _block() -> dict:
    return hz._cache().get("protection", {}) or {}


def record(lon: float, lat: float, peril: str) -> dict | None:
    """The full FLOPROS record for a point and peril, or None if we hold none."""
    if peril not in DEFENDED_PERILS:
        return None
    return _block().get(f"{lon:.4f}|{lat:.4f}|{peril}")


def sop(lon: float, lat: float, peril: str) -> float | None:
    """Protection standard in return-period years, or None where unknown.

    None means "no FLOPROS value here", never "no protection". The caller must
    treat it as undefended and say so, which is what compute.py does.
    """
    rec = record(lon, lat, peril)
    return rec.get("sop_years") if rec else None


def coverage() -> dict:
    """Which points carry a real standard and which are left undefended.

    Surfaced in the provenance ledger so a reader can see that, say, Manila is
    modelled with no defences because FLOPROS has no entry for it, rather than
    because someone decided it had none.
    """
    defended, undefended = [], []
    for key, rec in _block().items():
        (defended if rec.get("sop_years") else undefended).append(key)
    return {
        "source": next(
            (r.get("source") for r in _block().values() if r.get("source")),
            "FLOPROS (Scussolini et al. 2016)",
        ),
        "basis": "lower bound of the FLOPROS min/max range, the conservative end",
        "defended_points": len(defended),
        "undefended_points": len(undefended),
        "undefended": sorted(undefended),
    }


def demo() -> None:
    if hz.status()["degraded"]:
        print("protection.py self-check: DEGRADED (no hazard cache)")
        assert sop(4.4034, 51.9244, "inundation_coastal") is None
        return

    blk = _block()
    assert blk, "a warmed cache must carry a protection block"

    for key, rec in blk.items():
        v = rec.get("sop_years")
        assert v is None or 1.0 <= v <= 100_000.0, f"{key}: implausible SOP {v}"
        assert rec.get("source"), f"{key}: a protection standard without a source"
        if v is None:
            assert "undefended" in rec.get("note", ""), f"{key}: silent missing SOP"

    # Wind is never defended by a flood standard, whatever the cache says.
    assert sop(4.4034, 51.9244, "wind") is None
    assert record(4.4034, 51.9244, "wind") is None

    # A point we hold nothing for reads as unknown, not as zero protection.
    assert sop(0.0, 0.0, "inundation_coastal") is None

    # The Dutch delta is the most defended coast on earth; if it comes back
    # unprotected the FLOPROS read has broken.
    nl = sop(4.4034, 51.9244, "inundation_coastal")
    assert nl is not None and nl >= 100, f"Rotterdam coastal SOP looks wrong: {nl}"

    cov = coverage()
    assert cov["defended_points"] + cov["undefended_points"] == len(blk)
    assert cov["defended_points"] > 0, "no asset got a real FLOPROS standard"
    print(f"protection.py self-check passed ({cov['defended_points']} defended, "
          f"{cov['undefended_points']} undefended of {len(blk)} points)")


if __name__ == "__main__":
    demo()
