"""Regulatory evidence pack: one computed run, rendered as something filable.

Why this exists
---------------
A dashboard is not a disclosure. IFRS S2 and ESRS E1 both oblige a preparer to
put quantified climate figures, the scenarios behind them, and the basis on
which they were prepared into a document that an auditor can walk back to a
source. AlphaClimate already computes every number those standards ask for and
had no way to get them out of the browser.

The design rule for this module is that it is a *renderer*, not a second model.
Every figure printed here is lifted verbatim from `compute.summary()` and
`compute.asset_detail()`. Nothing is recomputed, re-derived, re-scaled or
rounded into a friendlier shape. If a number is wrong, it is wrong in the
engine, and the run id on the footer of every page says which run produced it.

The second design rule is that the document is never allowed to read more
confident than the model is. That is why the uncertainty section sits before
the methodology rather than in a footnote, why the declared damage-curve gaps
are printed in full with the reason we refuse to model them, and why the front
page carries a status statement saying this is model output rather than a
valuation or an assurance opinion. Most vendor climate reports omit all of
that. Including it is the product.

Standards references
--------------------
Paragraph numbers are carried in `REQUIREMENTS` with a `confirmed` flag and the
source that was checked. Where a requirement is real but the paragraph number
could not be confirmed against a primary source, the requirement is described
and `ref` is left empty. A plausible-looking fabricated citation in a filing is
worse than no citation, so this module will not emit one.

PDF engine
----------
reportlab, not weasyprint. weasyprint renders through Pango/cairo/GDK-pixbuf,
which means ~150 MB of system shared libraries in the image and a resident set
that a 1 GB VPS notices. reportlab is a single 1.9 MB wheel (4.8 MB installed)
whose only dependency, Pillow, is already pinned for the tile renderer. We are
laying out tables and paragraphs, not reflowing CSS, so the HTML engine buys
nothing here.
"""

from __future__ import annotations

# `python api/app/report.py` must run the self-check, and the module uses
# package-relative imports. Put api/ on the path and name the package so the
# direct run and the served import resolve identically.
if __package__ in (None, ""):  # pragma: no cover - direct-run shim only
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = "app"

import base64
import io
import json
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import compute
from . import curves as curve_lib
from . import hazard as hz
from . import portfolio as pf
from . import protection as prot
from .finance import Assumptions

router = APIRouter()

REPORT_VERSION = "1.0"

DISCLAIMER = (
    "The figures in this document are the output of a physical climate risk "
    "model. They are not a valuation, not an actuarial opinion, not investment "
    "advice, and not an assurance conclusion. They are a modelled estimate "
    "produced from public hazard data and published damage functions under "
    "stated assumptions, every one of which is disclosed in Section 7 and "
    "every one of which is arguable. The model disagreement in Section 6 is a "
    "measure of how much the answer moves under defensible alternative "
    "modelling choices; it is not a confidence interval and it does not bound "
    "the error. Nothing here has been reviewed by an auditor."
)


# ---------------------------------------------------------------------------
# what the standards actually ask for
# ---------------------------------------------------------------------------

@dataclass
class Requirement:
    """One disclosure obligation this pack answers.

    `ref` is a paragraph or disclosure-requirement number and is left empty
    where it could not be confirmed against a primary source. `source` is the
    document that was checked, so a reviewer can re-check it.
    """

    standard: str
    ref: str
    requirement: str
    confirmed: bool
    source: str = ""

    @property
    def label(self) -> str:
        return f"{self.standard} {self.ref}".strip() if self.ref else self.standard

    def as_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d


# Anything that could not be pinned to a paragraph in the primary text is
# carried with confirmed=False and an empty ref rather than a guessed number.
IFRS = "IFRS S2"
ESRS = "ESRS E1"
ESRS2 = "ESRS 2"
ESRS1 = "ESRS 1"

IFRS_SRC = (
    "IFRS S2 Climate-related Disclosures, ISSB, issued June 2023; paragraph "
    "text checked against the standard published at ifrs.org"
)
ESRS_SRC = (
    "ESRS E1 Climate change, Annex I to Commission Delegated Regulation (EU) "
    "2023/2772; paragraph text checked against the Official Journal text"
)
ESRS2_SRC = (
    "ESRS 2 General disclosures, Annex I to Commission Delegated Regulation "
    "(EU) 2023/2772"
)
ESRS1_SRC = (
    "ESRS 1 General requirements, Annex I to Commission Delegated Regulation "
    "(EU) 2023/2772"
)

# The ESRS set is being renumbered. Commission Delegated Regulation C(2026)
# 5010 of 3 July 2026 moves anticipated financial effects from E1-9 to E1-11
# (paragraphs 38-42) and resilience to E1-3, applying from financial year 2027.
# Whether that act has completed scrutiny and been published in the Official
# Journal is not confirmed here, so this generator cites the 2023/2772 text
# that is in force and says so rather than pre-empting the change.
ESRS_RENUMBERING = (
    "Citations below are to ESRS as adopted in Commission Delegated Regulation "
    "(EU) 2023/2772, the text in force. Commission Delegated Regulation "
    "C(2026) 5010 of 3 July 2026 renumbers this material, moving anticipated "
    "financial effects from E1-9 to E1-11 (paragraphs 38 to 42) and resilience "
    "to E1-3, applying from financial year 2027 and optional for financial "
    "year 2026. Whether that act has completed scrutiny and been published in "
    "the Official Journal has not been confirmed by this generator, so a pack "
    "prepared for a financial year 2027 filing must have its references "
    "re-checked against the version then in force."
)


# ---------------------------------------------------------------------------
# pack structure
# ---------------------------------------------------------------------------

@dataclass
class Tbl:
    """One table. `rows` are already-formatted strings; no arithmetic here."""

    title: str
    headers: list[str]
    rows: list[list[str]]
    note: str = ""
    widths: list[float] | None = None   # relative weights, not absolute mm

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "headers": self.headers,
            "rows": self.rows,
            "note": self.note,
        }


@dataclass
class Section:
    number: int
    title: str
    body: list[str] = field(default_factory=list)
    tables: list[Tbl] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "tables": [t.as_dict() for t in self.tables],
            "satisfies": [r.as_dict() for r in self.requirements],
        }


@dataclass
class EvidencePack:
    portfolio_id: str
    portfolio_name: str
    currency: str
    scenario: str
    scenario_label: str
    run_id: str
    engine_version: str
    report_version: str
    computed_at: str
    generated_at: str
    degraded: bool
    sections: list[Section]
    run: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return "Climate-related Physical Risk: Disclosure Evidence Pack"

    def section(self, title_fragment: str) -> Section:
        """Lookup used by the self-check; raises rather than returning None."""
        for s in self.sections:
            if title_fragment.lower() in s.title.lower():
                return s
        raise KeyError(f"no section matching {title_fragment!r}")

    def as_dict(self) -> dict:
        return {
            "document": {
                "title": self.title,
                "report_version": self.report_version,
                "generated_at": self.generated_at,
                "status_of_figures": DISCLAIMER,
            },
            "run": {
                "portfolio_id": self.portfolio_id,
                "portfolio_name": self.portfolio_name,
                "currency": self.currency,
                "scenario": self.scenario,
                "scenario_label": self.scenario_label,
                "run_id": self.run_id,
                "engine_version": self.engine_version,
                "computed_at": self.computed_at,
                "degraded": self.degraded,
            },
            "sections": [s.as_dict() for s in self.sections],
            "standards_index": [
                {"section": s.number, "title": s.title,
                 "satisfies": [r.as_dict() for r in s.requirements]}
                for s in self.sections if s.requirements
            ],
            "source_run": self.run,
        }


# ---------------------------------------------------------------------------
# formatting (presentation only, never arithmetic)
# ---------------------------------------------------------------------------

def _money(x: float | None, ccy: str = "USD") -> str:
    if x is None:
        return "not available"
    return f"{ccy} {x:,.0f}"


def _pct(x: float | None, dp: int = 2) -> str:
    if x is None:
        return "not available"
    return f"{x * 100:.{dp}f}%"


def _num(x: float | None, dp: int = 2) -> str:
    return "not available" if x is None else f"{x:,.{dp}f}"


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def _label(peril: str) -> str:
    return peril.replace("_", " ")


def _asset_name(run: dict, asset_id: str) -> str:
    for r in run["assets"]:
        if r["id"] == asset_id:
            return r["name"]
    return asset_id


def _scenario_label(scenario: str) -> str:
    for s in hz.scenarios():
        if s["id"] == scenario:
            return s["label"]
    return scenario


# ---------------------------------------------------------------------------
# section builders
# ---------------------------------------------------------------------------
#
# Each builder takes the already-computed run and returns a Section. None of
# them may call the engine, do arithmetic on a figure, or introduce a number
# that is not present in `run` or `details`.

