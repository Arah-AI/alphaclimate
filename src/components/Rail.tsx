"use client";

import * as React from "react";
import { clsx } from "clsx";
import {
  BarChart3,
  Building2,
  Upload,
  GitCompareArrows,
  Globe2,
  Zap,
  ScrollText,
  SlidersHorizontal,
} from "lucide-react";

export type ViewId =
  | "dashboard"
  | "assets"
  | "upload"
  | "map"
  | "disagreement"
  | "adaptation"
  | "ledger"
  | "assumptions";

const GROUPS: { id: ViewId; label: string; Icon: React.ElementType }[][] = [
  [
    { id: "dashboard", label: "Dashboard", Icon: BarChart3 },
    { id: "assets", label: "Assets", Icon: Building2 },
    { id: "upload", label: "Upload a portfolio", Icon: Upload },
    { id: "map", label: "Hazard map", Icon: Globe2 },
  ],
  [
    { id: "disagreement", label: "Model disagreement", Icon: GitCompareArrows },
    { id: "adaptation", label: "Adaptation", Icon: Zap },
    { id: "ledger", label: "Provenance ledger", Icon: ScrollText },
  ],
];

export function Rail({
  view,
  onView,
  assetCount,
}: {
  view: ViewId;
  onView: (v: ViewId) => void;
  assetCount: number;
}) {
  return (
    <nav
      aria-label="Main"
      className={clsx(
        "bg-charcoal rounded-[26px] w-[68px] shrink-0",
        "flex flex-col items-center gap-6 py-5 px-3",
        "sticky top-4 max-h-[calc(100dvh-2rem)]",
      )}
    >
      <span
        className="grid place-items-center w-9 h-9 shrink-0"
        title="AlphaClimate"
        aria-label="AlphaClimate"
      >
        <svg viewBox="0 0 24 24" width="23" height="23" aria-hidden>
          <circle cx="9.5" cy="9.5" r="5.4" fill="#fff" />
          <circle cx="15.6" cy="15.2" r="4.1" fill="#fff" opacity=".5" />
        </svg>
      </span>

      {GROUPS.map((group, gi) => (
        <div key={gi} className="flex flex-col items-center gap-2.5">
          {group.map(({ id, label, Icon }) => {
            const active = view === id;
            return (
              <button
                key={id}
                type="button"
                title={label}
                aria-label={label}
                aria-current={active ? "page" : undefined}
                onClick={() => onView(id)}
                className={clsx(
                  "relative grid place-items-center w-11 h-11 rounded-[14px] transition-colors",
                  active
                    ? "bg-white text-charcoal"
                    : "bg-charcoal-2 text-white/55 hover:text-white hover:bg-white/15",
                )}
              >
                <Icon size={19} strokeWidth={active ? 2.2 : 1.9} aria-hidden />
                {id === "assets" && assetCount > 0 && (
                  <span
                    className="absolute -top-1 -right-1 min-w-[19px] h-[19px] px-1 rounded-full bg-brand text-white text-micro font-semibold grid place-items-center tabular-nums"
                    aria-hidden
                  >
                    {assetCount}
                  </span>
                )}
              </button>
            );
          })}
          {gi === 0 && <span className="w-6 h-px bg-white/12 mt-1.5" aria-hidden />}
        </div>
      ))}

      <button
        type="button"
        title="Assumptions"
        aria-label="Assumptions"
        aria-current={view === "assumptions" ? "page" : undefined}
        onClick={() => onView("assumptions")}
        className={clsx(
          "mt-auto grid place-items-center w-11 h-11 rounded-[14px] transition-colors",
          view === "assumptions"
            ? "bg-white text-charcoal"
            : "bg-charcoal-2 text-white/55 hover:text-white hover:bg-white/15",
        )}
      >
        <SlidersHorizontal size={19} strokeWidth={1.9} aria-hidden />
      </button>
    </nav>
  );
}
