"""Grounded analyst: explains a computed run, never invents a number.

Runs on Kimi (Moonshot), which speaks the OpenAI-compatible protocol, so this
uses the `openai` client pointed at Moonshot's base URL rather than a
provider-specific SDK. Swapping providers is a base URL and a model id.

The rule this module exists to enforce: an LLM may explain, rank, compare and
summarise the engine's output, but it may not produce a figure of its own. In a
regulated report a fabricated number is an unmanageable liability, so every
numeric token in the answer is checked against the run payload before the answer
is returned. If a number cannot be traced, the answer is withheld.

That check is the product feature, not a safety afterthought.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

MODEL = os.environ.get("KIMI_MODEL", "kimi-k3")
BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")


def _api_key() -> str | None:
    return os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")

# Numbers that are part of ordinary prose rather than claims about the data.
_ALLOWED_BARE = {
    0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
    100.0, 1000.0, 1.25, 0.75,  # covenant floors quoted in the system prompt
}

SYSTEM = """You are the analyst inside AlphaClimate, a climate financial risk platform.

You are given the complete computed output of one risk run as JSON. Answer the
user's question about that run.

ABSOLUTE RULES, in order of importance:

1. NEVER state a number that is not present in the JSON. Do not calculate, do not
   estimate, do not round to a "nicer" figure, do not convert units, do not sum
   two figures to produce a third. If answering would require a number that is
   not in the data, say exactly which number is missing and stop.
2. When you quote a figure, quote it as it appears in the data. You may format it
   readably (1934716 may be written as 1,934,716) but the digits must match.
3. Never describe the portfolio as safe, fine, or acceptable. You report what the
   engine computed; risk appetite is the reader's judgement, not yours.
4. If the run is flagged degraded, say so before anything else.
5. Where the spread between models is wide, say so. A single number without its
   range is a misleading answer in this domain, and the range is in the data.
6. Cite the path you took through the JSON for each claim, in the form
   [headline.eal] or [assets[2].name]. One citation per factual sentence.