def _s_basis(run: dict, ccy: str) -> Section:
    prov = run["provenance"]
    return Section(
        0,
        "Basis of preparation and status of these figures",
        body=[
            DISCLAIMER,
            "This document reports one deterministic model run. The run "
            f"identifier <b>{prov['run_id']}</b> is a hash of the portfolio, "
            "the scenario, the assumption set and the engine version, and it "
            "appears in the footer of every page. Re-running the engine with "
            "the same inputs reproduces the same identifier and the same "
            "figures; a different identifier means an input moved.",
            "Amounts are stated in "
            f"{ccy} and are undiscounted annual expected values unless the "
            "line explicitly says otherwise. Expected annual loss is a "
            "probability-weighted average across the modelled return periods, "
            "not a forecast of any particular year, and no single year is "
            "expected to look like it.",
            "The preparer, not the model, remains responsible for the "
            "materiality judgements, the assumption set and anything that is "
            "ultimately filed. This pack is evidence for those judgements, "
            "not a substitute for them.",
        ],
        requirements=[
            Requirement(
                IFRS, "paragraph 18(a)",
                "Use all reasonable and supportable information that is "
                "available to the entity at the reporting date without undue "
                "cost or effort.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 18(b)",
                "Use an approach commensurate with the skills, capabilities "
                "and resources available to the entity.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "AR 67 and AR 68",
                "Acknowledge that no commonly accepted methodology exists for "
                "quantifying anticipated financial effects, so the disclosure "
                "depends on the preparer's own methodology and on significant "
                "judgement.",
                True, ESRS_SRC,
            ),
        ],
    )


def _s_scope(run: dict, ccy: str, a: Assumptions) -> Section:
    prov = run["provenance"]
    h = run["headline"]
    p = run["portfolio"]
    st = hz.status()

    scope = Tbl(
        "Scope of the assessment",
        ["Item", "Value"],
        [
            ["Portfolio identifier", p["id"]],
            ["Portfolio name", p["name"]],
            ["Reporting currency", p["currency"]],
            ["Assets in scope", f"{h['asset_count']}"],
            ["Total asset value in scope", _money(h["total_value"], ccy)],
            ["Scenario reported", f"{_scenario_label(prov['scenario'])} "
                                  f"({prov['scenario']})"],
            ["Financial projection horizon", f"{prov['horizon_years']} years "
                                             "from the run date"],
            ["Run computed at (UTC)", prov["computed_at"]],
            ["Hazard data as at (UTC)", st.get("generated") or "not recorded"],
            ["Hazard points held in cache", f"{st.get('cached', 0):,}"],
            ["Engine version", prov["engine_version"]],
            ["Run identifier", prov["run_id"]],
            ["Portfolio note", p["note"]],
        ],
        widths=[0.38, 0.62],
    )

    assets = Tbl(
        "Assets in scope",
        ["Asset", "Name", "Country", "Sector", "Value"],
        [
            [r["id"], r["name"], r["country"], r["sector"], _money(r["value"], ccy)]
            for r in sorted(run["assets"], key=lambda r: r["name"])
        ],
        note="Asset locations are real coordinates. Financial inputs for this "
             "demonstration portfolio are illustrative and are labelled as "
             "such by the platform; a filed pack must be run against the "
             "preparer's own asset register.",
        widths=[0.14, 0.34, 0.16, 0.18, 0.18],
    )

    horizons = Tbl(
        "Time horizons applied",
        ["Horizon", "Basis"],
        [
            ["Hazard epoch", "The hazard layers read for this run are the "
                             "2050 epoch of the underlying dataset, as shown "
                             "by the dataset paths in Section 7."],
            ["Financial projection", f"{prov['horizon_years']} years, the "
                                     "assumed real-asset hold period."],
            ["Discounting", f"{_pct(a.discount_rate)} nominal, applied to "
                            "each projected year."],
            ["Loss trend within horizon", f"{_pct(a.hazard_growth)} per year "
                                          "applied to the annual loss."],
        ],
        note="The mismatch between the hazard epoch and the projection "
             "horizon is a real limitation and is stated in Section 8.",
        widths=[0.24, 0.76],
    )

    return Section(
        0,
        "Scope, entity and reporting boundary",
        body=[
            "The assessment covers the physical assets listed below, at their "
            "real coordinates, for the perils for which a defensible published "
            "damage function exists. Perils that are measured but deliberately "
            "not priced are listed in Section 8; they are excluded from every "
            "monetary figure in this pack.",
            "Transition risk, liability risk, supply-chain exposure beyond the "
            "asset boundary, and climate-related opportunities are outside the "
            "scope of this engine and are not covered by this document.",
        ],
        tables=[scope, assets, horizons],
        requirements=[
            Requirement(
                IFRS, "paragraph 22(b)(i)(7)",
                "Disclose the scope of operations covered by the scenario "
                "analysis, for example operating locations and business units.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraphs 10(c) and 10(d)",
                "Disclose the time horizons over which the effects of each "
                "climate-related risk could reasonably be expected to occur, "
                "and how the entity defines short, medium and long term.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "paragraph 20(b) and AR 11(a) to AR 11(c)",
                "Screen the exposure of assets and business activities to "
                "climate-related hazards, identifying the hazards over time "
                "horizons linked to the expected lifetime of the assets.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS1, "paragraph 77",
                "Apply the defined time horizons: short term is the reporting "
                "period, medium term up to five years beyond it, long term "
                "more than five years.",
                True, ESRS1_SRC,
            ),
        ],
    )


def _s_scenarios(run: dict) -> Section:
    prov = run["provenance"]
    sub_all = hz.scenario_substitution()
    selected = prov["scenario"]

    roles = {
        "ssp585": "High-emissions pathway. Required as the stress case: "
                  "physical hazard is highest here and the standards expect a "
                  "high-emissions scenario to be among those tested.",
        "ssp245": "Middle-of-the-road pathway. The central case against which "
                  "the high-emissions result is read.",
        "ssp126": "Paris-aligned pathway. The low-hazard bound of the sweep.",
        "historical": "Observed baseline. Used to check that a forward "
                      "scenario is not milder than history, not to price loss.",
    }

    swept = Tbl(
        "Scenarios used and why",
        ["Scenario", "Label", "Role in this assessment", "Reported"],
        [
            [sid, _scenario_label(sid), roles.get(sid, "Not used"),
             "headline" if sid == selected else
             ("uncertainty sweep" if sid in compute.SPREAD_SCENARIOS else "no")]
            for sid in ("ssp126", "ssp245", "ssp585")
        ],
        note="The headline figures in Section 4 are computed under the "
             f"selected scenario ({selected}). All three pathways are re-run "
             "in full to produce the disagreement range in Section 6, so the "
             "reported range does not depend on which scenario was selected.",
        widths=[0.11, 0.20, 0.53, 0.16],
    )

    sub_rows = []
    for sid in ("ssp126", "ssp245", "ssp585"):
        rec = sub_all.get(sid) or {}
        sub_rows.append([
            sid,
            rec.get("wri", "no substitution recorded"),
            rec.get("note", "no note recorded"),
            "yes" if sid == selected else "no",
        ])

    sub = Tbl(
        "Scenario substitution actually applied",
        ["Scenario requested", "Pathway read", "Recorded reason", "Selected"],
        sub_rows,
        note="This substitution is a material modelling choice and is "
             "disclosed rather than buried. The inundation layers are indexed "
             "by RCP, not by SSP. Where no exact RCP counterpart of the "
             "requested SSP exists in the source data, the nearest available "
             "pathway is read and recorded here. SSP1-2.6 in particular is "
             "served by RCP4.5, which is a warmer pathway than the label "
             "implies: the low end of the range in Section 6 is therefore "
             "likely to overstate loss under a genuinely Paris-aligned world, "
             "not understate it. This also matters for the high-emissions "
             "case. ESRS E1 AR 11(d) names IPCC SSP5-8.5 as an acceptable "
             "high-emission scenario. The inundation layers read here are "
             "indexed on RCP8.5, the radiative-forcing pathway that SSP5-8.5 "
             "follows, and are the source provider's own RCP8.5 product; they "
             "are not an SSP5-8.5 run relabelled by this platform. A reviewer "
             "who requires a literal SSP5-8.5 hazard product should treat this "
             "as a substitution rather than a match.",
        widths=[0.16, 0.16, 0.55, 0.13],
    )

    return Section(
        0,
        "Scenarios used and the basis for choosing them",
        body=[
            "Three forward pathways are used: a Paris-aligned pathway, a "
            "middle-of-the-road pathway, and a high-emissions pathway. The "
            "high-emissions pathway is included deliberately, because a "
            "resilience assessment that only tests a benign world tests "
            "nothing.",
            "The scenarios are taken from the public hazard store as "
            "published. They are not re-downscaled, re-bias-corrected or "
            "otherwise adjusted by this platform. The consequence is that the "
            "scenario definitions carry the source provider's choices, "
            "including the RCP-for-SSP substitution set out below.",
            "The analysis is quantitative at asset level for the perils that "
            "have a damage function, and is deliberately not extended to "
            "perils that do not; see Section 8.",
        ],
        tables=[swept, sub],
        requirements=[
            Requirement(
                IFRS, "paragraph 22",
                "Use climate-related scenario analysis to assess climate "
                "resilience, using an approach commensurate with the entity's "
                "circumstances.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 22(b)(i)(1) to (7)",
                "Disclose the inputs used: which scenarios and their sources, "
                "whether a diverse range was included, whether the scenarios "
                "relate to transition or physical risk, whether a scenario "
                "aligned with the latest international agreement on climate "
                "change was among them, why the chosen scenarios are relevant, "
                "the time horizons, and the scope of operations covered.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 22(b)(iii)",
                "Disclose the reporting period in which the scenario analysis "
                "was carried out.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "paragraph 20(b)(i)",
                "Identify climate-related hazards considering at least high "
                "emission climate scenarios.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS, "AR 11(d)",
                "The high-emission scenario may be based on IPCC SSP5-8.5, on "
                "regional projections derived from it, or on an NGFS scenario "
                "with high physical risk.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS, "paragraph 21 and AR 13",
                "Explain how scenario analysis informed the identification and "
                "assessment of physical risks, disclosing the scenarios used, "
                "their sources, their alignment with state-of-the-art science, "
                "the time horizons and the key inputs.",
                True, ESRS_SRC,
            ),
        ],
    )


