"""Risk engine: hazard exceedance curve + vulnerability curve -> loss distribution.

The one piece of real modelling in this codebase. Everything else is plumbing.

Method follows standard catastrophe-risk practice (see CLIMADA, Aznar-Siguan &
Bresch 2019): losses are computed at a set of return periods, converted to
exceedance probabilities, and integrated to give an expected annual loss.
We keep the tail term separate rather than dropping it, because dropping it is
one of the documented ways asset-level loss gets understated.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Sequence


# --------------------------------------------------------------------------
# vulnerability
# --------------------------------------------------------------------------

def interp_damage(x: float, curve_x: Sequence[float], curve_y: Sequence[float]) -> float:
    """Damage fraction at hazard intensity `x`, linearly interpolated.

    Below the first knot the damage is zero (no flood, no damage). Above the
    last knot the curve is held flat rather than extrapolated upward: a damage
    fraction is bounded by 1.0 and inventing a slope past the calibration range
    is exactly the silent extrapolation this product is meant to expose.
    """
    if not curve_x or x is None:
        return 0.0
    if x <= curve_x[0]:
        # Curves normally start at 0 intensity / 0 damage; guard anyway.
        return float(curve_y[0]) if x >= curve_x[0] else 0.0
    if x >= curve_x[-1]:
        return float(curve_y[-1])
    for i in range(1, len(curve_x)):
        if x <= curve_x[i]:
            x0, x1 = curve_x[i - 1], curve_x[i]
            y0, y1 = curve_y[i - 1], curve_y[i]
            if x1 == x0:
                return float(y1)
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(curve_y[-1])


def is_extrapolated(x: float, calibration_range: Sequence[float]) -> bool:
    """True when the intensity sits outside the curve's calibrated range."""
    if not calibration_range or len(calibration_range) != 2:
        return False
    return bool(x > calibration_range[1])


# --------------------------------------------------------------------------
# loss integration
# --------------------------------------------------------------------------

@dataclass
class LossCurve:
    """Losses at a set of return periods, plus the integrated summary."""

    return_periods: list[float]
    intensities: list[float]
    damage_fractions: list[float]
    losses: list[float]
    eal: float                      # expected annual loss, currency
    eal_body: float                 # contribution from the modelled RP range
    eal_tail: float                 # contribution beyond the longest RP
    extrapolated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def loss_curve(
    return_periods: Sequence[float],
    intensities: Sequence[float],
    curve_x: Sequence[float],
    curve_y: Sequence[float],
    asset_value: float,
    calibration_range: Sequence[float] | None = None,
    protection_rp: float | None = None,
) -> LossCurve:
    """Build the loss exceedance curve for one asset under one hazard.

    `return_periods` and `intensities` must be the same length and sorted by
    ascending return period (so ascending intensity in the normal case).

    `protection_rp` is a flood standard of protection in return-period years
    (FLOPROS). Events more frequent than the standard are held back by the
    defence and contribute no loss. Pass None where no standard is known: the
    asset is then treated as undefended, which is what the raw WRI layers
    already assume.
    """
    if len(return_periods) != len(intensities):
        raise ValueError("return_periods and intensities must be the same length")
    if not return_periods:
        return LossCurve([], [], [], [], 0.0, 0.0, 0.0, False)

    pairs = sorted(zip(return_periods, intensities), key=lambda p: p[0])
    rps = [float(p[0]) for p in pairs]
    ints = [0.0 if p[1] is None else float(p[1]) for p in pairs]

    fracs = [interp_damage(i, curve_x, curve_y) for i in ints]
    # A defence built to a 1-in-N standard passes nothing through below N.
    # ponytail: modelled as a step at the standard, so the trapezoid spanning
    # the standard carries half a step of loss it strictly should not. That
    # errs high, which is the right direction, and real defences do not fail
    # as a clean step anyway.
    if protection_rp:
        fracs = [0.0 if rp < protection_rp else f for rp, f in zip(rps, fracs)]
    losses = [f * asset_value for f in fracs]

    # exceedance probability of each return period
    probs = [1.0 / rp for rp in rps]

    # Integrate loss over exceedance probability. probs descend as rps ascend.
    # Anchor at p = min(1.0, 1/rp_min) with zero loss: events more frequent than
    # the shortest modelled return period are assumed to cause no damage, which
    # is the standard assumption and is conservative in the right direction.
    eal_body = 0.0
    anchor_p = min(1.0, 1.0 / rps[0]) if rps[0] > 0 else 1.0
    if anchor_p > probs[0]:
        eal_body += 0.5 * (0.0 + losses[0]) * (anchor_p - probs[0])
    for i in range(len(probs) - 1):
        eal_body += 0.5 * (losses[i] + losses[i + 1]) * (probs[i] - probs[i + 1])

    # Tail: beyond the longest modelled return period, hold the loss flat.
    # Reported separately so the user can see how much of the answer is tail.
    eal_tail = losses[-1] * probs[-1]

    extrap = any(is_extrapolated(i, calibration_range or []) for i in ints)

    return LossCurve(
        return_periods=rps,
        intensities=ints,
        damage_fractions=fracs,
        losses=losses,
        eal=eal_body + eal_tail,
        eal_body=eal_body,
        eal_tail=eal_tail,
        extrapolated=extrap,
    )