STYLE: direct, specific, no hedging, no preamble, no bullet-point padding. Two to
five sentences unless the question genuinely needs more. Write for an investment
professional who is short on time. Do not use em dashes."""


class AnalystUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(_api_key())


def status() -> dict:
    if not available():
        return {
            "available": False,
            "reason": "No KIMI_API_KEY set. The analyst is disabled; every "
                      "other part of the platform works without it.",
        }
    return {"available": True, "model": MODEL, "base_url": BASE_URL, "reason": None}


# --------------------------------------------------------------------------
# the numeric guard
# --------------------------------------------------------------------------

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM.finditer(text):
        raw = m.group(0).rstrip(".").replace(",", "")
        if not raw or raw in {"-", "."}:
            continue
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _numbers_in_payload(obj: Any, acc: set[float] | None = None) -> set[float]:
    """Every numeric value anywhere in the run, plus useful derived forms."""
    if acc is None:
        acc = set()
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        v = float(obj)
        acc.add(v)
        acc.add(round(v))
        acc.add(round(v, 1))
        acc.add(round(v, 2))
        # a share of 0.1537 is very often quoted as 15.37 or 15
        acc.add(round(v * 100, 2))
        acc.add(round(v * 100, 1))
        acc.add(float(round(v * 100)))
        # large figures get quoted in millions or billions
        if abs(v) >= 1e6:
            acc.add(round(v / 1e6, 2))
            acc.add(round(v / 1e6, 1))
            acc.add(float(round(v / 1e6)))
        if abs(v) >= 1e9:
            acc.add(round(v / 1e9, 2))
            acc.add(round(v / 1e9, 1))
        if abs(v) >= 1e3:
            acc.add(round(v / 1e3, 1))
            acc.add(float(round(v / 1e3)))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _numbers_in_payload(k, acc)
            _numbers_in_payload(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in_payload(v, acc)
    elif isinstance(obj, str):
        for n in _numbers_in(obj):
            acc.add(n)
            acc.add(round(n, 2))
    return acc


def ungrounded_numbers(answer: str, payload: Any) -> list[float]:
    """Numbers in the answer that cannot be traced to the run. Empty is good."""
    known = _numbers_in_payload(payload)
    bad: list[float] = []
    for n in _numbers_in(answer):
        if n in _ALLOWED_BARE:
            continue
        if n in known or round(n, 2) in known or float(round(n)) in known:
            continue
        bad.append(n)
    return bad


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def ask(question: str, run: dict, max_tokens: int = 2000) -> dict:
    """Answer a question about one run. Raises AnalystUnavailable without a key."""
    if not available():
        raise AnalystUnavailable(status()["reason"])
    if not question.strip():
        raise ValueError("question must not be empty")

    from openai import OpenAI

    client = OpenAI(api_key=_api_key(), base_url=BASE_URL)
    payload = json.dumps(run, separators=(",", ":"), default=str)

    try:
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            # Low temperature: this is extraction and explanation over a fixed
            # payload, not writing. Creativity here is indistinguishable from
            # fabrication, which the guard below would reject anyway.
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "system", "content": f"THE RUN:\n{payload}"},
                {"role": "user", "content": question.strip()},
            ],
        )
    except Exception as exc:  # provider errors must not 500 the dashboard
        raise AnalystUnavailable(f"{type(exc).__name__}: {exc}") from exc

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        return {
            "answer": None,
            "refused": True,
            "reason": "The model declined to answer this question.",
            "grounded": None,
            "ungrounded_numbers": [],
        }

    text = (choice.message.content or "").strip()
    bad = ungrounded_numbers(text, run)

    return {
        "answer": text if not bad else None,
        "withheld": bool(bad),
        "refused": False,
        "grounded": not bad,
        "ungrounded_numbers": bad,
        "reason": (
            None if not bad else
            "The answer contained figures that could not be traced to this run, "
            "so it was withheld. This is the numeric guard working as intended."
        ),
        "model": MODEL,
        "usage": {
            "input_tokens": getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
        },
    }


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def demo() -> None:
    run = {
        "headline": {"eal": 1934716.0, "insured_share": 0.1537, "asset_count": 12},
        "assets": [{"name": "Botlek Chemical Terminal", "eal": 380191.115}],
        "provenance": {"scenario": "ssp585", "degraded": False},
    }

    # Number extraction handles separators, decimals and negatives.
    assert _numbers_in("EAL is 1,934,716 and the share is 15.37%") == [1934716.0, 15.37]
    assert _numbers_in("no digits here") == []
    assert -4.5 in _numbers_in("a drop of -4.5")

    # A faithful answer passes.
    good = "Expected annual loss is 1,934,716 [headline.eal] across 12 assets."
    assert ungrounded_numbers(good, run) == [], ungrounded_numbers(good, run)

    # A percentage restatement of a stored share passes.
    pctform = "Only 15.37% of the loss is insured [headline.insured_share]."
    assert ungrounded_numbers(pctform, run) == [], ungrounded_numbers(pctform, run)

    # A millions restatement passes.
    millions = "That is 1.93 million a year [headline.eal]."
    assert ungrounded_numbers(millions, run) == [], ungrounded_numbers(millions, run)

    # An invented number is caught. This is the whole point of the module.
    bad = "Expected annual loss is 1,934,716 and rises to 4,200,000 by 2040."
    caught = ungrounded_numbers(bad, run)
    assert 4200000.0 in caught, f"the guard must catch a fabricated figure, got {caught}"

    # A plausible but wrong arithmetic result is caught too.
    wrong = "The uninsured portion is 1,637,000."
    assert ungrounded_numbers(wrong, run), "derived arithmetic must not pass silently"

    # Small integers used as prose are not flagged.
    prose = "There are 3 reasons this matters, and 2 of them are structural."
    assert ungrounded_numbers(prose, run) == []

    # Nested values are reachable.
    nested = "Botlek Chemical Terminal contributes 380,191.12 [assets[0].eal]."
    assert ungrounded_numbers(nested, run) == [], ungrounded_numbers(nested, run)

    # An empty question is rejected before any API call.
    if available():
        try:
            ask("   ", run)
            raise AssertionError("empty question must be rejected")
        except ValueError:
            pass
    else:
        try:
            ask("anything", run)
            raise AssertionError("must refuse without a key")
        except AnalystUnavailable:
            pass

    st = status()
    assert "available" in st
    print(f"analyst.py self-check passed (available={st['available']})")


if __name__ == "__main__":
    demo()