def _s_effects(run: dict, ccy: str, a: Assumptions) -> Section:
    h = run["headline"]
    sp = h["eal_spread"]

    totals = Tbl(
        "Quantified anticipated financial effects, portfolio total",
        ["Effect", "Amount", "As a share of asset value"],
        [
            ["Expected annual loss, physical damage",
             _money(h["eal"], ccy), _pct(h["eal_pct_of_value"], 3)],
            ["Expected annual loss, range across modelling choices",
             f"{_money(sp['low'], ccy)} to {_money(sp['high'], ccy)}", "see Section 6"],
            [f"Net present value of climate cost over {a.horizon_years} years",
             _money(h["npv_climate_cost"], ccy), "not applicable"],
            ["Value impairment (NPV of climate cost, capped at asset value)",
             _money(h["value_impairment"], ccy), _pct(h["value_impairment_pct"], 2)],
            ["One-off write-down, permanently inundated assets",
             _money(h["permanent_writedown"], ccy),
             _pct(h["permanent_writedown_pct"], 2)],
            ["Annual protection gap (loss retained after insurance recovery)",
             _money(h["protection_gap"], ccy), "not applicable"],
            ["Share of expected annual loss recovered by insurance",
             _pct(h["insured_share"], 1), "not applicable"],
            ["Share of expected annual loss retained",
             _pct(h["retained_share"], 1), "not applicable"],
            ["Assets breaching a debt covenant after climate cost",
             f"{h['covenant_breaches']} of {h['asset_count']}", "not applicable"],
            ["Assets flagged as likely uninsurable",
             f"{h['uninsurable_count']} of {h['asset_count']}", "not applicable"],
        ],
        note="The net present value of climate cost includes physical damage, "
             "business interruption, insurance recovery and premium, projected "
             f"over {a.horizon_years} years and discounted at "
             f"{_pct(a.discount_rate)}. Value impairment is that present value "
             "capped at the asset's own value; it is a modelled impairment "
             "indicator, not an IAS 36 impairment test.",
        widths=[0.50, 0.28, 0.22],
    )

    per_asset = Tbl(
        "Quantified anticipated financial effects, by asset",
        ["Asset", "Value", "Expected annual loss", "EAL %",
         "Value impairment", "Impairment %", "Write-down", "Covenant breach"],
        [
            [r["name"], _money(r["value"], ccy), _money(r["eal"], ccy),
             _pct(r["eal_pct"], 3), _money(r["impairment"], ccy),
             _pct(r["impairment_pct"], 1), _money(r["writedown"], ccy),
             _yn(r["covenant_breach"])]
            for r in run["assets"]
        ],
        note="Sorted by expected annual loss, largest first. A write-down "
             "figure appears only where the hazard layer places the asset "
             "under permanent water; see Section 8.",
        widths=[0.20, 0.12, 0.13, 0.08, 0.13, 0.09, 0.13, 0.09],
    )

    return Section(
        0,
        "Quantified anticipated financial effects",
        body=[
            "The figures below are the anticipated financial effects of "
            "physical climate risk on the assets in scope, expressed in "
            "currency. They are presented both as a single amount and as a "
            "range, because a single amount on its own would misrepresent how "
            "much of this is genuinely known.",
            "The single amount is the best estimate under the selected "
            "scenario using the highest-ranked damage function for each asset. "
            "The range is the full spread across every scenario, climate model "
            "variant and defensible damage function tested. The single amount "
            "is not the mid-point of the range and is not intended to be; the "
            "two are computed on different bases and Section 6 explains why "
            "they differ.",
            "Business interruption is included in the present value and the "
            "impairment figure but is not shown as a separate line at "
            "portfolio level, because it is sized from asset revenue that this "
            "demonstration portfolio states as illustrative.",
        ],
        tables=[totals, per_asset],
        requirements=[
            Requirement(
                IFRS, "paragraph 15(b)",
                "Disclose the anticipated financial effects of climate-related "
                "risks on financial position, financial performance and cash "
                "flows over the short, medium and long term.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 16(a) to (d)",
                "Disclose quantitative and qualitative information about those "
                "effects, including the expected change in financial position, "
                "financial performance and cash flows.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 17",
                "In providing quantitative information, an entity may disclose "
                "a single amount or a range.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "E1-9, paragraphs 64(a) and 66(a)",
                "Disclose the anticipated financial effects from material "
                "physical risks, including the monetary amount and proportion "
                "of assets at material physical risk before considering "
                "adaptation actions, disaggregated by acute and chronic "
                "physical risk.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS, "AR 70(a)",
                "The amount of assets at material physical risk may be "
                "presented as either a single amount or a range.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS2, "SBM-3, paragraph 48(e)",
                "Disclose the anticipated financial effects over the short, "
                "medium and long term, including the expected time horizons.",
                True, ESRS2_SRC,
            ),
        ],
    )


def _s_physical(run: dict, details: dict, ccy: str) -> Section:
    h = run["headline"]

    by_peril = Tbl(
        "Expected annual loss by peril",
        ["Peril", "Expected annual loss", "Share of portfolio EAL"],
        [[_label(p["peril"]), _money(p["eal"], ccy), _pct(p["share"], 1)]
         for p in run["perils"]],
        note="Only perils with a published damage function appear here. "
             "Perils that are measured but not priced are in Section 8 and "
             "contribute nothing to any figure in this pack.",
        widths=[0.40, 0.32, 0.28],
    )

    bands = Tbl(
        "Risk banding, definition and distribution",
        ["Band", "Expected annual loss as a share of asset value", "Assets"],
        [
            ["low", f"below {_pct(compute.BAND_EDGES[0][0], 2)}",
             str(h["bands"]["low"])],
            ["moderate", f"{_pct(compute.BAND_EDGES[0][0], 2)} to "
                         f"{_pct(compute.BAND_EDGES[1][0], 2)}",
             str(h["bands"]["moderate"])],
            ["high", f"{_pct(compute.BAND_EDGES[1][0], 2)} to "
                     f"{_pct(compute.BAND_EDGES[2][0], 2)}",
             str(h["bands"]["high"])],
            ["severe", f"above {_pct(compute.BAND_EDGES[2][0], 2)}",
             str(h["bands"]["severe"])],
        ],
        note="The bands are a presentational grouping of the expected annual "
             "loss percentage. They are not a rating, not a score, and carry "
             "no information the currency figures do not already carry.",
        widths=[0.20, 0.55, 0.25],
    )

    rows = []
    for r in run["assets"]:
        d = details.get(r["id"]) or {}
        modelled = ", ".join(_label(x["peril"]) for x in d.get("hazards", [])) or "none"
        rows.append([
            r["name"], r["band"], _label(r["top_peril"]), modelled,
            _pct(r["eal_pct"], 3), _yn(r["uninsurable"]),
            _yn(r["extrapolated"]),
        ])

    by_asset = Tbl(
        "Physical risk by asset",
        ["Asset", "Band", "Dominant peril", "Perils priced", "EAL %",
         "Uninsurable", "Outside curve range"],
        rows,
        note="'Outside curve range' means at least one hazard intensity read "
             "for that asset sits beyond the calibrated range of the damage "
             "function applied to it. The engine holds the damage fraction "
             "flat past the last calibrated point rather than extrapolating "
             "upward, so those assets are more likely understated than "
             "overstated.",
        widths=[0.21, 0.10, 0.17, 0.20, 0.09, 0.11, 0.12],
    )

    peril_rows = []
    for r in run["assets"]:
        d = details.get(r["id"]) or {}
        for x in d.get("hazards", []):
            peril_rows.append([
                r["name"], _label(x["peril"]), x["units"],
                _num(max(x["intensities"]) if x["intensities"] else None, 2),
                _money(x["eal"], ccy), _money(x["eal_tail"], ccy),
                x["curve_confidence"],
            ])

    detail = Tbl(
        "Peril detail by asset",
        ["Asset", "Peril", "Units", "Peak modelled intensity",
         "Expected annual loss", "of which tail", "Curve confidence"],
        peril_rows,
        note="'Peak modelled intensity' is the hazard value at the longest "
             "return period held for that asset. 'of which tail' is the part "
             "of the expected annual loss contributed beyond the longest "
             "modelled return period, where the loss is held flat rather than "
             "extrapolated. Portfolio tail share is "
             f"{_pct(h['tail_share'], 1)} of expected annual loss.",
        widths=[0.19, 0.15, 0.09, 0.14, 0.16, 0.13, 0.14],
    )

    return Section(
        0,
        "Physical risk by peril and by asset",
        body=[
            "Physical risk is assessed at the individual asset, from the "
            "hazard value at the asset's own location, and is aggregated "
            "upward. It is not inferred from a sector average or a country "
            "score.",
            "Both acute perils (flood, windstorm) and the chronic perils held "
            "in the hazard store are read for each asset. Only the acute "
            "perils carry a damage function that this platform is prepared to "
            "defend, so only they are monetised. That is a deliberate refusal "
            "and it is documented in Section 8 rather than hidden by a "
            "plausible-looking number.",
        ],
        tables=[by_peril, bands, by_asset, detail],
        requirements=[
            Requirement(
                IFRS, "paragraph 10(b)",
                "For each climate-related risk, state whether it is a physical "
                "risk or a transition risk.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "Appendix A",
                "Physical risks are event-driven (acute) or arise from "
                "longer-term shifts in climatic patterns (chronic).",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 25(a)(iii)",
                "Disclose how the entity assesses the nature, likelihood and "
                "magnitude of the effects of each risk.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "E1-9, paragraph 66(c) and AR 70(c)",
                "Disclose the location of significant assets at material "
                "physical risk, disaggregated by acute and chronic physical "
                "risk.",
                True, ESRS_SRC,
            ),
        ],
    )


