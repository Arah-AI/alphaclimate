"""AlphaClimate API."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import analyst, compute, curves, hazard, portfolio, tiles

log = logging.getLogger("alphaclimate")

app = FastAPI(
    title="AlphaClimate",
    version=compute.ENGINE_VERSION,
    description="Asset-level climate financial risk.",
)

# The browser talks to Next, which proxies here. CORS is only for local work
# against the API directly, so it stays narrow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(tiles.router)


def _parse_assumptions(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "assumptions must be valid JSON")
    if not isinstance(val, dict):
        raise HTTPException(400, "assumptions must be a JSON object")
    return val


@app.get("/api/health")
def health() -> dict:
    st = hazard.status()
    return {
        "status": "degraded" if st["degraded"] else "ok",
        "engine_version": compute.ENGINE_VERSION,
        "hazard_source": st["source"],
        "hazard_points_cached": st.get("cached", 0),
        "curves_loaded": len(curves.curves()),
        "curve_gaps": len(curves.gaps()),
        "analyst": analyst.status(),
        "assets": len(portfolio.DEMO_ASSETS),
        "detail": st.get("reason"),
    }


@app.get("/api/portfolio/{portfolio_id}/summary")
def portfolio_summary(
    portfolio_id: str,
    scenario: str = Query("ssp585"),
    assumptions: str | None = Query(None),
) -> dict:
    if portfolio_id != portfolio.DEMO_PORTFOLIO["id"]:
        raise HTTPException(404, f"No portfolio '{portfolio_id}'.")
    if scenario not in {s["id"] for s in hazard.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{scenario}'.")
    return compute.summary(scenario, _parse_assumptions(assumptions))


@app.get("/api/asset/{asset_id}")
def asset(
    asset_id: str,
    scenario: str = Query("ssp585"),
    assumptions: str | None = Query(None),
) -> dict:
    if scenario not in {s["id"] for s in hazard.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{scenario}'.")
    detail = compute.asset_detail(asset_id, scenario, _parse_assumptions(assumptions))
    if detail is None:
        raise HTTPException(404, f"No asset '{asset_id}'.")
    return detail


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    scenario: str = "ssp585"


@app.post("/api/analyst/ask")
def analyst_ask(body: AskBody) -> dict:
    """Explain a computed run. The model may never state an untraceable number."""
    if body.scenario not in {s["id"] for s in hazard.scenarios()}:
        raise HTTPException(400, f"Unknown scenario '{body.scenario}'.")
    run = compute.summary(body.scenario)
    try:
        return analyst.ask(body.question, run)
    except analyst.AnalystUnavailable as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/analyst/status")
def analyst_status() -> dict:
    return analyst.status()


@app.get("/api/curves/gaps")
def curve_gaps() -> dict:
    """Where we refuse to model. Published deliberately."""
    return {"gaps": curves.gaps(), "curves_loaded": len(curves.curves())}
