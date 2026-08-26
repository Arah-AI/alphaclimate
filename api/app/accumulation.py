"""Correlated accumulation: what one event does to several assets at once.

The rest of the platform sums each asset's expected annual loss. Addition is
only the right operation when the losses are independent, and they are not: a
storm surge does not pick one terminal out of a delta. Summing independently
diversifies away exactly the thing that ends a portfolio, and it does it
silently, because every number downstream stays internally consistent.

The honest fix is not a covariance matrix. Nobody can estimate one for twelve
industrial sites from public data, and a fabricated one would be worse than the
independence it replaced because it would look like modelling. What is
defensible is a stated footprint radius per peril, taken from published event
geometry, and two bracketing assumptions computed over it:

  * comonotonic within a footprint -- one event, every asset in the disc hit at
    the same return period. This is an UPPER bound. Real events do not deliver
    their design intensity uniformly across a 300 km disc.
  * independent -- every asset its own generator, which is what the dashboard
    assumes today. This is a LOWER bound.

The truth is between them. The gap is the number worth selling; a single point
estimate inside it would be invention.

Aggregation across footprints uses the standard occurrence-exceedance
construction: independent Poisson generators, rates additive, so the annual
probability of at least one event exceeding a loss L is 1 - exp(-sum of rates
above L). Inverting that gives the portfolio OEP at a return period.

Loss maths is not re-implemented here. Hazard readings come from `hazard.read`,
loss curves from `engine.loss_curve` (via `compute._asset_perils`, which already
applies `protection.sop` and the regime classification), and interpolation from
`engine.loss_at_return_period`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

if not __package__:  # run as `python api/app/accumulation.py`
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "app"
    import app  # noqa: F401

from fastapi import APIRouter, HTTPException, Query

from . import compute
from . import curves as curve_lib
from . import hazard as hz
from . import portfolio as pf
from .engine import LossCurve, loss_at_return_period

router = APIRouter()

EARTH_RADIUS_KM = 6371.0088

# The return period the per-footprint correlated/independent comparison is
# quoted at. 1-in-100 is the market convention for a solvency-relevant loss.
REFERENCE_RP = 100.0
OEP_RETURN_PERIODS = (50.0, 100.0, 250.0)


# --------------------------------------------------------------------------
# footprint geometry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Footprint:
    """How far one event of this peril reaches, and why we believe that."""

    radius_km: float
    basis: str


# Radii are properties of the physics, not of this portfolio. They were chosen
# before the clusters were looked at and are not tuned to produce a headline.
# Each is the scale over which the hazard stays strongly correlated in the
# published literature, rounded to one significant figure because pretending to
# more precision than "a few hundred kilometres" would be false.
FOOTPRINTS: dict[str, Footprint] = {
    "inundation_riverine": Footprint(
        100.0,
        "Flood synchrony scale: annual-maximum river discharge stays strongly "
        "correlated between gauges out to roughly 100 km within a basin, and "
        "decorrelates beyond it (Berghuijs et al. 2019, GRL, 'Growing spatial "
        "scales of synchronous river flooding in Europe'). Fluvial flooding is "
        "basin-coherent, so the disc is a proxy for shared catchment, not for "
        "proximity as such.",
    ),
    "inundation_coastal": Footprint(
        200.0,
        "Storm-surge extremes stay correlated alongshore over a few hundred "
        "kilometres, because one low-pressure system and its wind fetch drive "
        "the whole stretch of coast (Haigh et al. 2016, Sci. Data, UK surge "
        "event footprints). 200 km is the conservative end of that range.",
    ),
    "wind:iris": Footprint(
        300.0,
        "Tropical cyclone damaging-wind footprint. The radius of gale-force "
        "(34 kt) winds in a mature cyclone is typically 150-300 km in IBTrACS "
        "R34; 300 km takes the wide end, so the disc covers the damaging "
        "swath rather than only the eyewall.",
    ),
    "wind:wisc": Footprint(
        600.0,
        "European windstorm footprint. Extratropical cyclones are an order of "
        "magnitude larger than tropical ones: WISC (Copernicus C3S) event "
        "footprints routinely span 1000-2000 km across Europe, so a 600 km "
        "radius is the corresponding half-width.",
    ),
}

BOUNDS_NOTE = (
    "Comonotonic is an UPPER bound: it hits every asset in the footprint at the "
    "same return period, which no real event does. Independent is a LOWER "
    "bound and is what the expected-annual-loss dashboard assumes today. The "
    "true portfolio loss sits between them. Neither figure is a best estimate, "
    "and no number in this payload claims to be one."
)


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def family_key(peril: str, variant: str) -> str | None:
    """Which footprint geometry applies. None means we refuse to guess.

    `wind` is two different phenomena in one peril name: IRIS is tropical
    cyclone, WISC is European windstorm, and their footprints differ by a
    factor of two. Keying on the variant keeps them apart instead of averaging
    a hurricane and a European gale into one radius.
    """
    if f"{peril}:{variant}" in FOOTPRINTS:
        return f"{peril}:{variant}"
    if peril in FOOTPRINTS:
        return peril
    return None


# --------------------------------------------------------------------------
# loss generators
# --------------------------------------------------------------------------

@dataclass
class EventCurve:
    """Loss against return period for one independent Poisson generator.

    Either one (asset, peril) pair under the independence assumption, or one
    whole footprint under the comonotonic assumption. Both are consumed the
    same way by the OEP aggregation, which is the point of the abstraction.
    """

    rps: list[float]
    losses: list[float]

    def __post_init__(self) -> None:
        # Loss must not fall as the event gets rarer. Damage curves are
        # monotone and intensities ascend, so this only bites on numerical
        # noise, but a non-monotone curve would silently invert the rate
        # interpolation below.
        run = 0.0
        fixed = []
        for x in self.losses:
            run = max(run, float(x))
            fixed.append(run)
        self.losses = fixed

    @property
    def max_loss(self) -> float:
        return self.losses[-1] if self.losses else 0.0

    def loss_at(self, rp: float) -> float:
        return loss_at_return_period(
            LossCurve(self.rps, [], [], self.losses, 0.0, 0.0, 0.0), rp
        )

    def rate_above(self, loss: float) -> float:
        """Annual rate of events causing more than `loss`.

        The exact inverse of `engine.loss_at_return_period`: that function is
        linear in log(return period) between knots, so this one is linear in
        log(rate) between the same knots. If the two disagreed, the OEP would
        not reproduce a single asset's own loss curve.
        """
        if not self.rps or self.max_loss <= 0:
            return 0.0
        # Leading zeros are real: a flood defence passes nothing below its
        # standard. The rate ceiling is the first return period that produces
        # any loss at all, not the shortest modelled one.
        first = next(i for i, x in enumerate(self.losses) if x > 0)
        if loss < self.losses[first]:
            return 1.0 / self.rps[first]
        if loss >= self.max_loss:
            return 0.0
        for i in range(first + 1, len(self.losses)):
            if loss < self.losses[i]:
                la, lb = self.losses[i - 1], self.losses[i]
                ra, rb = self.rps[i - 1], self.rps[i]
                if lb == la:
                    return 1.0 / rb
                w = (loss - la) / (lb - la)
                return 1.0 / math.exp(math.log(ra) + w * (math.log(rb) - math.log(ra)))
        return 0.0


def oep(generators: list[EventCurve], rp: float) -> float:
    """Occurrence exceedance loss: the loss whose event rate is 1 in `rp` years.

    Generators are independent Poisson processes, so their rates add and the
    aggregate rate above a loss L is the sum of the individual rates. We invert
    that by bisection because the rate function is piecewise but monotone.

    "1-in-rp" is the rate convention, 1/rp, not the annual-probability
    convention 1 - exp(-1/rp). They differ by half a percent at 1-in-100, but
    only the rate convention is the one `engine.loss_curve` already uses
    (`probs = 1/rp`). Mixing them would make a single-asset footprint disagree
    with that asset's own loss curve, which would read as an accumulation
    effect where there is none.

    Censored above: no generator produces more than its longest modelled return
    period, so a portfolio OEP at 1-in-250 can be pinned by a hazard layer that
    stops at 1-in-1000. That understates, never overstates.
    """
    if not generators or rp <= 1.0:
        return 0.0
    target = 1.0 / rp
    hi = sum(g.max_loss for g in generators)
    if hi <= 0:
        return 0.0
    if sum(g.rate_above(0.0) for g in generators) < target:
        return 0.0  # even a scratch is rarer than 1-in-rp across the book
    lo = 0.0
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if sum(g.rate_above(mid) for g in generators) >= target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------
# assembling the book
# --------------------------------------------------------------------------

@dataclass
class Unit:
    """One asset's exposure to one peril, with a usable loss curve."""

    asset: pf.Asset
    peril: str
    variant: str
    family: str
    curve: EventCurve