def _s_uncertainty(run: dict, details: dict, ccy: str) -> Section:
    h = run["headline"]
    sp = h["eal_spread"]

    band = Tbl(
        "Model disagreement, portfolio expected annual loss",
        ["Measure", "Amount"],
        [
            ["Lowest result across all modelling choices", _money(sp["low"], ccy)],
            ["Median result across all modelling choices", _money(sp["median"], ccy)],
            ["Highest result across all modelling choices", _money(sp["high"], ccy)],
            ["Ratio of highest to lowest", f"{sp['range_ratio']}x"],
            ["Number of full model runs behind the range", f"{sp['n']}"],
            ["Reported headline figure (Section 4)", _money(h["eal"], ccy)],
        ],
        note="The headline is computed under the selected scenario with the "
             "highest-ranked damage function for each asset, and it excludes "
             "permanently inundated assets from the annual loss. The range is "
             "produced by a separate full sweep over every combination. The "
             "two are different quantities and neither is a correction of the "
             "other. Where the headline sits outside the range the engine "
             "fails its own coherence check and refuses the run.",
        widths=[0.58, 0.42],
    )

    drivers = sp.get("by_driver") or {}
    order = sorted(drivers.items(), key=lambda kv: kv[1]["share"], reverse=True)
    attribution = Tbl(
        "What the disagreement is driven by",
        ["Driver", "Alternatives tested", "Range attributed",
         "Share of total range"],
        [[k.replace("_", " "), str(v["levels"]), _money(v["range"], ccy),
          _pct(v["share"], 1)] for k, v in order],
        note="This is a first-order attribution: for each driver the results "
             "are grouped by that driver's level and the spread of the group "
             "means is measured. It is not a variance decomposition and the "
             "shares are not required to sum to one hundred per cent. It is "
             "reported because knowing that the answer moves is much less "
             "useful than knowing what moves it.",
        widths=[0.28, 0.18, 0.30, 0.24],
    )

    rows = []
    for r in run["assets"]:
        d = details.get(r["id"]) or {}
        s = d.get("spread") or {}
        if not s or not s.get("n"):
            rows.append([r["name"], "no runs produced a loss", "", "", ""])
            continue
        rows.append([
            r["name"], _money(s["low"], ccy), _money(s["median"], ccy),
            _money(s["high"], ccy), f"{s['range_ratio']}x",
        ])

    # Assets whose headline loss is zero but whose sweep is not. The two code
    # paths admit different sets of damage curves, so the sweep can price an
    # asset the headline does not. Comparing two figures the run already
    # published is not a new computation, and a document that printed both
    # without saying so would be inviting the reader to spot the contradiction
    # unaided.
    contradicted = [
        r["name"] for r in run["assets"]
        if r["eal"] == 0 and ((details.get(r["id"]) or {}).get("spread") or {}).get("n")
    ]

    per_asset = Tbl(
        "Model disagreement by asset",
        ["Asset", "Low", "Median", "High", "High / low"],
        rows,
        note="An asset whose ratio is materially wider than the portfolio's is "
             "an asset where the modelling choice matters more than the "
             "hazard, and is the first place to spend diligence effort." +
             (
                 " Note that " + ", ".join(contradicted) + " carr" +
                 ("y" if len(contradicted) > 1 else "ies") +
                 " a disagreement band here while reporting no expected annual "
                 "loss in Section 4. The two are produced by different code "
                 "paths that admit different sets of damage curves: the "
                 "headline uses the highest-ranked curve for the asset, the "
                 "sweep also runs lower-ranked alternatives, and some of those "
                 "alternatives are datum-shifted curves that return a non-zero "
                 "damage fraction at zero hazard intensity. Those bands are an "
                 "artefact of curve admission, not a loss estimate, and should "
                 "not be read as one. This is a known engine defect and is "
                 "restated in Section 8."
                 if contradicted else ""
             ),
        widths=[0.28, 0.18, 0.18, 0.18, 0.18],
    )

    top = order[0][0].replace("_", " ") if order else "not attributed"
    return Section(
        0,
        "Uncertainty and model disagreement",
        body=[
            "Every figure in Section 4 is one answer out of many defensible "
            "ones. The same assets, run through every scenario, every climate "
            "model variant and every damage function this platform considers "
            "citable, produce a range of results. That range is reported here "
            "in full rather than collapsed to a single number.",
            f"Across {sp['n']} complete model runs the portfolio expected "
            f"annual loss ranges from {_money(sp['low'], ccy)} to "
            f"{_money(sp['high'], ccy)}, a factor of {sp['range_ratio']}. The "
            f"largest single contributor to that range is the choice of "
            f"{top}.",
            "This is model disagreement, not statistical confidence. It "
            "measures how much the answer moves when a defensible modelling "
            "choice is changed. It does not capture error in the underlying "
            "hazard data, error in the asset inputs, or the possibility that "
            "every curve tested is wrong in the same direction. The true "
            "uncertainty is wider than the range shown.",
        ],
        tables=[band, attribution, per_asset],
        requirements=[
            Requirement(
                IFRS, "paragraph 22(a)(ii)",
                "Disclose the significant areas of uncertainty considered in "
                "the assessment of climate resilience.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "paragraph 19(c) and AR 8",
                "Disclose the results of the resilience analysis, including "
                "the uncertainties involved and the ability to adapt.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS2, "SBM-3, paragraph 48(f)",
                "Disclose the resilience of the strategy and business model, "
                "including how the analysis was conducted and the time "
                "horizons applied; quantitative information may be disclosed "
                "as single amounts or ranges.",
                True, ESRS2_SRC,
            ),
        ],
    )


