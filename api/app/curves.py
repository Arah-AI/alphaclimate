"""Damage-curve selection.

Picks the vulnerability function for an (hazard, region, occupancy) triple, and
returns the *alternatives* alongside it. The alternatives are the point: running
the same asset through every defensible curve is how the disagreement number on
the dashboard gets produced.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

_DATA = os.environ.get(
    "ALPHACLIMATE_CURVES",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "damage_curves.json"),
)


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    with open(os.path.abspath(_DATA), "r") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def curves() -> list[dict]:
    return load().get("curves", [])


@lru_cache(maxsize=1)
def gaps() -> list[Any]:
    return load().get("gaps", [])


def _norm(s: str | None) -> str:
    return (s or "").strip().lower().replace(" ", "_")


# Hazard groups in the store map onto the two curve families we hold.
HAZARD_FAMILY = {
    "inundation": "flood",
    "inundation_riverine": "flood",
    "inundation_coastal": "flood",
    "combined_flood": "flood",
    "wind": "wind",
}


def family_for(peril: str) -> str | None:
    return HAZARD_FAMILY.get(peril)


def candidates(peril: str, region: str, occupancy: str) -> list[dict]:
    """Every defensible curve for this asset, best first.

    Order is deliberate: the regional JRC curve leads because it is the most
    specific citable source, then the JRC global curve, then a same-occupancy
    curve from another family (HAZUS). We never silently fall through to an
    unrelated occupancy.
    """
    fam = family_for(peril)
    if not fam:
        return []

    reg, occ = _norm(region), _norm(occupancy)
    pool = [c for c in curves() if c.get("hazard") == fam]

    def by_id_prefix(prefix: str) -> list[dict]:
        return [c for c in pool if c["id"].startswith(prefix)]

    ranked: list[dict] = []

    # 1. JRC, exact region and occupancy.
    ranked += [
        c for c in by_id_prefix("jrc_")
        if _norm(c.get("region")) == reg and _norm(c.get("occupancy")) == occ
    ]
    # 2. JRC global, same occupancy.
    ranked += [
        c for c in by_id_prefix("jrc_")
        if _norm(c.get("region")) == "global" and _norm(c.get("occupancy")) == occ
    ]
    # 3. Anything else with the same occupancy, structure damage only, so we do
    #    not mix a contents curve into a building-value calculation.
    ranked += [
        c for c in pool
        if _norm(c.get("occupancy")) == occ
        and c.get("damage_basis") in (None, "structure", "total")
        and c not in ranked
    ]

    # stable dedupe
    seen: set[str] = set()
    out: list[dict] = []
    for c in ranked:
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def best(peril: str, region: str, occupancy: str) -> dict | None:
    c = candidates(peril, region, occupancy)
    return c[0] if c else None


def alternates(peril: str, region: str, occupancy: str, limit: int = 3) -> list[dict]:
    """The set the engine runs to measure vulnerability-driven spread."""
    return candidates(peril, region, occupancy)[:limit]


def citation(curve: dict) -> str:
    src = curve.get("source", "unattributed")
    if curve.get("proxy_for"):
        return f"{src} [proxy for {curve['proxy_for']}]"
    return src


def demo() -> None:
    data = load()
    assert data.get("curves"), "curve file must not be empty"
    assert len(curves()) > 100, f"expected a full curve set, got {len(curves())}"

    # The demo portfolio's real combinations must all resolve.
    combos = [
        ("inundation_riverine", "Asia", "Industrial"),
        ("inundation_riverine", "Asia", "Commercial"),
        ("inundation_riverine", "Asia", "Transport"),
        ("inundation_riverine", "Asia", "Infrastructure"),
        ("inundation_riverine", "Europe", "Industrial"),
        ("inundation_riverine", "Europe", "Commercial"),
        ("inundation_riverine", "North America", "Industrial"),
        ("combined_flood", "Asia", "Industrial"),
    ]
    for peril, region, occ in combos:
        c = best(peril, region, occ)
        assert c is not None, f"no curve for {peril}/{region}/{occ}"
        assert c["x"] and c["y"], f"empty curve for {peril}/{region}/{occ}"
        assert len(c["x"]) == len(c["y"])
        assert citation(c), "every curve must carry a citation"

    # Regional specificity: the Asian curve must win for an Asian asset.
    asia = best("inundation_riverine", "Asia", "Industrial")
    assert "asia" in asia["id"], f"expected the Asian curve, got {asia['id']}"

    # Alternatives must be distinct and include the best one first.
    alts = alternates("inundation_riverine", "Asia", "Industrial", limit=3)
    assert alts[0]["id"] == asia["id"]
    assert len({a["id"] for a in alts}) == len(alts), "alternatives must be distinct"
    assert len(alts) >= 2, "spread needs at least two curves to be meaningful"

    # A hazard with no curve family must return nothing rather than a wrong curve.
    assert family_for("drought") is None
    assert best("drought", "Asia", "Industrial") is None
    assert alternates("drought", "Asia", "Industrial") == []

    # An unknown occupancy must not silently borrow another one.
    assert best("inundation_riverine", "Asia", "Spaceport") is None

    print(f"curves.py self-check passed ({len(curves())} curves, {len(gaps())} gaps)")


if __name__ == "__main__":
    demo()
