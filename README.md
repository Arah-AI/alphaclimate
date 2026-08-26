# AlphaClimate

Asset-level climate financial risk. Takes a portfolio of physical assets, pulls
real hazard data for each location, applies citable damage functions, and
returns the thing nobody else returns: **money a committee recognises**, with
the model disagreement shown rather than averaged away.

## Why this exists

Open source already covers most of the climate risk pipeline. Hazard data
(CMIP6, ERA5, ISIMIP), hazard indicators (OS-Climate, JRC), risk engines
(CLIMADA, physrisk, Oasis LMF) and integrated assessment models (Mimi, DICE,
MESSAGEix) are all free and maintained.

It breaks at one joint: **turning a modelled loss distribution into revised cash
flow, valuation and covenant metrics.** There is no open, production-grade
implementation of that step. Every vendor does it privately and will not show
the working, which is why a GARP benchmark of 13 vendors found their estimates
disperse substantially, and why only 2 of 9 firms would share documentation
with independent researchers.

This repository implements that missing joint, and publishes the disagreement.

## What it does

| Layer | Source | Status |
|---|---|---|
| Hazard indicators | OS-Climate public store on AWS (`os-climate-physical-risk`) | consumed, not rebuilt |
| Damage functions | JRC (Huizinga et al. 2017) + FEMA HAZUS 6.1, 265 curves | curated, cross-checked against the published tables |
| Risk engine | `api/app/engine.py` | loss exceedance curve, EAL with the tail kept separate |
| Financial translation | `api/app/finance.py` | **the differentiator**, no open equivalent exists |
| Disagreement | `api/app/compute.py` | every scenario × every defensible curve, spread attributed |
| Provenance | every response | dataset path, curve citation, engine version, run id |

### Deliberate refusals

- **No blended risk score.** Every headline number is currency.
- **No invented curves.** Nine hazards are listed in `data/damage_curves.json`
  under `gaps` with the reason, rather than given a made-up function. Drought,
  wildfire, hail and chronic heat are refused on those grounds.
- **No silent extrapolation.** A hazard intensity outside a curve's calibrated
  range is flagged in the UI, not quietly evaluated.
- **No silent zero.** If hazard data cannot be reached the API reports itself
  degraded. A zero that means "no data" is indistinguishable from "this asset is
  safe", which is the worst failure this system can have.

## Layout

```
api/          FastAPI service
  app/engine.py     loss integration and spread attribution
  app/finance.py    damage -> cash flow, NPV, LTV, DSCR, adaptation appraisal
  app/curves.py     damage-curve selection and alternatives
  app/hazard.py     cached point reads from the public hazard store
  app/compute.py    portfolio orchestration
  app/main.py       routes
data/         damage_curves.json (265 curves), hazard_cache.json (warmed points)
web/          Next.js 16 + Tailwind 4 + Recharts dashboard
scripts/      cache warming
```

## Running it

```bash
# API
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r api/requirements.txt
uvicorn app.main:app --app-dir api --reload --port 8000

# Web
cd web && npm install && npm run dev
```

Then http://localhost:3000. The web app proxies `/api/*` to the API, so there is
no CORS setup and no separate base URL to configure.

### With Docker

```bash
docker compose up --build
```

## Tests

Every non-trivial module carries an assert-based self-check that runs standalone:

```bash
python -m pytest tests/ -q          # runs them all
python api/app/engine.py            # or run one directly
```

The checks are behavioural, not smoke tests. They assert that EAL is linear in
asset value, that a dry asset has exactly zero EAL, that input ordering cannot
change the answer, that a higher discount rate reduces present value, that
impairment is capped at asset value, and that yearly rows reconcile with the NPV
they were summed from.

## Data credits

- Huizinga, De Moel & Szewczyk (2017), *Global flood depth-damage functions*,
  JRC105688, European Commission Joint Research Centre.
- FEMA Hazus 6.1 flood and wind damage functions.
- OS-Climate physrisk hazard indicators, AWS Open Data Registry.

`data/damage_curves.json` records a `source_discrepancies` block. Notably,
`physrisk-lib`'s bundled JRC North America / Industrial curve disagrees with
published Table 3-12 by up to 0.03. This build resolves in favour of the citable
report and records the difference rather than silently picking one.