def _s_methodology(run: dict, details: dict, ccy: str, a: Assumptions) -> Section:
    prov = run["provenance"]

    hazard_tbl = Tbl(
        "Hazard datasets read for this run",
        ["Dataset", "Path in store", "Units", "Resolution"],
        [[s["dataset"], s["path"], s["units"], s["resolution"]]
         for s in prov["hazard_sources"]],
        note="Source: " + (prov["hazard_sources"][0]["citation"]
                           if prov["hazard_sources"] else "no hazard read") +
             ". Paths are given exactly as read so a reviewer can pull the "
             "same array. The epoch and pathway are visible in the path "
             "itself.",
        widths=[0.18, 0.50, 0.10, 0.22],
    )

    seen: dict[str, list[str]] = {}
    for aid, d in details.items():
        for x in d.get("hazards", []):
            key = x["curve_id"]
            seen.setdefault(key, [x["curve_source"], x["curve_confidence"], ""])
            used = seen[key][2]
            name = _asset_name(run, aid)
            seen[key][2] = f"{used}, {name}" if used else name

    curve_tbl = Tbl(
        "Damage functions applied, with citation and confidence",
        ["Curve identifier", "Published source", "Confidence", "Applied to"],
        [[cid, v[0], v[1], v[2]] for cid, v in sorted(seen.items())],
        note="Confidence is the rating carried in the curated curve set, not a "
             "statistical confidence level. Damage fractions are held flat "
             "above the last calibrated point rather than extrapolated. "
             f"{len(curve_lib.curves()):,} curves are loaded in total; the "
             "selection rule prefers the regional curve for the asset's own "
             "region and occupancy, then the global curve for that occupancy, "
             "and never falls through to an unrelated occupancy.",
        widths=[0.24, 0.42, 0.12, 0.22],
    )

    agg = prov["aggregation"]
    agg_tbl = Tbl(
        "Spatial aggregation",
        ["Item", "Value"],
        [
            ["Method", agg.get("method", "not recorded")],
            ["Pixel size (degrees)", _num(agg.get("pixel_degrees"), 6)],
            ["Search rings on nodata", str(agg.get("search_rings", "not recorded"))],
            ["Maximum search radius", f"{_num(agg.get('search_radius_km'), 2)} km"],
            ["Rationale", agg.get("why", "not recorded")],
        ],
        widths=[0.24, 0.76],
    )

    cov = prov["flood_protection"]
    undef_rows = []
    for key in cov.get("undefended", []):
        lon, lat, peril = key.split("|")
        match = next(
            (x.name for x in pf.DEMO_ASSETS
             if f"{x.lon:.4f}" == lon and f"{x.lat:.4f}" == lat),
            "no asset in the current register at this point",
        )
        undef_rows.append([match, _label(peril), f"{lon}, {lat}"])

    prot_tbl = Tbl(
        "Flood protection standards applied",
        ["Item", "Value"],
        [
            ["Source", cov.get("source", "not recorded")],
            ["Basis", cov.get("basis", "not recorded")],
            ["Points carrying a protection standard", str(cov.get("defended_points", 0))],
            ["Points left undefended", str(cov.get("undefended_points", 0))],
            ["Perils to which a standard is applied",
             ", ".join(_label(p) for p in prot.DEFENDED_PERILS)],
        ],
        note="A defence built to a 1-in-N standard is modelled as passing no "
             "loss for events more frequent than 1-in-N. Where the protection "
             "database holds no value for a location, the asset is modelled "
             "undefended and is listed below. Absence of a standard is never "
             "treated as a default level of protection, in either direction.",
        widths=[0.44, 0.56],
    )

    undef_tbl = Tbl(
        "Points left undefended",
        ["Asset", "Peril", "Coordinates (lon, lat)"],
        undef_rows or [["none", "", ""]],
        note="Undefended points carry the raw undefended hazard. Their loss is "
             "therefore higher than it would be if a protection standard were "
             "assumed, and that is the intended direction.",
        widths=[0.46, 0.26, 0.28],
    )

    assum_tbl = Tbl(
        "Financial assumptions applied",
        ["Assumption", "Value", "What it drives"],
        [
            ["Discount rate", _pct(a.discount_rate), "present value of future loss"],
            ["Horizon", f"{a.horizon_years} years", "length of the projection"],
            ["Hazard growth", _pct(a.hazard_growth) + " per year",
             "trend applied to annual loss within the horizon"],
            ["EBITDA margin", _pct(a.ebitda_margin), "business interruption"],
            ["Downtime at total damage", f"{_num(a.downtime_days_per_damage_unit, 0)} days",
             "business interruption, scaled by damage fraction"],
            ["Recoverable share of lost revenue", _pct(a.bi_recovery_fraction),
             "business interruption"],
            ["Insurance deductible", _pct(a.deductible_fraction) + " of asset value",
             "insurance recovery"],
            ["Insurance limit", _pct(a.limit_fraction) + " of asset value",
             "insurance recovery"],
            ["Coinsurance retained", _pct(a.coinsurance), "insurance recovery"],
            ["Premium as a multiple of expected loss", f"{_num(a.premium_rate_on_eal)}x",
             "annual cost"],
            ["Premium escalation", _pct(a.premium_escalation) + " per year",
             "annual cost"],
            ["Cover assumed available", _yn(a.insurable), "insurance recovery"],
        ],
        note="Every assumption above is overridable through the API. A "
             "reviewer who disagrees with one can change it and see the "
             "figures move; the run identifier changes with it, so a pack "
             "produced under different assumptions is distinguishable from "
             "this one.",
        widths=[0.36, 0.24, 0.40],
    )

    identity = Tbl(
        "Run identity and reproducibility",
        ["Item", "Value"],
        [
            ["Run identifier", prov["run_id"]],
            ["Engine version", prov["engine_version"]],
            ["Report generator version", REPORT_VERSION],
            ["Computed at (UTC)", prov["computed_at"]],
            ["Service state at run time",
             "degraded" if prov["degraded"] else "healthy"],
            ["Degradation reason", prov.get("degraded_reason") or "none"],
        ],
        widths=[0.36, 0.64],
    )

    return Section(
        0,
        "Methodology",
        body=[
            "The method is standard catastrophe-risk practice. For each asset "
            "and each peril, the hazard value is read at the asset's location "
            "for a set of return periods. Each hazard value is converted to a "
            "damage fraction with a published vulnerability curve, multiplied "
            "by asset value to give a loss, and the resulting loss curve is "
            "integrated over exceedance probability to give an expected annual "
            "loss. The contribution beyond the longest modelled return period "
            "is computed and reported separately rather than dropped.",
            "The expected annual loss is then translated into cash flow, "
            "present value, valuation impact and credit metrics using the "
            "assumption set below. That translation step is arithmetic with "
            "every assumption named, not a proprietary black box.",
            "No hazard data is generated by this platform. No damage function "
            "is invented by this platform. Where a hazard is measured but no "
            "defensible damage function exists, it is refused rather than "
            "approximated; the refusals are in Section 8.",
        ],
        tables=[hazard_tbl, curve_tbl, agg_tbl, prot_tbl, undef_tbl,
                assum_tbl, identity],
        requirements=[
            Requirement(
                IFRS, "paragraph 25(a)(i)",
                "Disclose the inputs and parameters used to identify "
                "climate-related risks, including the data sources and the "
                "scope of operations covered.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 22(b)(ii)",
                "Disclose the key assumptions made in the scenario analysis.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "AR 69",
                "Explain the scope, time horizons, calculation methodology, "
                "critical assumptions, parameters and limitations behind the "
                "quantified anticipated financial effects.",
                True, ESRS_SRC,
            ),
            Requirement(
                ESRS, "AR 13(d)",
                "Disclose the key inputs and constraints, including whether "
                "the physical risk analysis uses geospatial coordinates or "
                "only broad national or regional data.",
                True, ESRS_SRC,
            ),
        ],
    )


def _s_limitations(run: dict, details: dict, ccy: str) -> Section:
    h = run["headline"]
    prov = run["provenance"]

    gaps = curve_lib.gaps()
    gap_tbl = Tbl(
        "Hazards deliberately not priced",
        ["Hazard", "Exposure", "Why it is refused", "What would close the gap"],
        [[g.get("hazard", ""), g.get("exposure", ""), g.get("why", ""),
          g.get("what_would_close_it", "")] for g in gaps],
        note=f"{len(gaps)} declared gaps. These hazards contribute nothing to "
             "any monetary figure in this pack. A reader must not treat their "
             "absence as evidence that the exposure is immaterial; it is "
             "evidence that this platform will not put a number on it.",
        widths=[0.17, 0.17, 0.42, 0.24],
    )

    no_data = [r for r in run["assets"]
               if not (details.get(r["id"]) or {}).get("hazards")]
    nd_tbl = Tbl(
        "Assets with no priced hazard reading",
        ["Asset", "Country", "Value", "Reported expected annual loss"],
        [[r["name"], r["country"], _money(r["value"], ccy), _money(r["eal"], ccy)]
         for r in no_data] or [["none", "", "", ""]],
        note="A zero for these assets means no priced hazard reading was "
             "available at that location, not that the asset is safe. Reading "
             "it as safety is the single most dangerous misuse of this "
             "document.",
        widths=[0.30, 0.20, 0.22, 0.28],
    )

    perm = [r for r in run["assets"] if r["permanent_inundation"]]
    perm_tbl = Tbl(
        "Assets treated as permanent inundation write-downs",
        ["Asset", "Write-down", "Share of value", "Basis for the classification"],
        [[r["name"], _money(r["writedown"], ccy), _pct(r["writedown_pct"], 1),
          r["permanent_reason"] or "not recorded"] for r in perm]
        or [["none", "", "", ""]],
        note="Where a flood layer is already deep at the most frequent return "
             "period and barely rises out to the rarest, it is describing "
             "standing water rather than a flood frequency distribution. "
             "Integrating that as an annual loss would charge the asset for "
             "the same water every year. These assets are removed from the "
             "expected annual loss and carried as a one-off write-down "
             "instead. The write-down is a modelled indicator of value at "
             "risk, not an impairment charge, and the underlying subsidence "
             "layer is itself contested.",
        widths=[0.20, 0.16, 0.12, 0.52],
    )

    unpriced_seen: dict[str, str] = {}
    for d in details.values():
        for u in d.get("unpriced_hazards", []):
            unpriced_seen.setdefault(u["peril"], u.get("why_unpriced", ""))
    unpriced_tbl = Tbl(
        "Hazards measured for these assets but not monetised",
        ["Hazard", "Reason"],
        [[_label(k), v] for k, v in sorted(unpriced_seen.items())]
        or [["none", ""]],
        note="These readings exist in the hazard store and are returned by the "
             "platform for inspection. They are excluded from every currency "
             "figure in this pack.",
        widths=[0.26, 0.74],
    )

    extrap = [r["name"] for r in run["assets"] if r["extrapolated"]]

    body = [
        "The limitations below are stated because a disclosure that omits them "
        "would read more confidently than the model behind it deserves.",
        "<b>Scope.</b> Only physical risk to the listed assets is assessed. "
        "Transition risk, policy and legal risk, supply-chain and customer "
        "exposure beyond the asset boundary, and climate-related opportunities "
        "are not modelled and are not reflected in any figure here.",
        "<b>Hazard epoch against projection horizon.</b> The hazard layers "
        "read for this run are a single future epoch. The financial "
        f"projection runs {prov['horizon_years']} years from the run date, "
        "with the annual loss grown by the declared hazard growth assumption "
        "rather than by re-reading the hazard for each projected year. The "
        "trajectory is therefore an assumption-driven interpolation, not a "
        "year-by-year hazard simulation.",
        "<b>Resolution.</b> Hazard is read at grid resolution stated in "
        "Section 7. A grid cell of roughly a kilometre cannot resolve "
        "within-site elevation, so an asset's real exposure depends on "
        "micro-topography this model cannot see.",
        "<b>Vulnerability.</b> A damage function maps one hazard intensity to "
        "one damage fraction. Real damage depends on construction, condition, "
        "contents, warning time and response, none of which is an input here. "
        "Section 6 shows that the choice of curve is a leading driver of the "
        "answer.",
        "<b>Single-peril treatment.</b> Perils are integrated independently "
        "and summed. Correlated events, compound events, and the same storm "
        "hitting several assets at once are not modelled, so portfolio tail "
        "risk is understated.",
        "<b>Financial inputs.</b> Asset values, revenues and debt for this "
        "portfolio are illustrative, as the platform states in its own "
        "portfolio note. Every currency figure scales directly with them.",
        "<b>Acute perils only.</b> Every peril that carries a currency figure "
        "in this pack is an acute, event-driven hazard. The chronic hazards "
        "held in the store are read and shown but are not monetised, so the "
        "acute-versus-chronic disaggregation that ESRS E1 AR 70(c)(ii) asks "
        "for is complete on the acute side and empty on the chronic side. That "
        "is a gap in coverage, not a finding that chronic exposure is nil.",
        "<b>No reconciliation to the accounts.</b> These figures are not "
        "reconciled to any line item in a set of financial statements, and no "
        "materiality assessment has been applied to them. Both are preparer "
        "steps. Section 9 lists the requirements this pack therefore leaves "
        "unanswered.",
        "<b>Independent perils, independent assets.</b> The portfolio total is "
        "a sum of asset-level expected losses computed independently. Nothing "
        "here models one event striking several assets, and nothing models the "
        "insurance programme responding at portfolio level rather than asset "
        "by asset.",
    ]
    contradicted = [
        r["name"] for r in run["assets"]
        if r["eal"] == 0 and ((details.get(r["id"]) or {}).get("spread") or {}).get("n")
    ]
    if contradicted:
        body.append(
            "<b>Headline and sweep disagree at zero.</b> " +
            ", ".join(contradicted) +
            " report no expected annual loss in Section 4 but a non-zero "
            "disagreement band in Section 6. The uncertainty sweep admits "
            "lower-ranked damage functions that the headline selection does "
            "not, and some of those are datum-shifted curves whose damage "
            "fraction is above zero at zero hazard intensity, so a site with "
            "no measured flood depth is still charged a loss. The affected "
            "band figures are a defect of the engine's curve admission and "
            "must not be read as a loss estimate for those assets."
        )
    if extrap:
        body.append(
            "<b>Outside calibrated range.</b> " + ", ".join(extrap) +
            " carry at least one hazard intensity beyond the calibrated range "
            "of the damage function applied. The damage fraction is held flat "
            "past the last calibrated point, which understates rather than "
            "overstates loss for those assets."
        )
    if h["permanently_inundated_count"]:
        body.append(
            f"<b>Permanent inundation.</b> {h['permanently_inundated_count']} "
            "asset(s) are excluded from the expected annual loss and carried "
            "as a write-down instead, for the reason set out in the table "
            "below. Portfolio expected annual loss is lower than it would be "
            "if those assets were integrated as annual events, and higher "
            "than it would be if the subsidence layer were not used at all."
        )
    if prov["degraded"]:
        body.append(
            "<b>Degraded run.</b> The platform reported itself degraded when "
            f"this run was computed: {prov.get('degraded_reason')}. The "
            "figures should not be relied on."
        )

    return Section(
        0,
        "Limitations and known gaps",
        body=body,
        tables=[gap_tbl, nd_tbl, perm_tbl, unpriced_tbl],
        requirements=[
            Requirement(
                IFRS, "paragraph 19",
                "Quantitative information need not be provided where the "
                "effects are not separately identifiable, or where the "
                "measurement uncertainty is so high that the resulting "
                "quantitative information would not be useful.",
                True, IFRS_SRC,
            ),
            Requirement(
                IFRS, "paragraph 21(a) and 21(b)",
                "Where that relief is used, explain why, and provide "
                "qualitative information about the effects instead.",
                True, IFRS_SRC,
            ),
            Requirement(
                ESRS, "AR 68",
                "Where no commonly accepted methodology exists, state that the "
                "amounts rest on the preparer's own methodology and on "
                "significant judgement.",
                True, ESRS_SRC,
            ),
        ],
    )


