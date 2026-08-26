"use client";

import * as React from "react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Cell,
  PieChart,
  Pie,
  AreaChart,
  Area,
} from "recharts";
import { money, perilLabel, PERIL_COLORS } from "@/lib/format";
import type { PerilSlice, YearPoint } from "@/lib/types";

/* --------------------------------------------------------------- tooltip */

function DarkTip({
  title,
  rows,
}: {
  title: string;
  rows: { label: string; value: string; color?: string }[];
}) {
  return (
    <div className="rounded-xl bg-charcoal text-white px-3.5 py-2.5 shadow-lg text-support min-w-[150px]">
      <p className="font-semibold mb-1.5 tabular-nums">{title}</p>
      <ul className="flex flex-col gap-1">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-2">
            <i
              aria-hidden
              className="w-[7px] h-[7px] rounded-full shrink-0"
              style={{ background: r.color ?? "rgba(255,255,255,.55)" }}
            />
            <span className="text-white/65">{r.label}</span>
            <b className="ml-auto font-semibold">{r.value}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------- value at risk */

/** White rounded bars on the brand card. Annual loss by peril. */
export function FeatureBars({ data }: { data: PerilSlice[] }) {
  const rows = data.slice(0, 7);
  if (!rows.length) {
    return (
      <p className="text-white/70 text-ui py-8">
        No modelled loss for this scenario.
      </p>
    );
  }
  const max = Math.max(...rows.map((r) => r.eal)) || 1;

  return (
    <div className="flex items-end gap-2 h-[150px] mt-auto pt-3">
      {rows.map((r, i) => {
        const h = Math.max(6, (r.eal / max) * 100);
        return (
          <div
            key={r.peril}
            className="flex-1 h-full flex flex-col items-center gap-2 min-w-0 group"
            title={`${perilLabel(r.peril)}: ${money(r.eal)} a year`}
          >
            <div className="w-full max-w-[64px] flex-1 flex items-end">
              <div
                className={
                  "w-full rounded-[7px] transition-[height] duration-500 " +
                  (i === 0 ? "bg-white" : "bg-white/55 group-hover:bg-white/75")
                }
                style={{ height: `${h}%` }}
              />
            </div>
            <span className="text-micro text-white/70 truncate w-full text-center">
              {perilLabel(r.peril).split(" ")[0]}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------- donut */

export function PerilDonut({ data }: { data: PerilSlice[] }) {
  const rows = data.filter((d) => d.eal > 0).slice(0, 6);
  if (!rows.length) {
    return (
      <div className="h-[190px] grid place-items-center text-muted text-ui">
        No loss to attribute.
      </div>
    );
  }

  return (
    <div className="h-[190px] w-[190px] shrink-0 relative">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={rows}
            dataKey="eal"
            nameKey="peril"
            innerRadius={58}
            outerRadius={88}
            paddingAngle={3}
            cornerRadius={9}
            stroke="none"
            isAnimationActive={false}
          >
            {rows.map((r, i) => (
              <Cell key={r.peril} fill={PERIL_COLORS[i % PERIL_COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            content={(p) => {
              const d = p.payload?.[0]?.payload as PerilSlice | undefined;
              if (!d) return null;
              return (
                <DarkTip
                  title={perilLabel(d.peril)}
                  rows={[
                    { label: "Annual loss", value: money(d.eal) },
                    { label: "Share", value: `${(d.share * 100).toFixed(1)}%` },
                  ]}
                />
              );
            }}
          />
        </PieChart>
      </ResponsiveContainer>

      <div className="absolute inset-0 grid place-items-center pointer-events-none">
        <div className="text-center">
          <p className="font-display font-medium text-panel-title leading-none">
            {money(rows.reduce((s, r) => s + r.eal, 0))}
          </p>
          <p className="text-micro text-muted mt-1">a year</p>
        </div>
      </div>
    </div>
  );
}

/* --------------------------------------------------- loss by year band */

/**
 * Solid bar = median estimate. Hatched bar stacked on top = the span up to the
 * highest defensible answer across models. The disagreement is drawn, not
 * hidden in an average, which is the whole product thesis.
 */
export function YearBand({
  data,
  focusYear,
  onFocus,
}: {
  data: YearPoint[];
  focusYear?: number;
  onFocus?: (year: number) => void;
}) {
  const rows = data.map((d) => ({
    ...d,
    band: Math.max(0, d.high - d.median),
    floor: Math.max(0, d.median - d.low),
  }));

  return (
    <div className="h-[230px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={rows}
          margin={{ top: 8, right: 4, bottom: 0, left: 4 }}
          barCategoryGap="26%"
          onClick={(s) => {
            const y = Number(s?.activeLabel);
            if (Number.isFinite(y) && onFocus) onFocus(y);
          }}
        >
          <defs>
            <pattern
              id="ac-hatch"
              patternUnits="userSpaceOnUse"
              width="6"
              height="6"
              patternTransform="rotate(-45)"
            >
              <rect width="6" height="6" fill="#ffffff" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="#aecdf5" strokeWidth="2.2" />
            </pattern>
          </defs>

          <XAxis
            dataKey="year"
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 11, fill: "var(--color-muted)" }}
            interval="preserveStartEnd"
            minTickGap={4}
          />
          <YAxis hide />
          <Tooltip
            cursor={{ fill: "rgba(11,107,225,.05)" }}
            content={(p) => {
              const d = p.payload?.[0]?.payload as
                | (YearPoint & { band: number })
                | undefined;
              if (!d) return null;
              return (
                <DarkTip
                  title={`Year ${d.year}`}
                  rows={[
                    { label: "Median", value: money(d.median), color: "#0b6be1" },
                    { label: "Low", value: money(d.low), color: "#aecdf5" },
                    { label: "High", value: money(d.high), color: "#ffffff" },
                  ]}
                />
              );
            }}
          />
          <Bar dataKey="median" stackId="a" radius={[8, 8, 8, 8]} isAnimationActive={false}>
            {rows.map((r) => (
              <Cell
                key={r.year}
                fill={
                  focusYear === undefined || focusYear === r.year
                    ? "#0b6be1"
                    : "#aecdf5"
                }
                cursor={onFocus ? "pointer" : undefined}
              />
            ))}
          </Bar>
          <Bar
            dataKey="band"
            stackId="a"
            radius={[8, 8, 8, 8]}
            fill="url(#ac-hatch)"
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ------------------------------------------------------------- spark */

export function Spark({
  data,
}: {
  data: { year: number; value: number }[];
}) {
  if (data.length < 2) return <div className="h-[74px]" />;
  const last = data[data.length - 1];

  return (
    <div className="h-[74px] w-full mt-3">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="ac-spark" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ffffff" stopOpacity={0.22} />
              <stop offset="100%" stopColor="#ffffff" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="year"
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 11, fill: "rgba(255,255,255,.75)" }}
            interval="preserveStartEnd"
            minTickGap={30}
          />
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <Tooltip
            cursor={false}
            content={(p) => {
              const d = p.payload?.[0]?.payload as
                | { year: number; value: number }
                | undefined;
              if (!d) return null;
              return (
                <DarkTip
                  title={`Year ${d.year}`}
                  rows={[{ label: "Cumulative impairment", value: money(d.value) }]}
                />
              );
            }}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke="#ffffff"
            strokeWidth={2}
            fill="url(#ac-spark)"
            isAnimationActive={false}
            dot={false}
            activeDot={{ r: 4, fill: "#fff", stroke: "none" }}
          />
        </AreaChart>
      </ResponsiveContainer>
      <span className="sr-only">
        Cumulative impairment reaches {money(last.value)} by year {last.year}.
      </span>
    </div>
  );
}

/* -------------------------------------------------------- vertical stack */

/** The segmented column beside the ranked list in the reference. */
export function StackBar({
  segments,
}: {
  segments: { value: number; color: string; hatch?: boolean }[];
}) {
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  return (
    <div
      aria-hidden
      className="w-[26px] shrink-0 flex flex-col gap-[3px] self-stretch min-h-[130px]"
    >
      {segments.map((s, i) => (
        <div
          key={i}
          className={"rounded-[5px] " + (s.hatch ? "ac-hatch border border-line" : "")}
          style={{
            flexGrow: Math.max(s.value / total, 0.04),
            background: s.hatch ? undefined : s.color,
          }}
        />
      ))}
    </div>
  );
}

/* ---------------------------------------------------------- spread rail */

/**
 * The range every other vendor collapses into one number: a hatched track from
 * the lowest to the highest defensible answer, with the median of all runs and
 * the number this dashboard is showing marked on it. Two marks, because where
 * the headline sits inside the spread is itself a finding.
 */
export function SpreadRail({
  low,
  median,
  high,
  mark,
  markLabel = "This run",
  currency = "USD",
}: {
  low: number;
  median: number;
  high: number;
  mark?: number;
  markLabel?: string;
  currency?: string;
}) {
  const span = high - low;
  const at = (v: number) =>
    span > 0 ? Math.min(100, Math.max(0, ((v - low) / span) * 100)) : 50;

  return (
    <div className="w-full">
      <div className="relative h-[14px] rounded-full bg-line overflow-hidden">
        <span aria-hidden className="absolute inset-0 ac-hatch bg-white/70" />
      </div>
      <div className="relative h-0">
        <span
          aria-hidden
          title={`Median of every run ${money(median, currency)}`}
          className="absolute -top-[19px] w-[3px] h-[24px] rounded-full bg-brand"
          style={{ left: `calc(${at(median)}% - 1.5px)` }}
        />
        {mark !== undefined && (
          <span
            aria-hidden
            title={`${markLabel} ${money(mark, currency)}`}
            className="absolute -top-[22px] w-[3px] h-[30px] rounded-full bg-charcoal"
            style={{ left: `calc(${at(mark)}% - 1.5px)` }}
          />
        )}
      </div>
      <div className="flex justify-between mt-3 text-support tabular-nums text-muted">
        <span>{money(low, currency)} lowest defensible</span>
        <span>{money(high, currency)} highest defensible</span>
      </div>
      <span className="sr-only">
        Across every defensible model combination the answer runs from{" "}
        {money(low, currency)} to {money(high, currency)} a year, with a median of{" "}
        {money(median, currency)}
        {mark !== undefined
          ? `, and ${markLabel.toLowerCase()} at ${money(mark, currency)}`
          : ""}
        .
      </span>
    </div>
  );
}
