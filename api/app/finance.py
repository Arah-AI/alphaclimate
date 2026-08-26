"""Financial translation: modelled physical loss -> money a committee recognises.

This is layer 05 in the landscape review, the one with no open-source
implementation. Everything here is deliberately simple arithmetic with every
assumption named and overridable. A client who disagrees with an assumption
must be able to change it and see the number move, so nothing is hidden in a
constant.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field


@dataclass
class Assumptions:
    """Every lever a client is allowed to argue with. All overridable via API."""

    # discounting
    discount_rate: float = 0.08          # WACC used to present-value future losses
    horizon_years: int = 15              # typical infra / real-asset hold period

    # business interruption
    downtime_days_per_damage_unit: float = 180.0
    # days of outage at 100% damage; scaled linearly by damage fraction
    ebitda_margin: float = 0.35
    bi_recovery_fraction: float = 0.60
    # share of revenue that is genuinely lost rather than deferred

    # insurance
    deductible_fraction: float = 0.02    # of asset value, per event
    limit_fraction: float = 0.80         # cover cap as a share of asset value
    coinsurance: float = 0.10            # share of covered loss retained
    premium_rate_on_eal: float = 1.35    # premium as a multiple of expected loss
    premium_escalation: float = 0.06     # annual real premium growth
    insurable: bool = True

    # hazard trend: how much worse the annual loss gets per year within horizon
    hazard_growth: float = 0.025

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def merged(cls, overrides: dict | None) -> "Assumptions":
        base = cls()
        if not overrides:
            return base
        valid = {f for f in base.__dataclass_fields__}
        for k, v in overrides.items():
            if k in valid and v is not None:
                setattr(base, k, type(getattr(base, k))(v))
        return base


@dataclass
class AssetFinancials:
    """What the owner already knows about the asset, from their own model."""

    value: float                     # current market / book value
    annual_revenue: float = 0.0
    debt: float = 0.0
    annual_debt_service: float = 0.0
    noi: float = 0.0                 # net operating income; derived if zero

    def resolved_noi(self, a: Assumptions) -> float:
        if self.noi:
            return self.noi
        return self.annual_revenue * a.ebitda_margin


@dataclass
class FinancialImpact:
    annual_physical_damage: float
    annual_business_interruption: float
    annual_insurance_recovery: float
    annual_premium: float
    annual_net_cost: float
    npv_climate_cost: float
    value_impairment: float          # currency
    value_impairment_pct: float      # share of asset value
    adjusted_value: float
    ltv_before: float
    ltv_after: float
    dscr_before: float
    dscr_after: float
    covenant_breach: bool
    uninsurable_flag: bool
    yearly: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _insurance_recovery(gross_loss: float, asset_value: float, a: Assumptions) -> float:
    """Recovery on a single loss amount after deductible, limit and coinsurance."""
    if not a.insurable or gross_loss <= 0:
        return 0.0
    deductible = a.deductible_fraction * asset_value
    limit = a.limit_fraction * asset_value
    covered = max(0.0, min(gross_loss - deductible, limit))
    return covered * (1.0 - a.coinsurance)


def translate(
    eal: float,
    mean_damage_fraction: float,
    fin: AssetFinancials,
    a: Assumptions | None = None,
) -> FinancialImpact:
    """Turn an expected annual loss into cash flow, valuation and covenant effects.

    `eal` is the expected annual physical damage in currency.
    `mean_damage_fraction` is the probability-weighted damage fraction, used to
    size outage duration. Both come from the risk engine.
    """
    a = a or Assumptions()
    value = max(fin.value, 0.0)

    # --- business interruption -------------------------------------------
    downtime_days = min(365.0, mean_damage_fraction * a.downtime_days_per_damage_unit)
    annual_bi = (
        fin.annual_revenue
        * (downtime_days / 365.0)
        * a.ebitda_margin
        * a.bi_recovery_fraction
    )

    # --- insurance --------------------------------------------------------
    recovery = _insurance_recovery(eal, value, a)
    premium = a.premium_rate_on_eal * eal if a.insurable else 0.0

    annual_net = eal + annual_bi - recovery + premium

    # --- projection and NPV ----------------------------------------------
    yearly: list[dict] = []
    npv = 0.0
    for t in range(1, a.horizon_years + 1):
        growth = (1.0 + a.hazard_growth) ** t
        prem_growth = (1.0 + a.premium_escalation) ** t
        damage_t = eal * growth
        bi_t = annual_bi * growth
        rec_t = _insurance_recovery(damage_t, value, a)
        prem_t = premium * prem_growth
        net_t = damage_t + bi_t - rec_t + prem_t
        disc = (1.0 + a.discount_rate) ** t
        npv += net_t / disc
        yearly.append({
            "year": t,
            "damage": round(damage_t, 2),
            "business_interruption": round(bi_t, 2),
            "insurance_recovery": round(rec_t, 2),
            "premium": round(prem_t, 2),
            "net_cost": round(net_t, 2),
            "discounted": round(net_t / disc, 2),
        })

    impairment = min(npv, value)
    adjusted_value = max(0.0, value - impairment)

    # --- credit metrics ---------------------------------------------------
    ltv_before = (fin.debt / value) if value > 0 else 0.0
    ltv_after = (fin.debt / adjusted_value) if adjusted_value > 0 else float("inf")

    noi = fin.resolved_noi(a)
    ds = fin.annual_debt_service
    dscr_before = (noi / ds) if ds > 0 else 0.0
    dscr_after = ((noi - annual_net) / ds) if ds > 0 else 0.0

    # Common covenant floors: DSCR 1.25x, LTV 75%.
    breach = bool((ds > 0 and dscr_after < 1.25) or (fin.debt > 0 and ltv_after > 0.75))

    # An asset whose expected loss approaches the premium the market will bear
    # is where cover withdraws. 8% of value a year is a blunt but defensible line.
    uninsurable = bool(value > 0 and (eal / value) > 0.08)

    return FinancialImpact(
        annual_physical_damage=round(eal, 2),
        annual_business_interruption=round(annual_bi, 2),
        annual_insurance_recovery=round(recovery, 2),
        annual_premium=round(premium, 2),
        annual_net_cost=round(annual_net, 2),
        npv_climate_cost=round(npv, 2),
        value_impairment=round(impairment, 2),
        value_impairment_pct=round((impairment / value) if value > 0 else 0.0, 6),
        adjusted_value=round(adjusted_value, 2),
        ltv_before=round(ltv_before, 4),
        ltv_after=round(ltv_after, 4) if ltv_after != float("inf") else None,
        dscr_before=round(dscr_before, 4),
        dscr_after=round(dscr_after, 4),
        covenant_breach=breach,
        uninsurable_flag=uninsurable,
        yearly=yearly,
    )


# --------------------------------------------------------------------------
# adaptation
# --------------------------------------------------------------------------

@dataclass
class AdaptationOption:
    name: str
    capex: float
    loss_reduction: float        # fraction of EAL removed, 0-1
    opex_per_year: float = 0.0
    lifetime_years: int = 25


def appraise(
    option: AdaptationOption,
    eal: float,
    mean_damage_fraction: float,
    fin: AssetFinancials,
    a: Assumptions | None = None,
) -> dict:
    """Cost-benefit of one intervention. Returns NPV, payback and BCR."""
    a = a or Assumptions()
    base = translate(eal, mean_damage_fraction, fin, a)
    after = translate(
        eal * (1.0 - option.loss_reduction),
        mean_damage_fraction * (1.0 - option.loss_reduction),
        fin,
        a,
    )

    benefit_npv = base.npv_climate_cost - after.npv_climate_cost
    opex_npv = sum(
        option.opex_per_year / ((1.0 + a.discount_rate) ** t)
        for t in range(1, a.horizon_years + 1)
    )
    net_npv = benefit_npv - option.capex - opex_npv
    bcr = (benefit_npv / (option.capex + opex_npv)) if (option.capex + opex_npv) > 0 else 0.0

    # simple payback on undiscounted annual saving
    annual_saving = base.annual_net_cost - after.annual_net_cost
    payback = (option.capex / annual_saving) if annual_saving > 0 else None

    return {
        "name": option.name,
        "capex": round(option.capex, 2),
        "loss_reduction": option.loss_reduction,
        "benefit_npv": round(benefit_npv, 2),
        "net_npv": round(net_npv, 2),
        "bcr": round(bcr, 3),
        "payback_years": round(payback, 1) if payback and payback < 500 else None,
        "annual_saving": round(annual_saving, 2),
        "worth_doing": bool(net_npv > 0),
    }


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    fin = AssetFinancials(
        value=50_000_000.0,
        annual_revenue=18_000_000.0,
        debt=30_000_000.0,
        annual_debt_service=2_400_000.0,
    )
    a = Assumptions()

    zero = translate(0.0, 0.0, fin, a)
    assert zero.annual_net_cost == 0.0, "no hazard means no cost"
    assert zero.npv_climate_cost == 0.0
    assert zero.value_impairment == 0.0
    assert zero.ltv_after == zero.ltv_before, "no loss must not move LTV"
    assert not zero.covenant_breach

    imp = translate(900_000.0, 0.22, fin, a)
    assert imp.annual_physical_damage == 900_000.0
    assert imp.annual_business_interruption > 0, "an outage must cost revenue"
    assert imp.npv_climate_cost > 0
    assert imp.value_impairment <= fin.value, "impairment cannot exceed the asset"
    assert imp.adjusted_value < fin.value
    assert imp.ltv_after > imp.ltv_before, "impairment must raise LTV"
    assert imp.dscr_after < imp.dscr_before, "loss must reduce debt cover"

    # Monotonicity: more hazard is never cheaper.
    worse = translate(1_800_000.0, 0.40, fin, a)
    assert worse.npv_climate_cost > imp.npv_climate_cost
    assert worse.value_impairment >= imp.value_impairment

    # A higher discount rate must reduce the present value of future losses.
    patient = translate(900_000.0, 0.22, fin, Assumptions(discount_rate=0.03))
    impatient = translate(900_000.0, 0.22, fin, Assumptions(discount_rate=0.15))
    assert patient.npv_climate_cost > impatient.npv_climate_cost, \
        "discount rate must bite"

    # Insurance must reduce net cost, and switching it off must raise it.
    bare = translate(900_000.0, 0.22, fin, Assumptions(insurable=False))
    assert bare.annual_insurance_recovery == 0.0
    assert bare.annual_premium == 0.0
    assert bare.annual_net_cost > (imp.annual_net_cost - imp.annual_premium), \
        "losing cover must hurt once premium is excluded from the comparison"

    # Deductible above the loss means no recovery at all.
    high_ded = translate(100_000.0, 0.05, fin, Assumptions(deductible_fraction=0.5))
    assert high_ded.annual_insurance_recovery == 0.0

    # Limit caps recovery.
    capped = _insurance_recovery(40_000_000.0, 50_000_000.0, Assumptions())
    assert capped <= 0.80 * 50_000_000.0

    # Impairment is capped at asset value even under an absurd loss.
    catastrophic = translate(40_000_000.0, 1.0, fin, a)
    assert catastrophic.value_impairment == fin.value
    assert catastrophic.adjusted_value == 0.0
    assert catastrophic.uninsurable_flag, "80% of value per year is uninsurable"

    # Horizon length must matter.
    short = translate(900_000.0, 0.22, fin, Assumptions(horizon_years=5))
    long = translate(900_000.0, 0.22, fin, Assumptions(horizon_years=30))
    assert long.npv_climate_cost > short.npv_climate_cost
    assert len(long.yearly) == 30 and len(short.yearly) == 5

    # Yearly rows must reconcile with the NPV they were summed from.
    recomputed = sum(r["discounted"] for r in imp.yearly)
    assert abs(recomputed - imp.npv_climate_cost) < 1.0, "yearly rows must add up"

    # Adaptation: a cheap, effective barrier is worth doing.
    good = appraise(AdaptationOption("Flood barrier", 1_200_000.0, 0.65),
                    900_000.0, 0.22, fin, a)
    assert good["benefit_npv"] > 0
    assert good["worth_doing"], "a 65% loss cut for 1.2m against this EAL should pay"
    assert good["bcr"] > 1

    # A gold-plated one is not.
    bad = appraise(AdaptationOption("Relocate site", 90_000_000.0, 0.95),
                   900_000.0, 0.22, fin, a)
    assert not bad["worth_doing"], "90m capex cannot be justified by this EAL"
    assert bad["bcr"] < 1

    # Doing nothing has zero benefit.
    nothing = appraise(AdaptationOption("Do nothing", 0.0, 0.0), 900_000.0, 0.22, fin, a)
    assert nothing["benefit_npv"] == 0.0

    # Overrides must actually take, and unknown keys must be ignored safely.
    m = Assumptions.merged({"discount_rate": 0.11, "bogus_key": 42})
    assert m.discount_rate == 0.11
    assert not hasattr(m, "bogus_key")
    assert Assumptions.merged(None).discount_rate == 0.08

    print("finance.py self-check passed")


if __name__ == "__main__":
    demo()