def _s_index(sections: list[Section]) -> Section:
    rows = []
    for s in sections:
        for r in s.requirements:
            rows.append([
                r.standard,
                r.ref or "reference not confirmed",
                r.requirement,
                f"Section {s.number}",
            ])
    unconfirmed = sum(1 for s in sections for r in s.requirements if not r.confirmed)
    body = [
        "The table below maps the requirements this pack is intended to "
        "support onto the sections that answer them. It is an aid to review, "
        "not an assertion of compliance: whether a filing complies is a "
        "judgement for the preparer and its auditor, and this pack covers only "
        "the physical-risk portion of what either standard asks for.",
        "IFRS S2 fully incorporates the TCFD recommendations. The TCFD was "
        "disbanded following its October 2023 status report and the Financial "
        "Stability Board asked the IFRS Foundation to take over monitoring of "
        "progress on climate-related disclosures. A preparer already reporting "
        "against TCFD should read the IFRS S2 references below as the "
        "successor requirements.",
        ESRS_RENUMBERING,
    ]
    if unconfirmed:
        body.append(
            f"{unconfirmed} of the requirements below are described without a "
            "paragraph reference, because the exact paragraph could not be "
            "confirmed against the primary text. The requirement is real; the "
            "citation is withheld, because a fabricated reference in a filing "
            "is worse than none."
        )

    not_answered = Tbl(
        "Requirements this pack does not answer",
        ["Standard", "Reference", "Requirement", "Why not"],
        [
            [IFRS, "paragraph 15(a)",
             "Current financial effects of climate-related risks on the "
             "reporting period's financial statements.",
             "The engine is forward-looking. It models expected loss, not "
             "losses already recognised in a set of accounts."],
            [IFRS, "paragraph 22(a)(iii)",
             "Capacity to adjust or adapt: financial resource flexibility, "
             "ability to redeploy, repurpose, upgrade or decommission assets, "
             "and the effect of current and planned investments in adaptation.",
             "The platform appraises adaptation options per asset, but this "
             "pack reports the unadapted position only."],
            [IFRS, "paragraphs 27 to 37",
             "Cross-industry metrics: greenhouse gas emissions, transition "
             "risk exposure, capital deployment, internal carbon price, "
             "remuneration.",
             "Out of scope. This platform models physical risk only and holds "
             "no emissions data."],
            [ESRS, "E1-9, paragraph 66(b)",
             "Proportion of assets at material physical risk addressed by "
             "adaptation actions.",
             "Adaptation appraisal is not carried into this pack, so the "
             "figures here are the position before adaptation."],
            [ESRS, "E1-9, paragraph 66(c) and AR 70(c)(i)",
             "Location of significant assets aggregated by NUTS level-3 code "
             "for assets in EU territory.",
             "Asset locations are reported as longitude and latitude, which is "
             "more precise but is not the required aggregation. A preparer "
             "must map the coordinates to NUTS level-3 themselves."],
            [ESRS, "E1-9, paragraph 66(d)",
             "Monetary amount and proportion of net revenue from business "
             "activities at material physical risk.",
             "The engine uses asset revenue to size business interruption but "
             "does not produce a revenue-at-risk figure, and this pack will "
             "not derive one."],
            [ESRS, "E1-9, paragraph 68",
             "Reconciliation of the disclosed amounts to the relevant line "
             "items or notes in the financial statements.",
             "The engine has no access to the preparer's financial statements. "
             "This is a preparer step and cannot be automated from here."],
            [ESRS, "E1-9, paragraph 67",
             "Anticipated financial effects from material transition risks.",
             "Out of scope. Transition risk is not modelled."],
            [ESRS1, "Appendix C, referenced by paragraphs 136 and 137",
             "Phase-in relief: E1-9 may be omitted for the first year of "
             "preparation and satisfied with qualitative disclosure only for "
             "the first three years where quantitative disclosure is "
             "impracticable.",
             "This is relief available to the preparer, not a requirement to "
             "be answered. Noted so a preparer knows it exists."],
        ],
        note="Listing what a document does not cover is part of not reading "
             "more confident than the model is. A reader must not infer from "
             "the presence of this pack that these requirements have been met "
             "elsewhere.",
        widths=[0.09, 0.19, 0.36, 0.36],
    )

    return Section(
        0,
        "Index of disclosure requirements addressed",
        body=body,
        tables=[
            Tbl(
                "Requirements addressed",
                ["Standard", "Reference", "Requirement", "Answered in"],
                rows,
                widths=[0.09, 0.19, 0.56, 0.16],
            ),
            not_answered,
        ],
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def build(portfolio_id: str = "demo", scenario: str = "ssp585",
          overrides: dict | None = None) -> EvidencePack:
    """Assemble the pack from one computed run. Computes no figures of its own.

    Two engine calls happen here, `compute.summary` and one
    `compute.asset_detail` per asset, because per-asset spread and per-peril
    curve provenance only exist in the detail payload. Both are the engine's
    own output under the same scenario and assumption set, so the pack is
    internally consistent by construction.
    """
    if portfolio_id != pf.DEMO_PORTFOLIO["id"]:
        raise KeyError(f"no portfolio '{portfolio_id}'")

    run = compute.summary(scenario, overrides)
    details = {
        r["id"]: compute.asset_detail(r["id"], scenario, overrides) or {}
        for r in run["assets"]
    }
    # Echoing the resolved assumption set is not a computation: it is the same
    # deterministic input object the engine used, reconstructed from the same
    # overrides.
    a = Assumptions.merged(overrides)
    ccy = run["portfolio"]["currency"]
    prov = run["provenance"]

    sections = [
        _s_basis(run, ccy),
        _s_scope(run, ccy, a),
        _s_scenarios(run),
        _s_effects(run, ccy, a),
        _s_physical(run, details, ccy),
        _s_uncertainty(run, details, ccy),
        _s_methodology(run, details, ccy, a),
        _s_limitations(run, details, ccy),
    ]
    for i, s in enumerate(sections, start=1):
        s.number = i
    sections.append(_s_index(sections))
    sections[-1].number = len(sections)

    return EvidencePack(
        portfolio_id=portfolio_id,
        portfolio_name=run["portfolio"]["name"],
        currency=ccy,
        scenario=scenario,
        scenario_label=_scenario_label(scenario),
        run_id=prov["run_id"],
        engine_version=prov["engine_version"],
        report_version=REPORT_VERSION,
        computed_at=prov["computed_at"],
        generated_at=datetime.now(timezone.utc).isoformat(),
        degraded=prov["degraded"],
        sections=sections,
        run=run,
    )


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

INK = colors.HexColor("#1B2430")
RULE = colors.HexColor("#C7CDD4")
BAND = colors.HexColor("#F2F4F6")
MUTED = colors.HexColor("#5A6672")

MARGIN = 18 * mm
FOOT = 14 * mm


class _Numbered(rl_canvas.Canvas):
    """Two-pass canvas so the footer can say 'Page n of m'.

    A filed document gets printed and separated. Every page therefore carries
    the run id and the page count, so a loose sheet can be traced back to the
    run that produced it and a short pack is visibly short.
    """

    def __init__(self, *args, run_id: str = "", doc_title: str = "", **kw):
        super().__init__(*args, **kw)
        self._run_id = run_id
        self._doc_title = doc_title
        self._pages: list[dict] = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        w, _ = self._pagesize
        y = FOOT
        self.setStrokeColor(RULE)
        self.setLineWidth(0.4)
        self.line(MARGIN, y + 5 * mm, w - MARGIN, y + 5 * mm)
        self.setFont("Helvetica", 7)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, y, self._run_id)
        self.drawCentredString(w / 2.0, y, self._doc_title)
        self.drawRightString(w - MARGIN, y,
                             f"Page {self._pageNumber} of {total}")