@dataclass
class Exclusion:
    """An (asset, peril) pair deliberately left out, and why.

    Never silently zeroed. A missing hazard reading and a genuine zero are
    different facts, and conflating them is how a model tells you a site is
    safe when it means it never looked.
    """

    asset_id: str
    asset_name: str
    peril: str
    reason: str
    kind: str  # no_reading | no_exposure | no_damage_curve | permanent_inundation

    def as_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "peril": self.peril,
            "kind": self.kind,
            "reason": self.reason,
        }


def _book(scenario: str) -> tuple[list[Unit], list[Exclusion]]:
    """Every modellable (asset, peril) pair, plus everything left out and why.

    Reuses `compute._asset_perils` so the loss curve here is bit-for-bit the
    one behind the dashboard's EAL: same damage curve selection, same FLOPROS
    standard of protection, same regime classification. Anything that function
    drops is re-derived here only to name the reason.
    """
    units: list[Unit] = []
    excluded: list[Exclusion] = []

    for a in pf.DEMO_ASSETS:
        results = {r.peril: r for r in compute._asset_perils(a, scenario)}
        for peril in hz.PERILS:
            r = results.get(peril)
            if r is not None and r.permanent:
                # Same exclusion compute.py applies to expected annual loss.
                # Standing water is a one-off write-down, not an event: there
                # is no storm to accumulate across a footprint.
                excluded.append(Exclusion(
                    a.id, a.name, peril, r.regime.reason, "permanent_inundation"))
                continue
            if r is not None:
                fam = family_key(peril, r.reading.variant)
                if fam is None:
                    excluded.append(Exclusion(
                        a.id, a.name, peril,
                        f"no published footprint radius for {peril}"
                        f"/{r.reading.variant}; refusing to invent one",
                        "no_footprint"))
                    continue
                units.append(Unit(a, peril, r.reading.variant, fam,
                                  EventCurve(list(r.lc.return_periods),
                                             list(r.lc.losses))))
                continue

            # Dropped by _asset_perils. Say which of the three reasons it was.
            reading = hz.read(a.lon, a.lat, peril, scenario)
            if reading is None or not reading.return_periods:
                excluded.append(Exclusion(
                    a.id, a.name, peril,
                    "no hazard reading at this point: the cache holds nothing "
                    "for this location and peril, which is not the same as "
                    "zero hazard and is not treated as zero",
                    "no_reading"))
            elif max(reading.intensities) <= 0:
                excluded.append(Exclusion(
                    a.id, a.name, peril,
                    "hazard layer reads zero intensity at every return period: "
                    "genuine non-exposure, contributes nothing",
                    "no_exposure"))
            elif curve_lib.best(peril, a.region, a.occupancy) is None:
                excluded.append(Exclusion(
                    a.id, a.name, peril,
                    f"real hazard here (up to {max(reading.intensities):.4g} "
                    f"{reading.units}) but no defensible damage curve for "
                    f"{a.occupancy} in {a.region}: measurable and unpriced",
                    "no_damage_curve"))
            else:
                excluded.append(Exclusion(
                    a.id, a.name, peril, "dropped upstream, reason unclassified",
                    "unknown"))

    return units, excluded


