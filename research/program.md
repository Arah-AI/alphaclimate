# Research program: the AlphaClimate risk engine

You are running one experiment in an automated research loop. Every experiment is
one git commit. The commit is public and permanent. Someone will read it.

## The goal

`research/invariants.py` scores this engine against 35 physical, financial and
mathematical invariants. Your job is to make more of them hold.

There is no ground truth in this repository: no backtest, no historical loss set,
no calibration target. So the score is not accuracy. It is whether the engine
obeys the properties it must obey to mean anything at all.

## What you may edit

Only these three files:

    api/app/engine.py      the loss integration and the vulnerability curve
    api/app/finance.py     the financial translation
    api/app/compute.py     the portfolio assembly

Anything you change outside them is reverted before you are scored, and the
attempt is recorded in the commit message under a `Tampered:` trailer. That
includes `research/`, `tests/`, `data/`, and `hazard.py`, `curves.py`,
`portfolio.py`, `protection.py`.

The eval is not reachable. Do not spend budget trying.

## The one rule that matters

**Weakening the model to satisfy a check is a failure, not a win.**

Clamping a number into a plausible band, widening a tolerance, special-casing an
asset id, or returning a constant that happens to pass: these all raise the score
and destroy the product. This engine exists to expose silent extrapolation. An
invariant that holds because you hid the problem is worse than one that fails
honestly.

Five of the 35 invariants exist only to catch that. They are the anchors, and a
zeroed or clamped model fails all five while passing every bound.

If you conclude a check is wrong, say so in your findings and leave it failing.
That is a legitimate outcome of an experiment.

## Method

You have a fixed wall-clock budget. Spend it on **one hypothesis**, not five. A
single well-argued change that moves two invariants beats four speculative edits.

Verify before you finish:

    python3 -B research/invariants.py | python3 -m json.tool
    python3 -m pytest tests/ -q

Both run in under a second. There is no excuse for an unverified change. The
engine is fully deterministic, has no RNG, and reads no network. Note that
`curves.load()` and `hazard._cache()` are memoised, so always use a fresh
process.

Two module self-checks currently assert the broken behaviour: `finance.demo()`
asserts that a high deductible recovers exactly zero, and `engine.demo()` asserts
a positional `losses[:3] == [0, 0, 0]`. When you fix the underlying model these
will fail, and updating them is part of your change, not a workaround. The
invariant harness is frozen; the module self-checks are not.

## Using search

You have web search and fetch. Use them when a **domain fact** would change your
fix: how a deductible and limit are actually applied to an expected annual loss,
what a FLOPROS standard of protection means, how JRC or HAZUS depth-damage curves
are meant to be indexed, how catastrophe models treat correlated perils at one
site.

Do not search to decide whether an invariant is right, and do not search in place
of reading the code in front of you. One focused lookup that settles a modelling
question is worth the budget. Five tabs of background reading is not.

Record what you looked up and what it said, in your findings. A verified domain
fact is one of the most useful things you can leave the next experiment.

## Leads

These are symptoms observed in the current output, all verified by running the
engine. The diagnosis is yours. Some share a root cause and some may be red
herrings.

1. `compute.asset_detail("tj-priok")` reports 179,722,589 of annual physical
   damage. `compute.summary()` reports 5,063,840 for the same asset, the same
   scenario, the same run. The dashboard shows both. One path applies a
   classification the other does not. Find which number is right and why the two
   paths diverged.

2. `engine.py`: `probs` is built as `[1.0 / rp for rp in rps]`, and two lines
   later `anchor_p = min(1.0, 1.0 / rps[0])`. Work out when the guard
   `anchor_p > probs[0]` is ever true, given that every return period in
   `data/hazard_cache.json` starts at 2.0 or 5.0.

3. Refining the return-period grid 32x by log-RP interpolation moves the
   portfolio EAL by -8.09%, and tj-priok riverine by -31.1%. A converged
   integral does not move when you refine the grid. Note the `ponytail:` comment
   at `engine.py:105` about the protection step, and consider whether it is
   confessing to the same thing.

4. cat-lai's flood damage fractions sum to 1.563 at the rare return periods:
   0.56 from coastal and 1.00 from riverine, added. The asset is destroyed 1.56
   times. Both perils map to the same curve family in `curves.py`.

5. Four assets (cilegon, bangkok-lp, manila-pp, hamburg-dc) read exactly 0.00 m
   of flood depth at every return period and are dropped at `compute.py:93`. A
   Bang Na logistics park with no flood exposure is not a result. `hazard.py`'s
   own docstring says a silent zero is the worst failure this system can have.
   What should a reading that is entirely zero mean?

6. Ten of twelve assets receive exactly zero insurance recovery while paying a
   premium of 1.35x their expected annual loss. Read the docstring of
   `_insurance_recovery` and then read the type of the value passed to it at
   `finance.py:130`.

7. Read the docstring of `PerilResult.mean_damage_fraction` (`compute.py:72-81`),
   which claims it is derived from the same integration as the EAL "so the two
   cannot drift". Then read the three lines below it. Expected business
   interruption exceeds expected physical damage by 9.5x at rotterdam-chem.

8. `compute._run_id` takes a `portfolio_id` argument and is called at
   `compute.py:204` with a string literal. Changing an asset's value from
   420,000,000 to 1 leaves the run id unchanged.

9. `portfolio_spread` sweeps `n_variants` ranks, where `n_variants` is the max
   across perils. Coastal has 1 variant and wind has 2. `hazard.read` falls back
   to the same variant for out-of-range ranks. 54 sweep points carry 30 distinct
   values, and the median and the driver attribution are computed over all 54.

10. Seven covenant breaches are reported. hcmc-tower has a `dscr_before` of 0.73,
    so it breaches at zero climate loss. Consider what a climate report is
    claiming when it attributes that breach to climate.

## Reporting

End your final message with exactly these two blocks, both headed, in this order.
Anything before them is ignored.

    ## Summary
    Route asset_detail through the same permanent-inundation exclusion as summary

One line, under 72 characters, imperative mood. It becomes the commit subject, so
it must say what you changed. Not "Done", not a score, not a list of everything
you touched. If you changed nothing, say what you ruled out instead.

Then the findings block, which is appended to `research/FINDINGS.md` and read by
every later experiment:

    ## Findings
    - The two EAL paths diverge because ... (root cause, in one or two lines)
    - Confirmed dead end: ... (so nobody repeats it)
    - Looked up: ... said ... (source, and what it settles)
    - Invariant X looks wrong because ... (if you concluded that)

Write findings that save the next experiment time. A negative result is a real
finding and belongs in the log. If you changed nothing, say why: that is also a
result, and the loop records it honestly rather than hiding it.