def _styles() -> dict:
    ss = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                             fontSize=14, leading=17, textColor=INK,
                             spaceBefore=2, spaceAfter=7),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                             fontSize=9, leading=12, textColor=INK,
                             spaceBefore=9, spaceAfter=4, keepWithNext=1),
        "body": ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica",
                               fontSize=8.6, leading=12.4, textColor=INK,
                               alignment=TA_JUSTIFY, spaceAfter=6),
        "note": ParagraphStyle("note", parent=ss["BodyText"], fontName="Helvetica-Oblique",
                               fontSize=7.3, leading=10, textColor=MUTED,
                               alignment=TA_JUSTIFY, spaceBefore=3, spaceAfter=10),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=7.2,
                               leading=9.2, textColor=INK),
        "cellh": ParagraphStyle("cellh", fontName="Helvetica-Bold", fontSize=7.2,
                                leading=9.2, textColor=colors.white),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=23,
                                leading=27, textColor=INK, spaceAfter=8),
        "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=11.5,
                              leading=15, textColor=MUTED, spaceAfter=22),
        "toc": ParagraphStyle("toc", fontName="Helvetica", fontSize=8.4,
                              leading=13, textColor=INK),
    }


def _table(t: Tbl, st: dict, width: float):
    weights = t.widths or [1.0 / max(1, len(t.headers))] * len(t.headers)
    if len(weights) != len(t.headers):
        weights = [1.0 / len(t.headers)] * len(t.headers)
    total = sum(weights) or 1.0
    cols = [width * w / total for w in weights]

    data = [[Paragraph(h, st["cellh"]) for h in t.headers]]
    for row in t.rows:
        data.append([Paragraph(str(c), st["cell"]) for c in row])

    tbl = Table(data, colWidths=cols, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
    ]
    for i in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, i), (-1, i), BAND))
    tbl.setStyle(TableStyle(style))

    flow = [Paragraph(t.title, st["h2"]), tbl]
    if t.note:
        flow.append(Paragraph(t.note, st["note"]))
    else:
        flow.append(Spacer(1, 8))
    return flow


def _title_page(pack: EvidencePack, st: dict, width: float) -> list:
    run = pack.run
    h = run["headline"]
    out = [
        Spacer(1, 26 * mm),
        Paragraph(pack.title, st["title"]),
        Paragraph(f"{pack.portfolio_name} &middot; {pack.scenario_label}", st["sub"]),
    ]

    facts = Tbl(
        "",
        ["Item", "Value"],
        [
            ["Portfolio", f"{pack.portfolio_name} ({pack.portfolio_id})"],
            ["Scenario reported", f"{pack.scenario_label} ({pack.scenario})"],
            ["Assets in scope", f"{h['asset_count']}"],
            ["Total value in scope", _money(h["total_value"], pack.currency)],
            ["Expected annual loss", _money(h["eal"], pack.currency)],
            ["Range across modelling choices",
             f"{_money(h['eal_spread']['low'], pack.currency)} to "
             f"{_money(h['eal_spread']['high'], pack.currency)}"],
            ["Run identifier", pack.run_id],
            ["Engine version", pack.engine_version],
            ["Computed at (UTC)", pack.computed_at],
            ["Document generated at (UTC)", pack.generated_at],
        ],
        widths=[0.34, 0.66],
    )
    out += _table(facts, st, width)

    out.append(Paragraph("Status of these figures", st["h2"]))
    box = Table([[Paragraph(DISCLAIMER, st["cell"])]], colWidths=[width])
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.7, INK),
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    out.append(box)

    out.append(Spacer(1, 9 * mm))
    out.append(Paragraph("Contents", st["h2"]))
    for s in pack.sections:
        out.append(Paragraph(f"{s.number}.&nbsp;&nbsp;{s.title}", st["toc"]))
    out.append(PageBreak())
    return out


def render_pdf(pack: EvidencePack) -> bytes:
    """Lay the pack out as a PDF. Adds no content the pack does not carry."""
    st = _styles()
    buf = io.BytesIO()
    width = A4[0] - 2 * MARGIN

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=FOOT + 9 * mm,
        title=pack.title,
        author=f"AlphaClimate {pack.engine_version}",
        subject=f"{pack.portfolio_name} / {pack.scenario} / {pack.run_id}",
    )

    story = _title_page(pack, st, width)
    for s in pack.sections:
        story.append(Paragraph(f"{s.number}. {s.title}", st["h1"]))
        for para in s.body:
            story.append(Paragraph(para, st["body"]))
        for t in s.tables:
            flow = _table(t, st, width)
            # Keep a small table with its heading; let a long one break.
            story.extend(flow)
        story.append(Spacer(1, 4 * mm))

    def maker(*args, **kw):
        return _Numbered(*args, run_id=pack.run_id,
                         doc_title=f"{pack.portfolio_name} · climate physical "
                                   f"risk evidence pack", **kw)

    doc.build(story, canvasmaker=maker)
    return buf.getvalue()


def page_count(pdf: bytes) -> int:
    """Pages in a reportlab PDF, counted from its page objects.

    `/Type /Pages` is the page tree node and contains `/Type /Page` as a
    substring, so it is subtracted out.
    """
    return pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_PDF_LITERAL = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)


def extracted_text(pdf: bytes) -> str:
    """Text pulled back out of a rendered PDF, for verification only.

    reportlab writes content streams ASCII85-encoded and flate-compressed, so
    "is the word on the page" cannot be answered by searching the raw bytes.
    This undoes both filters and collects the literal strings. It is not a
    general PDF text extractor and does not try to be; it exists so the
    self-check can prove that the tables actually rendered rather than trusting
    that `build` did not raise.
    """
    parts: list[bytes] = []
    for m in _PDF_STREAM.finditer(pdf):
        chunk = m.group(1).strip()
        if chunk.endswith(b"~>"):
            try:
                chunk = base64.a85decode(chunk[:-2])
            except ValueError:
                continue
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for lit in _PDF_LITERAL.finditer(chunk):
            body = lit.group(0)[1:-1]
            for esc, raw in ((b"\\(", b"("), (b"\\)", b")"), (b"\\\\", b"\\")):
                body = body.replace(esc, raw)
            parts.append(body)
    return b" ".join(parts).decode("latin-1")


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def _filename(pack: EvidencePack, ext: str) -> str:
    return (f"alphaclimate-{pack.portfolio_id}-{pack.scenario}-"
            f"{pack.run_id}.{ext}")


