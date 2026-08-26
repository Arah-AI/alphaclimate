"""Capital rationing across the portfolio: which interventions, given a budget.

Per-asset cost-benefit already exists in finance.appraise. It answers "is this
barrier worth building". It cannot answer the question a real capital committee
actually asks, which is "I have 40 million, where does it go".

That is a multiple-choice knapsack: at most one intervention per asset, chosen
to maximise net present value subject to a hard capex ceiling. Solved exactly by
dynamic programming rather than by a greedy benefit-cost sort, because greedy is
provably wrong on this problem and the failure is silent: it spends the budget
on a high-ratio small project and misses a larger one that fits.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

from fastapi import APIRouter, HTTPException, Query

from . import compute
from . import portfolio as pf

router = APIRouter()

# Budget is discretised for the DP. $50k buckets keep the table small while
# staying far finer than the precision of any input to this calculation.
BUCKET = 50_000.0


@dataclass
class Choice:
    asset_id: str
    asset_name: str
    option: str
    capex: float
    benefit_npv: float
    net_npv: float
    bcr: float
    payback_years: float | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    budget: float
    spent: float
    unspent: float
    total_net_npv: float
    total_benefit_npv: float
    choices: list[Choice] = field(default_factory=list)
    untreated: list[dict] = field(default_factory=list)
    method: str = ""
    caveat: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["portfolio_bcr"] = (
            round(self.total_benefit_npv / self.spent, 3) if self.spent > 0 else 0.0
        )
        return d


def _candidates(scenario: str, overrides: dict | None) -> dict[str, list[dict]]:
    """Appraised options per asset, reusing the existing per-asset appraisal.

    Deliberately not re-deriving the cost-benefit maths here: one implementation
    of a number, one place for it to be wrong.
    """
    out: dict[str, list[dict]] = {}
    for a in pf.DEMO_ASSETS:
        detail = compute.asset_detail(a.id, scenario, overrides)
        if detail is None:
            continue
        out[a.id] = detail.get("adaptation", [])
    return out


def optimise(
    budget: float,
    scenario: str = "ssp585",
    overrides: dict | None = None,
) -> Plan:
    """Choose at most one intervention per asset to maximise NPV within budget."""
    if budget < 0:
        raise ValueError("budget must not be negative")

    cands = _candidates(scenario, overrides)
    assets = {a.id: a for a in pf.DEMO_ASSETS}

    # Only options that create value are worth a slot. An option with negative
    # net NPV can never improve the plan, so excluding it shrinks the table
    # without changing the answer.
    items: list[tuple[str, list[dict]]] = []
    for aid, opts in cands.items():
        viable = [o for o in opts if o.get("net_npv", 0) > 0 and o.get("capex", 0) > 0]
        if viable:
            items.append((aid, viable))

    n_buckets = int(budget // BUCKET) + 1

    # dp[b] = best net NPV achievable with b buckets of capex.
    dp = [0.0] * n_buckets
    # back[i][b] = the option index chosen for item i at budget b, or None.
    back: list[list[int | None]] = []

    for _aid, opts in items:
        prev = dp[:]
        row: list[int | None] = [None] * n_buckets
        for b in range(n_buckets):
            best = prev[b]
            pick: int | None = None
            for oi, o in enumerate(opts):
                cost_b = int(round(o["capex"] / BUCKET))
                if cost_b <= b:
                    val = prev[b - cost_b] + o["net_npv"]
                    if val > best + 1e-9:
                        best, pick = val, oi
            dp[b] = best
            row[b] = pick
        back.append(row)

    # Walk the table back to recover which options were chosen.
    b = n_buckets - 1
    chosen: dict[str, dict] = {}
    for i in range(len(items) - 1, -1, -1):
        oi = back[i][b]
        if oi is not None:
            aid, opts = items[i]
            o = opts[oi]
            chosen[aid] = o
            b -= int(round(o["capex"] / BUCKET))

    choices = [
        Choice(
            asset_id=aid,
            asset_name=assets[aid].name,
            option=o["name"],
            capex=o["capex"],
            benefit_npv=o["benefit_npv"],
            net_npv=o["net_npv"],
            bcr=o["bcr"],
            payback_years=o.get("payback_years"),
        )
        for aid, o in chosen.items()
    ]
    choices.sort(key=lambda c: c.net_npv, reverse=True)

    spent = sum(c.capex for c in choices)
    untreated = [
        {
            "asset_id": aid,
            "asset_name": assets[aid].name,
            "reason": (
                "no intervention creates value at this asset"
                if aid not in {a for a, _ in items}
                else "did not fit the budget"
            ),
        }
        for aid in cands
        if aid not in chosen
    ]

    return Plan(
        budget=round(budget, 2),
        spent=round(spent, 2),
        unspent=round(budget - spent, 2),
        total_net_npv=round(sum(c.net_npv for c in choices), 2),
        total_benefit_npv=round(sum(c.benefit_npv for c in choices), 2),
        choices=choices,
        untreated=untreated,
        method=(
            f"Exact multiple-choice knapsack by dynamic programming, capex "
            f"discretised into ${BUCKET:,.0f} buckets. At most one intervention "
            f"per asset."
        ),
        caveat=(
            "Every figure inherits the engine's assumptions and its known "
            "limitations. The ranking between options is more robust than the "
            "absolute NPVs, because the errors are largely common to all of them."
        ),
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.get("/api/adaptation/optimise")
def optimise_route(
    budget: float = Query(..., ge=0, le=1e11, description="Capex ceiling"),
    scenario: str = Query("ssp585"),
) -> dict:
    try:
        return optimise(budget, scenario).as_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/adaptation/frontier")
def frontier_route(
    scenario: str = Query("ssp585"),
    steps: int = Query(12, ge=2, le=40),
    max_budget: float = Query(2.0e8, gt=0, le=1e11),
) -> dict:
    """Net NPV against budget: where more capex stops buying anything."""
    points = []
    for i in range(steps + 1):
        b = max_budget * i / steps
        p = optimise(b, scenario)
        points.append({
            "budget": round(b, 2),
            "spent": p.spent,
            "net_npv": p.total_net_npv,
            "assets_treated": len(p.choices),
        })
    return {"scenario": scenario, "points": points}


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    from . import hazard

    if hazard.status()["degraded"]:
        print("optimiser.py self-check: SKIPPED (hazard cache not warmed)")
        return

    zero = optimise(0.0)
    assert zero.choices == [], "a zero budget can fund nothing"
    assert zero.spent == 0.0 and zero.total_net_npv == 0.0

    small = optimise(5_000_000.0)
    assert small.spent <= 5_000_000.0 + 1e-6, "must never exceed the budget"

    big = optimise(500_000_000.0)
    assert big.spent <= 500_000_000.0 + 1e-6

    # More budget can never buy less value. This is the property that makes the
    # DP worth having over a greedy sort, so it is the one worth asserting.
    prev = -1.0
    for b in (0, 2e6, 1e7, 5e7, 2e8, 5e8):
        v = optimise(float(b)).total_net_npv
        assert v >= prev - 1e-6, f"net NPV fell from {prev:,.0f} to {v:,.0f} at {b:,.0f}"
        prev = v

    # At most one intervention per asset.
    ids = [c.asset_id for c in big.choices]
    assert len(ids) == len(set(ids)), "an asset cannot be treated twice"

    # Every chosen option must create value and be affordable on its own.
    for c in big.choices:
        assert c.net_npv > 0, f"{c.asset_id}: chose a value-destroying option"
        assert c.capex <= big.budget

    # Accounting must reconcile.
    assert abs(big.spent - sum(c.capex for c in big.choices)) < 1e-6
    assert abs(big.unspent - (big.budget - big.spent)) < 1e-6
    assert abs(big.total_net_npv - sum(c.net_npv for c in big.choices)) < 1e-6

    # Everything is accounted for: treated plus untreated equals the portfolio.
    assert len(big.choices) + len(big.untreated) == len(pf.DEMO_ASSETS)

    # A negative budget is a caller error, not a silent zero.
    try:
        optimise(-1.0)
        raise AssertionError("negative budget must raise")
    except ValueError:
        pass

    print(
        f"optimiser.py self-check passed "
        f"(at $500m: {len(big.choices)} assets treated, "
        f"${big.spent/1e6:.1f}m spent, ${big.total_net_npv/1e6:.1f}m net NPV)"
    )


if __name__ == "__main__":
    demo()
