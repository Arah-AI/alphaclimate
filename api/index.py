"""Vercel Python serverless entrypoint.

Vercel's Python runtime detects an ASGI application exported as `app`, so this
re-exports the FastAPI instance. Everything the engine needs is bundled: the
damage curves and the warmed hazard cache are plain JSON files in data/, and
nothing calls S3 at request time.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault(
    "ALPHACLIMATE_CURVES", os.path.join(_ROOT, "data", "damage_curves.json")
)
os.environ.setdefault(
    "ALPHACLIMATE_HAZARD_CACHE", os.path.join(_ROOT, "data", "hazard_cache.json")
)

from api.app.main import app  # noqa: E402

__all__ = ["app"]