def _build_or_404(portfolio_id: str, scenario: str) -> EvidencePack:
    if scenario not in {s["id"] for s in hz.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{scenario}'.")
    if scenario == "historical":
        raise HTTPException(
            400,
            "The historical baseline is not a disclosure scenario. Pick a "
            "forward pathway.",
        )
    try:
        return build(portfolio_id, scenario)
    except KeyError:
        raise HTTPException(404, f"No portfolio '{portfolio_id}'.")


@router.get("/api/report/{portfolio_id}")
def report(
    portfolio_id: str,
    scenario: str = Query("ssp585"),
    format: str = Query("pdf", pattern="^(pdf|json)$"),
) -> StreamingResponse:
    """The evidence pack for one run, as a PDF to file or JSON to machine-read."""
    pack = _build_or_404(portfolio_id, scenario)

    if format == "json":
        payload = json.dumps(pack.as_dict(), indent=2).encode()
        media, ext = "application/json", "json"
    else:
        payload = render_pdf(pack)
        media, ext = "application/pdf", "pdf"

    return StreamingResponse(
        io.BytesIO(payload),
        media_type=media,
        headers={
            "Content-Disposition":
                f'attachment; filename="{_filename(pack, ext)}"',
            "Content-Length": str(len(payload)),
            "X-AlphaClimate-Run-Id": pack.run_id,
            "X-AlphaClimate-Engine-Version": pack.engine_version,
        },
    )


@router.get("/api/report/{portfolio_id}/preview")
def report_preview(
    portfolio_id: str,
    scenario: str = Query("ssp585"),
) -> dict:
    """The pack structure for a UI preview: everything except the raw run."""
    pack = _build_or_404(portfolio_id, scenario)
    out = pack.as_dict()
    out.pop("source_run", None)
    return out


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------

def demo() -> None:
    import os
    import tempfile

    if hz.status()["degraded"]:
        print("report.py self-check: DEGRADED (no hazard cache)")
        return

    pack = build("demo", "ssp585")
    run = pack.run
    h = run["headline"]
    ccy = pack.currency

    # --- structure --------------------------------------------------------
    assert len(pack.sections) == 9, f"expected 9 sections, got {len(pack.sections)}"
    assert [s.number for s in pack.sections] == list(range(1, 10)), \
        "sections must be numbered consecutively from 1"
    for s in pack.sections:
        assert s.body or s.tables, f"section {s.number} is empty"
        for t in s.tables:
            for row in t.rows:
                assert len(row) == len(t.headers), \
                    f"ragged row in '{t.title}': {len(row)} vs {len(t.headers)}"

    # --- no fabricated citations -----------------------------------------
    reqs = [r for s in pack.sections for r in s.requirements]
    assert reqs, "the pack must claim to answer at least one requirement"
    for r in reqs:
        assert r.confirmed or not r.ref, \
            f"unconfirmed requirement carries a citation: {r.label}"
        assert r.source, f"requirement without a checked source: {r.requirement[:40]}"

    # --- every figure traces to the run ----------------------------------
    def cells(section_fragment: str) -> str:
        s = pack.section(section_fragment)
        return " | ".join(c for t in s.tables for row in t.rows for c in row)

    eff = cells("financial effects")
    assert _money(h["eal"], ccy) in eff, "headline EAL must appear as computed"
    assert _money(h["npv_climate_cost"], ccy) in eff, "NPV must appear as computed"
    assert _money(h["value_impairment"], ccy) in eff
    assert _money(h["protection_gap"], ccy) in eff
    for r in run["assets"]:
        assert _money(r["eal"], ccy) in eff, f"{r['id']} EAL missing from the pack"

    scope = cells("Scope")
    assert _money(h["total_value"], ccy) in scope
    assert str(h["asset_count"]) in scope
    for r in run["assets"]:
        assert r["name"] in scope, f"{r['id']} missing from the scope table"

    # --- scenario substitution is actually disclosed ---------------------
    sc = cells("Scenarios")
    sub = hz.scenario_substitution()
    assert sub, "the cache must record a scenario substitution"
    for sid, rec in sub.items():
        assert rec["wri"] in sc, f"substitution for {sid} not disclosed"
        assert rec["note"] in sc, f"substitution reason for {sid} not disclosed"
    assert "ssp585" in sc and "ssp126" in sc, "all swept pathways must be named"

    # --- uncertainty is present with its attribution ---------------------
    unc = pack.section("Uncertainty")
    ub = cells("Uncertainty") + " ".join(unc.body)
    sp = h["eal_spread"]
    assert _money(sp["low"], ccy) in ub and _money(sp["high"], ccy) in ub
    assert _money(sp["median"], ccy) in ub
    assert str(sp["n"]) in ub, "the number of runs behind the range must be stated"
    for driver in sp["by_driver"]:
        assert driver.replace("_", " ") in ub, f"driver {driver} not attributed"
    assert "not a confidence interval" in DISCLAIMER

    # --- methodology carries the exact provenance ------------------------
    meth = cells("Methodology")
    for src in run["provenance"]["hazard_sources"]:
        assert src["path"] in meth, f"hazard path {src['path']} missing"
    assert run["provenance"]["run_id"] in meth
    assert run["provenance"]["engine_version"] in meth
    assert run["provenance"]["aggregation"]["method"] in meth
    cov = run["provenance"]["flood_protection"]
    assert str(cov["defended_points"]) in meth
    assert str(cov["undefended_points"]) in meth
    assert cov["source"] in meth
    used_curves = {x["curve_id"] for aid in [r["id"] for r in run["assets"]]
                   for x in (compute.asset_detail(aid, "ssp585") or {}).get("hazards", [])}
    for cid in used_curves:
        assert cid in meth, f"damage curve {cid} not disclosed in the methodology"
    assert used_curves, "a healthy run must apply at least one damage curve"

    # --- limitations state the real ones ---------------------------------
    lim = pack.section("Limitations")
    limtext = " ".join(lim.body) + cells("Limitations")
    gaps = curve_lib.gaps()
    assert gaps, "the curve set must declare its gaps"
    for g in gaps:
        assert g["hazard"] in limtext, f"declared gap {g['hazard']} not published"
        assert g["why"] in limtext, f"gap reason for {g['hazard']} not published"
    no_data = [r["name"] for r in run["assets"]
               if not (compute.asset_detail(r["id"], "ssp585") or {}).get("hazards")]
    for name in no_data:
        assert name in limtext, f"{name} has no hazard reading and is not disclosed"
    odd = [r["name"] for r in run["assets"]
           if r["eal"] == 0
           and ((compute.asset_detail(r["id"], "ssp585") or {}).get("spread") or {}).get("n")]
    for name in odd:
        assert name in limtext, \
            f"{name} has a zero headline but a non-zero sweep and is not flagged"
    if odd:
        assert "must not be read as a loss estimate" in limtext

    if h["permanently_inundated_count"]:
        assert "permanent" in limtext.lower()
        for r in run["assets"]:
            if r["permanent_inundation"]:
                assert _money(r["writedown"], ccy) in limtext

    # --- JSON form -------------------------------------------------------
    js = pack.as_dict()
    blob = json.dumps(js)
    assert json.loads(blob)["run"]["run_id"] == pack.run_id
    assert len(js["sections"]) == 9
    assert js["source_run"]["headline"]["eal"] == h["eal"], \
        "the JSON pack must ship the run it was built from, unaltered"
    assert js["document"]["status_of_figures"] == DISCLAIMER
    assert js["standards_index"], "the JSON must expose the standards mapping"

    # --- determinism ------------------------------------------------------
    again = build("demo", "ssp585")
    assert again.run_id == pack.run_id, "same inputs must give the same run id"
    # Sections carry the run's wall-clock timestamp, so the whole pack cannot
    # be byte-identical. Every figure in it must be.
    for frag in ("financial effects", "Uncertainty", "Physical risk",
                 "Limitations", "Scenarios"):
        assert again.section(frag).as_dict() == pack.section(frag).as_dict(), \
            f"the same run rendered '{frag}' differently"

    # --- an override must move the pack ----------------------------------
    alt = build("demo", "ssp585", {"discount_rate": 0.15})
    assert alt.run_id != pack.run_id, "an assumption change must change the run id"

    # --- a bad portfolio is refused --------------------------------------
    try:
        build("not-a-portfolio")
        raise AssertionError("an unknown portfolio must raise")
    except KeyError:
        pass

    # --- PDF --------------------------------------------------------------
    pdf = render_pdf(pack)
    assert pdf.startswith(b"%PDF-"), "not a PDF"
    assert pdf.rstrip().endswith(b"%%EOF"), "PDF is truncated"
    pages = page_count(pdf)
    assert pages >= 6, f"a real evidence pack should not fit in {pages} pages"
    assert len(pdf) > 20_000, f"PDF is suspiciously small at {len(pdf)} bytes"

    # Not blank, and the tables actually rendered.
    text = extracted_text(pdf)
    assert len(text) > 20_000, f"the PDF carries only {len(text)} characters"
    for token in (
        pack.run_id, pack.portfolio_name, "Expected annual loss",
        "Limitations and known gaps", "Uncertainty and model disagreement",
        "Requirements addressed", "Requirements this pack does not answer",
        f"Page {pages} of {pages}", "Page 2 of ",
        "paragraph 22(b)(i)(1) to (7)", "AR 11(d)", "SSP5-8.5", "FLOPROS",
    ):
        assert token in text, f"{token!r} did not make it onto a page"

    # Real figures, not just headings: the money must be on the page too.
    assert _money(h["eal"], ccy) in text, "the headline EAL is not in the PDF"
    assert _money(h["total_value"], ccy) in text
    for r in run["assets"]:
        assert r["name"] in text, f"{r['id']} does not appear in the PDF"
    for src in run["provenance"]["hazard_sources"]:
        assert src["path"] in text, f"{src['path']} is not in the rendered PDF"
    for g in curve_lib.gaps():
        assert g["hazard"] in text, f"gap {g['hazard']} is not in the rendered PDF"

    out_dir = os.environ.get("ALPHACLIMATE_REPORT_OUT") or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, _filename(pack, "pdf"))
    with open(path, "wb") as fh:
        fh.write(pdf)
    on_disk = os.path.getsize(path)
    assert on_disk == len(pdf)

    jpath = os.path.join(out_dir, _filename(pack, "json"))
    with open(jpath, "w") as fh:
        fh.write(json.dumps(js, indent=2))

    print(f"report.py self-check passed ({len(pack.sections)} sections, "
          f"{sum(len(s.tables) for s in pack.sections)} tables, "
          f"{pages} pages, {on_disk / 1024:.0f} KB)")
    print(f"  wrote {path}")
    print(f"  wrote {jpath}")


if __name__ == "__main__":
    demo()
