"use client";

import * as React from "react";
import { clsx } from "clsx";
import { Card, CardHead, Chip, LegendDots, Skeleton, StatBig, TickBar } from "./ui";
import { SpreadRail } from "./charts";
import { money, pct, perilLabel } from "@/lib/format";
import type { Summary } from "@/lib/types";
import type { CurveGaps, HeadlineExtras } from "@/lib/insight";

const EYEBROW = "text-micro uppercase tracking-[0.1em] text-muted font-semibold";

const sentenceCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/* ============================================================ run control */

/**
 * One control for the two things that move every number on the page: how warm
 * it gets and how long you hold the asset. Native radios and a native range
 * input, so keyboard, screen reader and touch behaviour come for free.
 */
export function ScenarioBar({
  scenarios,
  scenario,
  onScenario,
  horizon,
  onHorizon,
  busy,
}: {
  scenarios: { id: string; label: string }[];
  scenario: string;
  onScenario: (id: string) => void;
  horizon: number;
  onHorizon: (years: number) => void;
  busy: boolean;
}) {
  const active = scenarios.find((s) => s.id === scenario);
  const [head, tail] = (active?.label ?? scenario).split(" · ");

  return (
    <section className="rounded-[20px] bg-card border border-line px-4 sm:px-5 py-3.5 flex flex-wrap items-center gap-x-6 gap-y-3">
      <fieldset className="min-w-0">
        <legend className="sr-only">Climate scenario</legend>
        <div className="inline-flex flex-wrap rounded-full bg-canvas border border-line-2 p-[3px] gap-[2px]">
          {scenarios.map((s) => {
            const on = s.id === scenario;
            return (
              <label
                key={s.id}
                className={clsx(
                  "cursor-pointer rounded-full px-3 py-[7px] text-support font-medium whitespace-nowrap transition-colors",
                  "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-brand has-[:focus-visible]:outline-offset-2",
                  on ? "bg-charcoal text-white" : "text-ink-2 hover:text-ink",
                )}
              >
                <input
                  type="radio"
                  name="ac-scenario"
                  className="sr-only"
                  value={s.id}
                  checked={on}
                  onChange={() => onScenario(s.id)}
                />
                {s.label.split(" · ")[0]}
              </label>
            );
          })}
        </div>
      </fieldset>

      <label className="flex items-center gap-3 min-w-0">
        <span className="text-support text-muted whitespace-nowrap">Hold period</span>
        <input
          type="range"
          min={5}
          max={40}
          step={1}
          value={horizon}
          aria-label="Hold period in years"
          aria-valuetext={`${horizon} years`}
          onChange={(e) => onHorizon(Number(e.target.value))}
          className="w-[130px] sm:w-[170px] accent-[#0b6be1] cursor-pointer"
        />
        <b className="text-ui font-semibold tabular-nums w-[46px]">{horizon} yr</b>
      </label>

      <p className="text-support text-muted ml-auto flex items-center gap-2 min-w-0">
        <span className="truncate">
          {head}
          {tail ? `, ${tail}` : ""} over {horizon} years
        </span>
        <span
          role="status"
          aria-live="polite"
          className={clsx(
            "text-support font-medium whitespace-nowrap",
            busy ? "text-brand" : "sr-only",
          )}
        >
          {busy ? "recomputing…" : "up to date"}
        </span>
      </p>
    </section>
  );
}

/* =============================================================== lead block */