# --------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------

@dataclass
class Cluster:
    """One event footprint: a disc of stated radius, and who is inside it."""

    family: str
    peril: str
    variant: str
    radius_km: float
    basis: str
    centre: pf.Asset
    members: list[Unit]
    inside_unmodelled: list[Exclusion] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.family}@{self.centre.id}"

    def comonotonic_curve(self) -> EventCurve:
        """One event, every member hit at the same return period.

        Built on the union of the members' return-period axes so a hazard layer
        that stops at 1-in-500 is not extended past its own range: outside a
        member's axis `loss_at` holds its loss flat, which is the same
        assumption `engine.loss_curve` makes for its tail.
        """
        rps = sorted({rp for m in self.members for rp in m.curve.rps})
        return EventCurve(rps, [sum(m.curve.loss_at(rp) for m in self.members)
                                for rp in rps])

    def correlated(self, rp: float) -> float:
        return sum(m.curve.loss_at(rp) for m in self.members)

    def independent(self, rp: float) -> float:
        return oep([m.curve for m in self.members], rp)


def _clusters(units: list[Unit], excluded: list[Exclusion]) -> list[Cluster]:
    """Greedy disjoint discs, one family at a time.

    Each candidate footprint is a disc centred on an asset, which is how
    Lloyd's realistic disaster scenarios are specified: an event lands
    somewhere and everything within R of it is hit. We take the disc with the
    largest correlated 1-in-100 loss, remove its members, and repeat.

    Discs are made disjoint on purpose. Overlapping footprints would count the
    same asset's rate twice in the portfolio OEP, which inflates the tail.

    ponytail: greedy, and epicentres are restricted to asset locations rather
    than a search over the plane, so a disc sitting between two assets and
    catching both is missed. Upgrade path is a licensed stochastic event set,
    at which point the disc heuristic goes away entirely.
    """
    out: list[Cluster] = []
    by_family: dict[str, list[Unit]] = {}
    for u in units:
        by_family.setdefault(u.family, []).append(u)

    for family, pool in sorted(by_family.items()):
        fp = FOOTPRINTS[family]
        remaining = list(pool)
        while remaining:
            best: tuple[float, int, list[Unit], Unit] | None = None
            for centre in remaining:
                members = [
                    u for u in remaining
                    if haversine_km(centre.asset.lon, centre.asset.lat,
                                    u.asset.lon, u.asset.lat) <= fp.radius_km
                ]
                loss = sum(m.curve.loss_at(REFERENCE_RP) for m in members)
                cand = (loss, len(members), members, centre)
                if best is None or cand[:2] > best[:2]:
                    best = cand
            _, _, members, centre = best
            peril = centre.peril

            # Assets sitting inside the same disc that we could not model.
            # They belong on the map: an unpriced 60 m/s wind reading next door
            # to a priced one is a hole in the answer, not an absence of risk.
            inside = [
                e for e in excluded
                if e.peril == peril
                and e.asset_id not in {m.asset.id for m in members}
                and (asset := pf.by_id(e.asset_id)) is not None
                and haversine_km(centre.asset.lon, centre.asset.lat,
                                 asset.lon, asset.lat) <= fp.radius_km
            ]

            out.append(Cluster(family, peril, centre.variant, fp.radius_km,
                               fp.basis, centre.asset, members, inside))
            done = {m.asset.id for m in members}
            remaining = [u for u in remaining if u.asset.id not in done]

    out.sort(key=lambda c: c.correlated(REFERENCE_RP), reverse=True)
    return out


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def _member_row(c: Cluster, u: Unit) -> dict:
    return {
        "asset_id": u.asset.id,
        "name": u.asset.name,
        "country": u.asset.country,
        "lon": u.asset.lon,
        "lat": u.asset.lat,
        "value": u.asset.value,
        "distance_km": round(haversine_km(c.centre.lon, c.centre.lat,
                                          u.asset.lon, u.asset.lat), 1),
        "loss_at_reference_rp": round(u.curve.loss_at(REFERENCE_RP), 2),
        "modelled": True,
    }