def loss_at_return_period(lc: LossCurve, rp: float) -> float:
    """Loss at an arbitrary return period, interpolated in log-RP space."""
    if not lc.return_periods:
        return 0.0
    if rp <= lc.return_periods[0]:
        return lc.losses[0]
    if rp >= lc.return_periods[-1]:
        return lc.losses[-1]
    import math
    for i in range(1, len(lc.return_periods)):
        if rp <= lc.return_periods[i]:
            a, b = lc.return_periods[i - 1], lc.return_periods[i]
            la, lb = lc.losses[i - 1], lc.losses[i]
            w = (math.log(rp) - math.log(a)) / (math.log(b) - math.log(a))
            return la + (lb - la) * w
    return lc.losses[-1]


# --------------------------------------------------------------------------
# inundation regime
# --------------------------------------------------------------------------

# A flood hazard layer answers "how deep, at this exceedance probability".
# When the shallowest, most frequent return period is already deep AND the
# curve barely rises out to the rarest return period, the layer is not
# describing floods at all: it is describing a pixel that is under water in the
# baseline state. Integrating an annual expected loss over standing water
# charges the same damage every year forever, which is the wrong quantity. The
# right answer is a one-off write-down.
PERMANENT_MIN_DEPTH = 0.25      # metres at the most frequent return period
PERMANENT_FLATNESS = 1.5        # rarest depth over most-frequent depth


@dataclass
class Regime:
    """Whether an inundation reading is an event distribution or standing water."""

    regime: str                 # "event" | "permanent"
    reason: str
    baseline_depth: float       # depth at the most frequent return period
    flatness: float             # rarest / most frequent

    @property
    def permanent(self) -> bool:
        return self.regime == "permanent"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["permanent"] = self.permanent
        return d


def classify_regime(
    return_periods: Sequence[float],
    intensities: Sequence[float],
) -> Regime:
    """Classify an inundation reading. Only meaningful for depth-valued hazards."""
    if not return_periods or not intensities:
        return Regime("event", "no reading", 0.0, 0.0)

    pairs = sorted(zip(return_periods, intensities), key=lambda p: p[0])
    base = float(pairs[0][1] or 0.0)
    rare = float(pairs[-1][1] or 0.0)

    if base <= 0:
        return Regime("event", "dry at the most frequent return period", base, 0.0)

    flat = rare / base if base > 0 else float("inf")

    if base >= PERMANENT_MIN_DEPTH and flat < PERMANENT_FLATNESS:
        return Regime(
            "permanent",
            f"{base:.2f} m already present at 1 in {pairs[0][0]:.0f}, rising only "
            f"{flat:.2f}x out to 1 in {pairs[-1][0]:.0f}: standing water, not a "
            f"flood frequency distribution",
            base,
            flat,
        )
    return Regime(
        "event",
        f"depth rises {flat:.2f}x across the return-period range",
        base,
        flat,
    )