export function LeadBlock({
  data,
  busy,
  gaps,
}: {
  data: Summary | null;
  busy: boolean;
  gaps: CurveGaps | null;
}) {
  const h = data?.headline as (Summary["headline"] & HeadlineExtras) | undefined;
  const currency = data?.portfolio.currency ?? "USD";

  if (busy || !h) {
    return (
      <Card>
        <Skeleton className="h-[188px]" />
      </Card>
    );
  }

  const s = h.eal_spread;
  const drivers = Object.entries(s.by_driver).sort((a, b) => b[1].share - a[1].share);
  const topDriver = drivers[0];
  const drowned = h.permanently_inundated_count ?? 0;
  const writedown = h.permanent_writedown ?? 0;

  return (
    <Card>
      <div className="flex flex-col lg:flex-row gap-7 lg:gap-9">
        {/* ------------------------------------------- the number and its range */}
        <div className="flex-1 min-w-0">
          <p className={EYEBROW}>Expected annual loss</p>
          <div className="flex flex-wrap items-baseline gap-2.5 mt-1.5 mb-1">
            <StatBig>{money(h.eal, currency)}</StatBig>
            <Chip tone="up" arrow="up">
              {pct(h.eal_pct_of_value, 2)} of insured value
            </Chip>
            <Chip tone="flat">{pct(h.tail_share, 0)} from the tail</Chip>
          </div>

          <p className="text-prose text-ink-2 leading-relaxed ac-prose mb-5">
            Quoted as a range, because a point estimate here is a choice dressed as a
            fact. Run {s.n} defensible ways, this portfolio answers anywhere from{" "}
            <b className="font-semibold text-ink">{money(s.low, currency)}</b> to{" "}
            <b className="font-semibold text-ink">{money(s.high, currency)}</b> a year,
            a {s.range_ratio.toFixed(2)}x spread.
            {topDriver && (
              <>
                {" "}
                {topDriver[0].replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase())}{" "}
                choice alone accounts for {pct(topDriver[1].share, 0)} of it.
              </>
            )}
          </p>

          <div className="mb-3">
            <LegendDots
              items={[
                { label: "This run", color: "#16181d", value: money(h.eal, currency) },
                {
                  label: `Median of ${s.n} runs`,
                  color: "#0b6be1",
                  value: money(s.median, currency),
                },
              ]}
            />
          </div>
          <SpreadRail
            low={s.low}
            median={s.median}
            high={s.high}
            mark={h.eal}
            currency={currency}
          />
        </div>

        {/* ------------------------------------------------ permanent writedown */}
        <div className="lg:w-[310px] shrink-0 border-t lg:border-t-0 lg:border-l border-line pt-6 lg:pt-0 lg:pl-9">
          <p className={EYEBROW}>Permanent write-down</p>
          {drowned > 0 ? (
            <>
              <div className="flex flex-wrap items-baseline gap-2.5 mt-1.5 mb-3">
                <StatBig>{money(writedown, currency)}</StatBig>
                <Chip tone="danger">
                  {pct(h.permanent_writedown_pct ?? 0, 1)} of value
                </Chip>
              </div>
              <p className="text-ui text-ink-2 leading-relaxed ac-prose mb-4">
                {drowned} asset{drowned > 1 ? "s are" : " is"} projected to sit below
                the waterline permanently, not to be hit by an event. There is no
                return period and nothing to insure, so this sits outside the annual
                loss on the left rather than inflating it.
              </p>
              <TickBar
                label={`Permanent write-down is ${pct(
                  h.permanent_writedown_pct ?? 0,
                  1,
                )} of portfolio value`}
                height={22}
                segments={[
                  { value: writedown, color: "#16181d" },
                  { value: Math.max(0, h.total_value - writedown), color: "#e8eaee" },
                ]}
              />
              <p className="text-support text-muted mt-2.5">
                Written off against {money(h.total_value, currency)} of insured value
              </p>
            </>
          ) : (
            <>
              <div className="flex items-baseline gap-2.5 mt-1.5 mb-3">
                <StatBig>None</StatBig>
              </div>
              <p className="text-ui text-ink-2 leading-relaxed">
                No asset is projected to be permanently inundated under this scenario
                and horizon. Raise the scenario to see whether that holds.
              </p>
            </>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------------ provenance */}
      <div className="mt-6 pt-4 border-t border-line flex flex-wrap items-center gap-x-5 gap-y-2 text-support text-muted">
        <span>
          <b className="text-ink font-semibold tabular-nums">{s.n}</b> model runs
        </span>
        <span>
          <b className="text-ink font-semibold tabular-nums">
            {gaps?.curves_loaded ?? "—"}
          </b>{" "}
          damage curves applied
        </span>
        <span>
          <b className="text-ink font-semibold tabular-nums">
            {gaps?.gaps.length ?? "—"}
          </b>{" "}
          hazards refused
        </span>
        <a
          href="#not-modelled"
          className="text-brand font-medium hover:underline ml-auto"
        >
          What we will not model →
        </a>
      </div>
    </Card>
  );
}

/* ========================================================== the refusals */

export function NotModelled({
  gaps,
  error,
}: {
  gaps: CurveGaps | null;
  error: string | null;
}) {
  return (
    <Card id="not-modelled" className="scroll-mt-4">
      <CardHead title="What we will not model">
        {gaps ? (
          <Chip tone="warn">{gaps.gaps.length} refused</Chip>
        ) : (
          <span className="sr-only">loading</span>
        )}
      </CardHead>

      <p className="text-ui text-ink-2 leading-relaxed ac-prose mb-4">
        Measured at our assets, priced at nothing, because no damage function exists
        that we can defend. An absence here is a refusal, not a zero.
      </p>

      {error && (
        <p className="text-ui text-muted">
          The refusal ledger did not load: {error}
        </p>
      )}
      {!gaps && !error && <Skeleton className="h-[220px]" />}

      {gaps && (
        <ul className="flex flex-col max-h-[300px] overflow-y-auto ac-scroll -mx-1 px-1">
          {gaps.gaps.map((g, i) => (
            <li key={`${g.hazard}-${i}`} className="border-t border-line first:border-0">
              <details className="group py-2.5">
                <summary className="cursor-pointer list-none flex items-start gap-2.5 text-ui">
                  <span
                    aria-hidden
                    className="mt-[6px] w-[7px] h-[7px] rounded-full bg-warn shrink-0"
                  />
                  <span className="min-w-0 flex-1">
                    <b className="font-medium">{sentenceCase(perilLabel(g.hazard))}</b>
                    <span className="block text-support text-muted">{g.exposure}</span>
                  </span>
                  <span
                    aria-hidden
                    className="text-muted text-support shrink-0 group-open:hidden"
                  >
                    why →
                  </span>
                </summary>
                <div className="pl-[17px] pt-2 pb-1 flex flex-col gap-2">
                  <p className="text-support text-ink-2 leading-relaxed">{g.why}</p>
                  <p className="text-support text-muted leading-relaxed">
                    <b className="text-ink-2 font-semibold">What would close it: </b>
                    {g.what_would_close_it}
                  </p>
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