def _cluster_row(c: Cluster) -> dict:
    corr = c.correlated(REFERENCE_RP)
    ind = c.independent(REFERENCE_RP)
    return {
        "id": c.id,
        "family": c.family,
        "peril": c.peril,
        "variant": c.variant,
        "radius_km": c.radius_km,
        "radius_basis": c.basis,
        "centre": {"asset_id": c.centre.id, "name": c.centre.name,
                   "lon": c.centre.lon, "lat": c.centre.lat},
        "asset_count": len(c.members),
        "members": [_member_row(c, u) for u in
                    sorted(c.members, key=lambda u: u.curve.loss_at(REFERENCE_RP),
                           reverse=True)],
        "inside_but_unmodelled": [
            dict(e.as_dict(),
                 distance_km=round(haversine_km(
                     c.centre.lon, c.centre.lat,
                     pf.by_id(e.asset_id).lon, pf.by_id(e.asset_id).lat), 1))
            for e in c.inside_unmodelled
        ],
        "reference_rp": REFERENCE_RP,
        "correlated_loss": round(corr, 2),
        "independent_loss": round(ind, 2),
        "ratio": round(corr / ind, 4) if ind > 0 else None,
    }


def _degraded() -> dict:
    st = hz.status()
    return {
        "degraded": True,
        "reason": st.get("reason"),
        "clusters": [],
        "excluded": [],
        "note": "No hazard cache. Nothing is reported rather than zeros.",
    }