# --------------------------------------------------------------------------
# uncertainty
# --------------------------------------------------------------------------

@dataclass
class Spread:
    """Min / median / max across a set of runs, with the driver attribution."""

    low: float
    median: float
    high: float
    n: int
    by_driver: dict = field(default_factory=dict)

    @property
    def range_ratio(self) -> float:
        """High divided by low. 1.0 means perfect agreement."""
        return (self.high / self.low) if self.low > 0 else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["range_ratio"] = round(self.range_ratio, 2)
        return d


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def spread(values: list[float], drivers: list[dict] | None = None) -> Spread:
    """Summarise a set of EALs produced under different modelling choices.

    `drivers` is a parallel list of dicts describing what varied in each run
    (e.g. {"scenario": "ssp585", "curve": "jrc_asia_commercial"}). We attribute
    the spread to each driver by measuring how much of the total range that
    driver's own variation accounts for, holding nothing else fixed. This is a
    first-order attribution, not a Sobol decomposition.
    """
    if not values:
        return Spread(0.0, 0.0, 0.0, 0)

    sp = Spread(low=min(values), median=_median(values), high=max(values), n=len(values))

    if drivers and len(drivers) == len(values):
        total_range = sp.high - sp.low
        keys = sorted({k for d in drivers for k in d})
        for key in keys:
            groups: dict[str, list[float]] = {}
            for d, v in zip(drivers, values):
                groups.setdefault(str(d.get(key)), []).append(v)
            # spread of the group means attributable to this driver
            means = [sum(g) / len(g) for g in groups.values() if g]
            drv_range = (max(means) - min(means)) if len(means) > 1 else 0.0
            share = (drv_range / total_range) if total_range > 0 else 0.0
            sp.by_driver[key] = {
                "range": round(drv_range, 2),
                "share": round(min(share, 1.0), 4),
                "levels": len(groups),
            }
        # ponytail: first-order attribution, shares need not sum to 1.
        # Upgrade to a variance-based Sobol decomposition if clients push back.
    return sp


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    # A curve where 1 m of water destroys half the asset.
    cx = [0.0, 0.5, 1.0, 2.0, 4.0, 6.0]
    cy = [0.0, 0.25, 0.50, 0.70, 0.90, 1.00]

    assert interp_damage(0.0, cx, cy) == 0.0
    assert interp_damage(1.0, cx, cy) == 0.50
    assert abs(interp_damage(0.75, cx, cy) - 0.375) < 1e-9, "linear interpolation"
    assert interp_damage(99.0, cx, cy) == 1.00, "held flat above last knot, never >1"
    assert interp_damage(-5.0, cx, cy) == 0.0

    rps = [10, 25, 50, 100, 250, 500]
    ints = [0.0, 0.3, 0.8, 1.5, 2.6, 3.4]
    lc = loss_curve(rps, ints, cx, cy, asset_value=10_000_000.0,
                    calibration_range=[0.0, 6.0])

    assert len(lc.losses) == len(rps)
    assert lc.losses == sorted(lc.losses), "loss must increase with return period"
    assert lc.eal > 0, "a flooding asset must have a positive EAL"
    assert abs(lc.eal - (lc.eal_body + lc.eal_tail)) < 1e-6
    assert lc.eal < lc.losses[-1], "EAL cannot exceed the worst modelled loss"
    assert not lc.extrapolated, "3.4 m is inside a 0-6 m calibration range"

    # An asset that never floods has zero EAL, not a small positive one.
    dry = loss_curve(rps, [0.0] * 6, cx, cy, asset_value=10_000_000.0)
    assert dry.eal == 0.0, f"dry asset should have zero EAL, got {dry.eal}"

    # Doubling asset value doubles the loss, exactly.
    twice = loss_curve(rps, ints, cx, cy, asset_value=20_000_000.0)
    assert abs(twice.eal - 2 * lc.eal) < 1e-6, "EAL must be linear in asset value"

    # Extrapolation flag fires outside the calibration range.
    deep = loss_curve(rps, [0, 1, 2, 4, 7, 9], cx, cy, 1e6, calibration_range=[0.0, 6.0])
    assert deep.extrapolated, "9 m is outside a 0-6 m calibration range"

    # RP interpolation is monotone and bracketed.
    l100 = loss_at_return_period(lc, 100)
    assert abs(l100 - lc.losses[3]) < 1e-6, "exact RP hit returns the exact loss"
    l75 = loss_at_return_period(lc, 75)
    assert lc.losses[2] <= l75 <= lc.losses[3], "interpolated loss sits between knots"

    # Unsorted input must still work.
    shuffled = loss_curve([100, 10, 50, 25, 500, 250],
                          [1.5, 0.0, 0.8, 0.3, 3.4, 2.6], cx, cy, 10_000_000.0)
    assert abs(shuffled.eal - lc.eal) < 1e-6, "input order must not change the answer"

    # Flood defences: a 1-in-100 standard zeroes everything more frequent than
    # a 1-in-100 event, and must cut the EAL hard without touching the tail.
    defended = loss_curve(rps, ints, cx, cy, 10_000_000.0, protection_rp=100)
    assert defended.losses[:3] == [0.0, 0.0, 0.0], "sub-SOP events must be held back"
    assert defended.losses[3:] == lc.losses[3:], "events past the SOP are unchanged"
    assert defended.eal < 0.5 * lc.eal, "a 1-in-100 defence must cut EAL sharply"
    assert defended.eal_tail == lc.eal_tail, "the tail sits past the SOP, untouched"
    assert loss_curve(rps, ints, cx, cy, 1e7, protection_rp=None).eal == lc.eal, \
        "no known standard means undefended, identical to before"
    assert loss_curve(rps, ints, cx, cy, 1e7, protection_rp=1e6).eal == 0.0, \
        "a standard beyond every modelled RP leaves no loss at all"
    assert defended.eal > 0, "a 1-in-100 defence is not immunity"

    # Regime classification: the Jakarta signature must read as permanent.
    jakarta = classify_regime([2, 5, 10, 25, 50, 100, 250, 500, 1000],
                              [1.871, 1.90, 1.94, 1.99, 2.03, 2.07, 2.11, 2.14, 2.156])
    assert jakarta.permanent, "flat, deep curve must classify as standing water"
    assert "standing water" in jakarta.reason
    assert abs(jakarta.baseline_depth - 1.871) < 1e-9

    # A normal flood curve must not be misread as permanent.
    normal = classify_regime(rps, ints)
    assert not normal.permanent, f"event curve misclassified: {normal.reason}"

    # A dry asset is an event regime with no depth, never permanent.
    dry_r = classify_regime(rps, [0.0] * 6)
    assert not dry_r.permanent and dry_r.baseline_depth == 0.0

    # Shallow but flat is NOT permanent: a few centimetres of nuisance water
    # everywhere is not an asset sitting below sea level.
    shallow = classify_regime([2, 1000], [0.05, 0.06])
    assert not shallow.permanent, "shallow flat water must not trigger a write-down"

    # Deep but steeply rising is a real flood distribution.
    steep = classify_regime([2, 1000], [0.4, 3.2])
    assert not steep.permanent, "a steep curve is an event regime"

    assert classify_regime([], []).regime == "event"

    # Spread attribution.
    vals = [100.0, 150.0, 200.0, 260.0]
    drv = [{"scenario": "ssp126", "curve": "a"}, {"scenario": "ssp126", "curve": "b"},
           {"scenario": "ssp585", "curve": "a"}, {"scenario": "ssp585", "curve": "b"}]
    sp = spread(vals, drv)
    assert sp.low == 100.0 and sp.high == 260.0
    assert sp.median == 175.0
    assert sp.range_ratio == 2.6
    assert sp.by_driver["scenario"]["share"] > sp.by_driver["curve"]["share"], \
        "scenario varies more than curve here, attribution must reflect that"

    assert spread([]).n == 0
    print("engine.py self-check passed")


if __name__ == "__main__":
    demo()
