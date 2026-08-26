"use client";

import * as React from "react";
import { clsx } from "clsx";
import { ChevronDown } from "lucide-react";

/* ------------------------------------------------------------------ card */

export function Card({
  className,
  children,
  tone = "light",
  ...rest
}: React.HTMLAttributes<HTMLElement> & { tone?: "light" | "dark" | "brand" }) {
  return (
    <section
      className={clsx(
        "rounded-[20px] p-5 sm:p-6 flex flex-col min-w-0",
        tone === "light" && "bg-card border border-line",
        tone === "dark" && "bg-charcoal text-white",
        tone === "brand" &&
          "text-white bg-[linear-gradient(155deg,#1f7ae8_0%,#0b6be1_45%,#07439a_100%)] relative overflow-hidden",
        className,
      )}
      {...rest}
    >
      {children}
    </section>
  );
}

export function CardHead({
  title,
  icon,
  children,
  tone = "light",
  stack = false,
}: {
  title: string;
  icon?: React.ReactNode;
  children?: React.ReactNode;
  tone?: "light" | "dark";
  stack?: boolean;
}) {
  return (
    <header
      className={clsx(
        "flex gap-3 mb-4",
        stack ? "items-start justify-between" : "items-center justify-between",
      )}
    >
      <h2
        className={clsx(
          "font-display text-[19px] leading-tight font-medium flex items-center gap-2 min-w-0",
          tone === "dark" ? "text-white" : "text-ink",
        )}
      >
        {icon}
        <span className="truncate">{title}</span>
      </h2>
      {children ?? <Grip tone={tone} />}
    </header>
  );
}

/** The six-dot drag affordance from the reference. Decorative only. */
export function Grip({ tone = "light" }: { tone?: "light" | "dark" }) {
  return (
    <span
      aria-hidden
      className="grid grid-cols-2 gap-[3px] shrink-0 p-1"
      title="Drag to rearrange"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <i
          key={i}
          className={clsx(
            "block w-[3px] h-[3px] rounded-full",
            tone === "dark" ? "bg-white/35" : "bg-line-2",
          )}
        />
      ))}
    </span>
  );
}

/* ------------------------------------------------------------------ text */

export function StatBig({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "font-display font-medium tabular-nums tracking-[-0.02em]",
        "text-[38px] sm:text-[44px] leading-[1.05]",
        className,
      )}
    >
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ chip */

type ChipTone = "up" | "down" | "flat" | "ghost" | "dark" | "danger" | "warn";

const CHIP_TONE: Record<ChipTone, string> = {
  up: "bg-brand-tint text-brand",
  down: "bg-brand-tint text-brand",
  flat: "bg-brand-tint text-brand",
  ghost: "bg-white/20 text-white",
  dark: "bg-white/12 text-white",
  danger: "bg-danger-tint text-danger",
  warn: "bg-warn-tint text-warn",
};

export function Chip({
  tone = "flat",
  arrow,
  children,
  title,
}: {
  tone?: ChipTone;
  arrow?: "up" | "down";
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex items-center gap-1 rounded-full px-2 py-[3px]",
        "text-[12px] font-medium leading-none whitespace-nowrap tabular-nums",
        CHIP_TONE[tone],
      )}
    >
      {arrow && <span aria-hidden>{arrow === "up" ? "↗" : "↘"}</span>}
      {children}
    </span>
  );
}

/* ---------------------------------------------------------------- select */

export function Select({
  value,
  onChange,
  options,
  label,
  tone = "light",
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  label: string;
  tone?: "light" | "dark";
}) {
  return (
    <div
      className={clsx(
        "inline-flex items-center gap-1 rounded-full pl-3 pr-2 py-[6px] shrink-0",
        "text-[13px] font-medium",
        tone === "light"
          ? "bg-card border border-line-2 text-ink"
          : "bg-white/12 text-white",
      )}
    >
      <select
        aria-label={label}
        className="ac-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} className="text-ink">
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown size={15} aria-hidden className="opacity-70 shrink-0" />
    </div>
  );
}

/* --------------------------------------------------------------- tickbar */

/**
 * The fine tick meter from the reference: a run of thin vertical bars, filled
 * left to right in segment order with the remainder left grey. Encodes a
 * composition, not a single value, which is why it beats a plain progress bar.
 */
export function TickBar({
  segments,
  ticks = 56,
  height = 26,
  label,
}: {
  segments: { value: number; color: string }[];
  ticks?: number;
  height?: number;
  label: string;
}) {
  const total = segments.reduce((s, x) => s + Math.max(0, x.value), 0);
  const counts = segments.map((s) =>
    total > 0 ? Math.round((Math.max(0, s.value) / total) * ticks) : 0,
  );
  const colors: string[] = [];
  counts.forEach((c, i) => {
    for (let k = 0; k < c && colors.length < ticks; k++) {
      colors.push(segments[i].color);
    }
  });

  return (
    <div
      role="img"
      aria-label={label}
      className="flex items-end gap-[2px] w-full"
      style={{ height }}
    >
      {Array.from({ length: ticks }).map((_, i) => (
        <span
          key={i}
          className="flex-1 rounded-[2px] min-w-[2px]"
          style={{ height: "100%", background: colors[i] ?? "#e8eaee" }}
        />
      ))}
    </div>
  );
}

export function LegendDots({
  items,
  tone = "light",
}: {
  items: { label: string; color: string; value?: string }[];
  tone?: "light" | "dark";
}) {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1">
      {items.map((it) => (
        <li
          key={it.label}
          className={clsx(
            "flex items-center gap-[6px] text-[12.5px]",
            tone === "dark" ? "text-white/70" : "text-muted",
          )}
        >
          <i
            aria-hidden
            className="w-[7px] h-[7px] rounded-full shrink-0"
            style={{ background: it.color }}
          />
          <span>{it.label}</span>
          {it.value && (
            <b
              className={clsx(
                "font-semibold tabular-nums",
                tone === "dark" ? "text-white" : "text-ink",
              )}
            >
              {it.value}
            </b>
          )}
        </li>
      ))}
    </ul>
  );
}

/* ---------------------------------------------------------------- states */

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={clsx(
        "animate-pulse rounded-lg bg-line",
        className,
      )}
    />
  );
}

export function ErrorNote({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="rounded-[20px] border border-danger/25 bg-danger-tint p-6"
    >
      <p className="font-display text-[19px] text-ink mb-1">
        The risk engine did not answer
      </p>
      <p className="text-[14px] text-ink-2 mb-3">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded-full bg-ink text-white text-[13px] font-medium px-4 py-2 hover:bg-charcoal-2 transition-colors"
        >
          Try again
        </button>
      )}
    </div>
  );
}

/** Shown when a number is outside the calibration range of its damage curve. */
export function ExtrapolationFlag({ title }: { title?: string }) {
  return (
    <span
      title={title ?? "Hazard intensity sits outside the damage curve's calibrated range"}
      className="inline-flex items-center gap-1 rounded-full bg-warn-tint text-warn px-2 py-[2px] text-[11px] font-medium whitespace-nowrap"
    >
      ▲ extrapolated
    </span>
  );
}