def accumulation(scenario: str = "ssp585") -> dict:
    """Correlated versus independent portfolio loss, with the gap left open."""
    if hz.status()["degraded"]:
        return _degraded()

    units, excluded = _book(scenario)
    clusters = _clusters(units, excluded)

    independent_gens = [u.curve for u in units]
    comonotonic_gens = [c.comonotonic_curve() for c in clusters]

    tail = []
    for rp in OEP_RETURN_PERIODS:
        ind = oep(independent_gens, rp)
        com = oep(comonotonic_gens, rp)
        tail.append({
            "return_period": rp,
            "independent": round(ind, 2),
            "comonotonic": round(com, 2),
            "uplift": round(com - ind, 2),
            "ratio": round(com / ind, 4) if ind > 0 else None,
        })

    multi = [c for c in clusters if len(c.members) > 1]
    worst = max(clusters, key=lambda c: c.correlated(REFERENCE_RP), default=None)

    # A ratio of 1.0 with no explanation reads as "accumulation does not
    # matter". Usually it means the book had nothing to accumulate, which is a
    # fact about the book. Say which it is, derived, not asserted.
    blind = [(c, e) for c in clusters for e in c.inside_unmodelled]
    if not multi:
        finding = (
            "No footprint contains two assets sharing a peril, so correlated "
            "accumulation adds nothing to this book. That is a property of "
            "these assets, not evidence that accumulation is unimportant."
        )
    elif all(r["uplift"] <= 0 for r in tail):
        finding = (
            f"{len(multi)} multi-asset footprint(s) found, but no uplift at any "
            f"reported return period: in each one only a single member carries "
            f"a non-zero loss, so there is nothing for the other to correlate "
            f"with."
        )
    else:
        peak = max(tail, key=lambda r: r["uplift"])
        finding = (
            f"Correlation adds ${peak['uplift']:,.0f} at 1-in-"
            f"{peak['return_period']:.0f} over the independent sum the "
            f"dashboard reports, across {len(multi)} multi-asset footprint(s)."
        )
    if blind:
        finding += (
            f" {len(blind)} asset(s) sit inside a footprint but could not be "
            f"modelled (see inside_but_unmodelled); each is a place this "
            f"number is too low by an unknown amount."
        )

    total_value = sum(a.value for a in pf.DEMO_ASSETS)
    no_reading = sorted({e.asset_id for e in excluded if e.kind == "no_reading"})
    fully_out = sorted(
        {a.id for a in pf.DEMO_ASSETS} - {u.asset.id for u in units}
    )

    return {
        "degraded": False,
        "scenario": scenario,
        "reference_rp": REFERENCE_RP,
        "bounds": BOUNDS_NOTE,
        "finding": finding,
        "method": {
            "clustering": "fixed-radius disc per peril, centred on an asset, "
                          "greedily made disjoint by correlated 1-in-100 loss",
            "aggregation": "independent Poisson generators, rates additive; "
                           "annual exceedance probability 1 - exp(-rate)",
            "correlation": "comonotonic within a footprint, independent across "
                           "footprints. No covariance matrix is estimated, "
                           "because none can be estimated honestly from twelve "
                           "points of public data.",
            "footprints": {k: {"radius_km": v.radius_km, "basis": v.basis}
                           for k, v in sorted(FOOTPRINTS.items())},
            "permanent_inundation": "excluded from event accumulation, exactly "
                                    "as compute.py excludes it from expected "
                                    "annual loss: standing water is not an event",
        },
        "coverage": {
            "assets_total": len(pf.DEMO_ASSETS),
            "assets_modelled": len({u.asset.id for u in units}),
            "asset_peril_pairs_modelled": len(units),
            "asset_peril_pairs_excluded": len(excluded),
            "assets_with_no_hazard_reading": no_reading,
            "assets_absent_from_every_footprint": fully_out,
            "portfolio_value": total_value,
        },
        "oep": tail,
        "worst_footprint": _cluster_row(worst) if worst else None,
        "clusters": [_cluster_row(c) for c in clusters],
        "multi_asset_clusters": len(multi),
        "excluded": [e.as_dict() for e in excluded],
        "provenance": {
            "hazard_source": hz.status()["source"],
            "hazard_citation": hz.CITATION,
            "engine_version": compute.ENGINE_VERSION,
            "aggregation": hz.aggregation(),
        },
    }


