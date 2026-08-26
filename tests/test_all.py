"""Runs every module's own self-check.

The checks live next to the code they test, in a demo() function, so they can be
run standalone during development. This file exists so CI runs them all at once.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app import curves, engine, finance, hazard, portfolio, protection  # noqa: E402


def test_engine():
    engine.demo()


def test_finance():
    finance.demo()


def test_curves():
    curves.demo()


def test_portfolio():
    portfolio.demo()


def test_hazard():
    hazard.demo()


def test_protection():
    protection.demo()


def test_compute_plausible():
    """The portfolio EAL must stay inside the physically plausible band."""
    from app import compute
    if hazard.status()["degraded"]:
        import pytest
        pytest.skip("hazard cache not warmed")
    compute.demo()


def test_compute_end_to_end():
    """A full run must produce a coherent portfolio summary, or skip if degraded."""
    from app import compute
    if hazard.status()["degraded"]:
        import pytest
        pytest.skip("hazard cache not warmed")

    s = compute.summary("ssp585")
    h = s["headline"]
    assert h["asset_count"] == len(portfolio.DEMO_ASSETS)
    assert h["total_value"] > 0
    assert h["eal"] >= 0
    assert 0 <= h["eal_pct_of_value"] <= 1, "EAL cannot exceed portfolio value"
    assert abs(h["insured_share"] + h["retained_share"] - 1.0) < 1e-6
    assert sum(h["bands"].values()) == h["asset_count"], "every asset needs a band"
    assert len(s["assets"]) == h["asset_count"]
    assert s["assets"] == sorted(s["assets"], key=lambda r: r["eal"], reverse=True)
    assert s["provenance"]["run_id"].startswith("run_")
    assert len(s["yearly"]) == s["provenance"]["horizon_years"]

    if h["eal"] > 0:
        assert abs(sum(p["share"] for p in s["perils"]) - 1.0) < 1e-4
        sp = h["eal_spread"]
        assert sp["low"] <= sp["median"] <= sp["high"], "spread must bracket the median"

    # Determinism: same inputs, same run id.
    assert compute.summary("ssp585")["provenance"]["run_id"] == s["provenance"]["run_id"]

    # An assumption override must move the answer and change the run id.
    alt = compute.summary("ssp585", {"discount_rate": 0.15})
    assert alt["provenance"]["run_id"] != s["provenance"]["run_id"]
    if h["npv_climate_cost"] > 0:
        assert alt["headline"]["npv_climate_cost"] < h["npv_climate_cost"], \
            "a higher discount rate must reduce NPV"


def test_compute_coherent():
    """The spread must bracket the headline it claims to describe."""
    from app import compute
    if hazard.status()["degraded"]:
        import pytest
        pytest.skip("hazard cache not warmed")
    for scenario in ("ssp126", "ssp245", "ssp585"):
        compute.check_coherent(compute.summary(scenario))


def test_permanent_inundation_excluded_from_eal():
    """Standing water must be a write-down, never an annual loss line."""
    from app import compute
    if hazard.status()["degraded"]:
        import pytest
        pytest.skip("hazard cache not warmed")
    s = compute.summary("ssp585")
    perm = [a for a in s["assets"] if a["permanent_inundation"]]
    if not perm:
        return
    for a in perm:
        assert a["writedown"] > 0, f"{a['id']} flagged permanent but has no write-down"
        assert a["permanent_reason"], "a write-down must say why"
        assert a["writedown_pct"] <= 1.0, "cannot write down more than the asset"
    assert s["headline"]["permanent_writedown"] > 0
    assert s["headline"]["permanently_inundated_count"] == len(perm)


def test_asset_detail():
    from app import compute
    if hazard.status()["degraded"]:
        import pytest
        pytest.skip("hazard cache not warmed")
    d = compute.asset_detail("tj-priok", "ssp585")
    assert d is not None
    assert d["asset"]["id"] == "tj-priok"
    assert d["finance"]["annual_net_cost"] >= 0
    assert d["adaptation"], "adaptation options must be appraised"
    for hz_row in d["hazards"]:
        n = len(hz_row["return_periods"])
        assert len(hz_row["intensities"]) == n
        assert len(hz_row["damage_fractions"]) == n
        assert len(hz_row["losses"]) == n
        assert hz_row["curve_source"], "every hazard row must cite its curve"
    assert compute.asset_detail("does-not-exist", "ssp585") is None