def cluster_geometry(scenario: str = "ssp585") -> dict:
    """Just the discs, for drawing. Radius and members, nothing computed."""
    if hz.status()["degraded"]:
        return _degraded()
    units, excluded = _book(scenario)
    return {
        "degraded": False,
        "scenario": scenario,
        "footprints": {k: {"radius_km": v.radius_km, "basis": v.basis}
                       for k, v in sorted(FOOTPRINTS.items())},
        "clusters": [
            {
                "id": c.id,
                "family": c.family,
                "peril": c.peril,
                "variant": c.variant,
                "radius_km": c.radius_km,
                "radius_basis": c.basis,
                "centre": {"asset_id": c.centre.id, "name": c.centre.name,
                           "lon": c.centre.lon, "lat": c.centre.lat},
                "members": [_member_row(c, u) for u in c.members],
                "inside_but_unmodelled": [e.as_dict() for e in c.inside_unmodelled],
            }
            for c in _clusters(units, excluded)
        ],
    }


def _check_scenario(scenario: str) -> None:
    if scenario not in {s["id"] for s in hz.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{scenario}'.")


@router.get("/api/accumulation")
def accumulation_endpoint(scenario: str = Query("ssp585")) -> dict:
    _check_scenario(scenario)
    return accumulation(scenario)


@router.get("/api/accumulation/clusters")
def clusters_endpoint(scenario: str = Query("ssp585")) -> dict:
    _check_scenario(scenario)
    return cluster_geometry(scenario)


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    # Geometry, against known distances.
    assert haversine_km(0, 0, 0, 0) == 0.0
    rot_ham = haversine_km(4.4034, 51.9244, 9.9937, 53.5511)
    assert 400 < rot_ham < 430, f"Rotterdam-Hamburg should be ~417 km, got {rot_ham}"
    cat_hcmc = haversine_km(106.7770, 10.7690, 106.7009, 10.7769)
    assert cat_hcmc < 10, f"Cat Lai and Thu Thiem are one delta, got {cat_hcmc} km"
    assert haversine_km(4.4034, 51.9244, 106.8806, -6.1045) > 10_000, \
        "Rotterdam and Jakarta must never share a footprint"

    assert family_key("wind", "iris") == "wind:iris"
    assert family_key("wind", "wisc") == "wind:wisc"
    assert FOOTPRINTS["wind:wisc"].radius_km > FOOTPRINTS["wind:iris"].radius_km, \
        "European windstorms are larger than tropical cyclones"
    assert family_key("inundation_coastal", "wri-coastal") == "inundation_coastal"
    assert family_key("hail", "anything") is None, "unknown perils must not guess"

    # EventCurve is the exact inverse of the engine's own interpolation.
    ec = EventCurve([10, 50, 100, 500], [0.0, 100.0, 400.0, 900.0])
    assert ec.loss_at(100) == 400.0
    assert abs(ec.rate_above(400.0 - 1e-9) - 1 / 100) < 1e-6, \
        "loss at 1-in-100 must read back as a rate of 1/100"
    assert abs(ec.rate_above(100.0 - 1e-9) - 1 / 50) < 1e-6
    assert ec.rate_above(1e12) == 0.0, "nothing exceeds the modelled maximum"
    assert ec.rate_above(0.0) == 1 / 50, \
        "a curve with no loss at 1-in-10 has its rate ceiling at 1-in-50"
    mid = ec.loss_at(75)
    assert 100.0 < mid < 400.0
    assert abs(1 / ec.rate_above(mid - 1e-9) - 75) < 1e-3, \
        "loss_at and rate_above must invert each other at an interpolated point"

    # Monotonicity is enforced on construction.
    assert EventCurve([10, 100], [50.0, 20.0]).losses == [50.0, 50.0]

    # A single generator's OEP is its own loss curve, exactly. If this drifts,
    # every single-asset footprint reports a phantom accumulation effect.
    solo = oep([ec], 100)
    assert abs(solo - 400.0) < 1e-6, \
        f"one generator's OEP must reproduce its own 1-in-100 loss, got {solo}"
    assert abs(oep([ec], 500) - 900.0) < 1e-6
    # A curve whose first loss sits exactly at the reference return period is
    # the edge case that exposed the probability-vs-rate mismatch.
    edge = EventCurve([2, 100, 1000], [0.0, 250.0, 400.0])
    assert abs(oep([edge], 100) - 250.0) < 1e-6, \
        "loss starting exactly at 1-in-100 must read back as 250, not zero"

    # Two identical generators. Comonotonic doubles the loss at every return
    # period; independent does not, because two independent 1-in-100 events do
    # not happen in the same year. This is the entire thesis of the module.
    pair_ind = oep([ec, ec], 100)
    pair_com = oep([EventCurve(ec.rps, [2 * x for x in ec.losses])], 100)
    assert abs(pair_com - 2 * 400.0) / 800.0 < 0.02
    assert pair_ind < pair_com, \
        "independence must under-report a correlated pair"
    assert pair_ind > 400.0, \
        "two assets are still worse than one even when independent"
    assert oep([], 100) == 0.0
    assert oep([ec], 1.0) == 0.0
    assert oep([EventCurve([10, 100], [0.0, 0.0])], 100) == 0.0, \
        "a curve with no loss anywhere must produce no OEP"

    if hz.status()["degraded"]:
        d = accumulation()
        assert d["degraded"] and d["clusters"] == []
        print("accumulation.py self-check: DEGRADED (no hazard cache)")
        return

    out = accumulation("ssp585")
    cov = out["coverage"]
    assert not out["degraded"]

    # Nothing is silently dropped: every (asset, peril) pair is either modelled
    # or named in the exclusion list with a reason.
    assert cov["asset_peril_pairs_modelled"] + cov["asset_peril_pairs_excluded"] \
        == len(pf.DEMO_ASSETS) * len(list(hz.PERILS)), \
        "an asset-peril pair went missing without an exclusion reason"
    assert all(e["reason"] and e["kind"] for e in out["excluded"])
    assert not any(e["kind"] == "unknown" for e in out["excluded"]), \
        "every exclusion must be classified"

    # The Limay site has no hazard reading at all. It must be listed, not zeroed.
    assert "manila-pp" in cov["assets_with_no_hazard_reading"], \
        "an asset with no reading anywhere must be reported as such"
    assert "manila-pp" not in {m["asset_id"] for c in out["clusters"]
                               for m in c["members"]}, \
        "an unread asset must not appear inside a footprint as a zero"

    # Permanent inundation is excluded from event accumulation, as in compute.py.
    perm = [e for e in out["excluded"] if e["kind"] == "permanent_inundation"]
    assert perm, "the standing-water exclusion is not firing at all"
    assert any(e["asset_id"] == "tj-priok" and e["peril"] == "inundation_coastal"
               for e in perm), \
        "Tanjung Priok coastal is standing water and must not accumulate"
    assert not any(m["asset_id"] == "tj-priok"
                   for c in out["clusters"] if c["peril"] == "inundation_coastal"
                   for m in c["members"])

    # Every cluster is internally consistent with its own radius.
    seen: dict[str, set[str]] = {}
    for c in out["clusters"]:
        assert c["radius_km"] == FOOTPRINTS[c["family"]].radius_km
        assert c["radius_basis"], "a radius without a justification is a guess"
        for m in c["members"] + c["inside_but_unmodelled"]:
            assert m["distance_km"] <= c["radius_km"] + 1e-6, \
                f"{m['asset_id']} is outside the {c['radius_km']} km footprint"
        # Footprints of one family must be disjoint or the OEP double-counts.
        ids = {m["asset_id"] for m in c["members"]}
        assert not (ids & seen.get(c["family"], set())), \
            f"{c['family']} footprints overlap: rates would be counted twice"
        seen.setdefault(c["family"], set()).update(ids)
        # Correlation cannot help.
        if c["ratio"] is not None:
            assert c["ratio"] >= 1.0 - 1e-6, \
                f"{c['id']}: correlated loss below independent, bounds inverted"
        assert c["correlated_loss"] >= c["independent_loss"] - 1e-6
        # A footprint containing one asset has nothing to accumulate. Any
        # uplift there is an artefact of the aggregation, not a finding.
        if c["asset_count"] == 1:
            assert abs(c["correlated_loss"] - c["independent_loss"]) < 1.0, (
                f"{c['id']} holds one asset but reports a "
                f"{c['ratio']}x accumulation effect"
            )

    # The bounds must bracket, at every return period, or the whole framing is
    # wrong. This is the assertion that matters most in the file.
    for row in out["oep"]:
        assert row["comonotonic"] >= row["independent"] - 1e-6, (
            f"at 1-in-{row['return_period']:.0f} the comonotonic upper bound "
            f"({row['comonotonic']:,.0f}) sits below the independent lower "
            f"bound ({row['independent']:,.0f})"
        )
        assert row["uplift"] >= -1e-6

    # OEP must rise with return period under both assumptions.
    for key in ("independent", "comonotonic"):
        vals = [r[key] for r in out["oep"]]
        assert vals == sorted(vals), f"{key} OEP falls as the event gets rarer"

    # And must stay inside the portfolio. A 1-in-250 loss above total value
    # would mean the damage fractions have broken out of [0, 1].
    assert out["oep"][-1]["comonotonic"] <= cov["portfolio_value"], \
        "a portfolio cannot lose more than it is worth"

    # The finding must not report an uplift the OEP table does not show.
    assert out["finding"], "a run without a stated finding is a run nobody reads"
    if all(r["uplift"] <= 0 for r in out["oep"]):
        assert "adds nothing" in out["finding"] or "no uplift" in out["finding"], \
            "zero uplift must be stated plainly, not left as a bare ratio of 1.0"

    worst = out["worst_footprint"]
    assert worst is not None
    assert worst["correlated_loss"] == max(c["correlated_loss"]
                                           for c in out["clusters"])

    geo = cluster_geometry("ssp585")
    assert len(geo["clusters"]) == len(out["clusters"])
    assert geo["footprints"] == out["method"]["footprints"]

    # Scenario must actually move the answer, or we are not reading scenarios.
    hist = accumulation("historical")
    assert hist["oep"][1]["comonotonic"] != out["oep"][1]["comonotonic"], \
        "historical and SSP5-8.5 gave the identical 1-in-100 loss"

    print(
        f"accumulation.py self-check passed "
        f"({len(out['clusters'])} footprints, {out['multi_asset_clusters']} "
        f"multi-asset, {cov['asset_peril_pairs_modelled']} pairs modelled, "
        f"{cov['asset_peril_pairs_excluded']} excluded; 1-in-100 independent "
        f"${out['oep'][1]['independent']/1e6:.1f}m vs comonotonic "
        f"${out['oep'][1]['comonotonic']/1e6:.1f}m)"
    )


if __name__ == "__main__":
    demo()
