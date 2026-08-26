"use client";

import * as React from "react";
import { X } from "lucide-react";
import { Card, CardHead, Chip, Skeleton, StatBig, ErrorNote, ExtrapolationFlag } from "./ui";
import { getAsset } from "@/lib/api";
import type { AssetDetail, Assumptions, Summary } from "@/lib/types";
import {
  money,
  moneyExact,
  pct,
  ratio,
  num,
  perilLabel,
  BAND_COLOR,
  BAND_TINT,
  BAND_LABEL,
} from "@/lib/format";

/* ================================================================ assets */

export function AssetsView({
  data,
  onOpen,
}: {
  data: Summary;
  onOpen: (id: string) => void;
}) {
  const [q, setQ] = React.useState("");
  const cur = data.portfolio.currency;
  const rows = data.assets.filter((a) =>
    `${a.name} ${a.country} ${a.sector}`.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <Card>
      <CardHead title={`${rows.length} assets`}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by name, country or sector"
          aria-label="Filter assets"
          className="rounded-full border border-line-2 bg-card px-4 py-2 text-[13px] w-[280px] max-w-full placeholder:text-muted"
        />
      </CardHead>

      <div className="overflow-x-auto ac-scroll -mx-1 px-1">
        <table className="w-full min-w-[880px] border-collapse">
          <thead>
            <tr className="text-left">
              {[
                "Asset",
                "Value",
                "Annual loss",
                "% of value",
                "Impairment",
                "Top peril",
                "Band",
                "Flags",
              ].map((th) => (
                <th
                  key={th}
                  className="text-[11px] uppercase tracking-[0.08em] text-muted font-semibold pb-3 pr-4 whitespace-nowrap"
                >
                  {th}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr
                key={a.id}
                onClick={() => onOpen(a.id)}
                tabIndex={0}
                role="button"
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpen(a.id);
                  }
                }}
                className="border-t border-line cursor-pointer hover:bg-canvas transition-colors"
              >
                <td className="py-3 pr-4">
                  <span className="block text-[13.5px] font-medium">{a.name}</span>
                  <span className="block text-[12px] text-muted">
                    {a.country} · {a.sector}
                  </span>
                </td>
                <td className="py-3 pr-4 text-[13.5px] tabular-nums whitespace-nowrap">
                  {money(a.value, cur)}
                </td>
                <td className="py-3 pr-4 text-[13.5px] tabular-nums font-semibold whitespace-nowrap">
                  {money(a.eal, cur)}
                </td>
                <td className="py-3 pr-4 text-[13.5px] tabular-nums text-muted">
                  {pct(a.eal_pct, 2)}
                </td>
                <td className="py-3 pr-4 text-[13.5px] tabular-nums whitespace-nowrap">
                  {money(a.impairment, cur)}
                  <span className="text-muted"> ({pct(a.impairment_pct, 1)})</span>
                </td>
                <td className="py-3 pr-4 text-[13px] whitespace-nowrap">
                  {perilLabel(a.top_peril)}
                </td>
                <td className="py-3 pr-4">
                  <span
                    className="inline-block rounded-full px-2.5 py-[3px] text-[11.5px] font-semibold whitespace-nowrap"
                    style={{
                      background: BAND_TINT[a.band],
                      color: BAND_COLOR[a.band],
                    }}
                  >
                    {BAND_LABEL[a.band]}
                  </span>
                </td>
                <td className="py-3 pr-1">
                  <span className="flex gap-1.5 flex-wrap">
                    {a.covenant_breach && <Chip tone="danger">covenant</Chip>}
                    {a.uninsurable && <Chip tone="warn">cover limit</Chip>}
                    {a.extrapolated && <ExtrapolationFlag />}
                  </span>
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={8} className="py-10 text-center text-[13.5px] text-muted">
                  No asset matches “{q}”.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/* ========================================================= disagreement */

export function DisagreementView({ data }: { data: Summary }) {
  const s = data.headline.eal_spread;
  const cur = data.portfolio.currency;

  return (
    <div className="grid gap-4 lg:grid-cols-2 items-start">
      <Card>
        <CardHead title="Spread across every run" />
        <div className="flex items-baseline gap-3 mb-1">
          <StatBig>{s.range_ratio.toFixed(2)}x</StatBig>
          <span className="text-[13px] text-muted">
            highest over lowest, {s.n} runs
          </span>
        </div>

        <div className="mt-6 mb-3">
          <div className="relative h-[10px] rounded-full bg-line">
            <div
              className="absolute h-full rounded-full bg-brand-soft"
              style={{ left: "0%", right: "0%" }}
            />
            <div
              className="absolute top-1/2 -translate-y-1/2 w-[3px] h-[20px] rounded-full bg-brand"
              style={{
                left: `${
                  s.high > s.low
                    ? ((s.median - s.low) / (s.high - s.low)) * 100
                    : 50
                }%`,
              }}
              title={`Median ${money(s.median, cur)}`}
            />
          </div>
          <div className="flex justify-between mt-2 text-[12px] tabular-nums">
            <span className="text-muted">{money(s.low, cur)} low</span>
            <span className="font-semibold">{money(s.median, cur)} median</span>
            <span className="text-muted">{money(s.high, cur)} high</span>
          </div>
        </div>

        <p className="text-[13px] text-ink-2 leading-relaxed mt-4">
          Every one of these answers is defensible under a published method. We do
          not average them into a single number, because the spread is the finding.
          A range ratio above 2 means the choice of model moves the answer more
          than the hazard does.
        </p>
      </Card>

      <Card>
        <CardHead title="What drives the spread" />
        <ul className="flex flex-col gap-4">
          {Object.entries(s.by_driver)
            .sort((a, b) => b[1].share - a[1].share)
            .map(([k, v]) => (
              <li key={k}>
                <div className="flex items-baseline justify-between gap-3 mb-1.5">
                  <span className="text-[14px] font-medium capitalize">
                    {k.replace(/_/g, " ")}
                  </span>
                  <span className="text-[12.5px] text-muted tabular-nums">
                    {v.levels} variants · {money(v.range, cur)} range
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="flex-1 h-[8px] rounded-full bg-line overflow-hidden">
                    <span
                      className="block h-full rounded-full bg-brand"
                      style={{ width: `${Math.min(100, v.share * 100)}%` }}
                    />
                  </span>
                  <b className="text-[13px] tabular-nums w-[46px] text-right">
                    {pct(v.share, 0)}
                  </b>
                </div>
              </li>
            ))}
        </ul>
        <p className="text-[12px] text-muted mt-5 leading-relaxed">
          First-order attribution: each driver&apos;s share is the range of its group
          means over the total range. Shares need not sum to 100% because drivers
          interact.
        </p>
      </Card>
    </div>
  );
}

/* ============================================================ adaptation */

export function AdaptationView({
  data,
  scenario,
  assumptions,
}: {
  data: Summary;
  scenario: string;
  assumptions: Partial<Assumptions>;
}) {
  const [sel, setSel] = React.useState(data.assets[0]?.id ?? "");
  const [detail, setDetail] = React.useState<AssetDetail | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const cur = data.portfolio.currency;

  React.useEffect(() => {
    if (!sel) return;
    let live = true;
    setBusy(true);
    setErr(null);
    getAsset(sel, scenario, assumptions)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setErr(e instanceof Error ? e.message : "Failed."))
      .finally(() => live && setBusy(false));
    return () => {
      live = false;
    };
  }, [sel, scenario, assumptions]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHead title="Appraise an intervention">
          <select
            aria-label="Asset"
            value={sel}
            onChange={(e) => setSel(e.target.value)}
            className="rounded-full border border-line-2 bg-card px-4 py-2 text-[13px] max-w-full"
          >
            {data.assets.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </CardHead>

        {err && <ErrorNote message={err} />}
        {busy && <Skeleton className="h-[200px]" />}

        {!busy && detail && (
          <>
            <p className="text-[13.5px] text-ink-2 mb-5">
              Baseline expected annual loss{" "}
              <b>{money(detail.finance.annual_physical_damage, cur)}</b>, NPV of
              climate cost <b>{money(detail.finance.npv_climate_cost, cur)}</b> over{" "}
              {detail.provenance.horizon_years} years.
            </p>
            <div className="overflow-x-auto ac-scroll">
              <table className="w-full min-w-[720px] border-collapse">
                <thead>
                  <tr className="text-left">
                    {[
                      "Option",
                      "Capex",
                      "Loss cut",
                      "Benefit NPV",
                      "Net NPV",
                      "BCR",
                      "Payback",
                      "Verdict",
                    ].map((th) => (
                      <th
                        key={th}
                        className="text-[11px] uppercase tracking-[0.08em] text-muted font-semibold pb-3 pr-4 whitespace-nowrap"
                      >
                        {th}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {detail.adaptation.map((o) => (
                    <tr key={o.name} className="border-t border-line">
                      <td className="py-3 pr-4 text-[13.5px] font-medium">{o.name}</td>
                      <td className="py-3 pr-4 text-[13.5px] tabular-nums">
                        {money(o.capex, cur)}
                      </td>
                      <td className="py-3 pr-4 text-[13.5px] tabular-nums">
                        {pct(o.loss_reduction, 0)}
                      </td>
                      <td className="py-3 pr-4 text-[13.5px] tabular-nums">
                        {money(o.benefit_npv, cur)}
                      </td>
                      <td
                        className={
                          "py-3 pr-4 text-[13.5px] tabular-nums font-semibold " +
                          (o.net_npv > 0 ? "text-good" : "text-danger")
                        }
                      >
                        {money(o.net_npv, cur)}
                      </td>
                      <td className="py-3 pr-4 text-[13.5px] tabular-nums">
                        {num(o.bcr, 2)}
                      </td>
                      <td className="py-3 pr-4 text-[13.5px] tabular-nums">
                        {o.payback_years ? `${o.payback_years} yr` : "never"}
                      </td>
                      <td className="py-3 pr-1">
                        <Chip tone={o.worth_doing ? "flat" : "danger"}>
                          {o.worth_doing ? "worth doing" : "does not pay"}
                        </Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

/* ================================================================ ledger */

export function LedgerView({ data }: { data: Summary }) {
  const p = data.provenance;
  const Row = ({ k, v }: { k: string; v: React.ReactNode }) => (
    <div className="flex flex-wrap gap-x-4 gap-y-1 py-2.5 border-t border-line text-[13.5px]">
      <span className="w-[190px] shrink-0 text-muted">{k}</span>
      <span className="flex-1 min-w-[200px] break-words">{v}</span>
    </div>
  );

  return (
    <div className="grid gap-4 lg:grid-cols-2 items-start">
      <Card>
        <CardHead title="This run" />
        <Row k="Run id" v={<code className="text-[12.5px]">{p.run_id}</code>} />
        <Row k="Computed at" v={new Date(p.computed_at).toUTCString()} />
        <Row k="Engine version" v={p.engine_version} />
        <Row k="Scenario" v={p.scenario} />
        <Row k="Horizon" v={`${p.horizon_years} years`} />
        <Row
          k="Status"
          v={
            p.degraded ? (
              <Chip tone="warn">degraded: {p.degraded_reason}</Chip>
            ) : (
              <Chip tone="flat">complete</Chip>
            )
          }
        />
        <p className="text-[12px] text-muted mt-4 leading-relaxed">
          Every figure on the dashboard traces to this record. Re-running with the
          same inputs reproduces the same numbers.
        </p>
      </Card>

      <Card>
        <CardHead title="Data lineage" />
        <h3 className="text-[12px] uppercase tracking-[0.08em] text-muted font-semibold mb-1 mt-1">
          Hazard
        </h3>
        {p.hazard_sources.length ? (
          p.hazard_sources.map((s) => (
            <Row
              key={s.path}
              k={s.dataset}
              v={
                <>
                  <code className="text-[12px] break-all">{s.path}</code>
                  <br />
                  <span className="text-muted">
                    {s.units} · {s.resolution} · {s.citation}
                  </span>
                </>
              }
            />
          ))
        ) : (
          <Row k="—" v="No hazard source recorded for this run." />
        )}

        <h3 className="text-[12px] uppercase tracking-[0.08em] text-muted font-semibold mb-1 mt-5">
          Vulnerability
        </h3>
        {p.curve_sources.length ? (
          p.curve_sources.map((c, i) => <Row key={i} k={`Curve ${i + 1}`} v={c} />)
        ) : (
          <Row k="—" v="No damage curve recorded." />
        )}
      </Card>
    </div>
  );
}

/* =========================================================== assumptions */

const FIELDS: {
  key: keyof Assumptions;
  label: string;
  help: string;
  min: number;
  max: number;
  step: number;
  as?: "pct";
}[] = [
  { key: "discount_rate", label: "Discount rate", help: "WACC used to present-value future losses", min: 0, max: 0.25, step: 0.005, as: "pct" },
  { key: "horizon_years", label: "Hold period", help: "Years of losses included in the NPV", min: 1, max: 40, step: 1 },
  { key: "hazard_growth", label: "Hazard growth", help: "Annual worsening of expected loss inside the horizon", min: 0, max: 0.1, step: 0.005, as: "pct" },
  { key: "ebitda_margin", label: "EBITDA margin", help: "Used to size lost earnings during outage", min: 0, max: 1, step: 0.01, as: "pct" },
  { key: "downtime_days_per_damage_unit", label: "Downtime at total loss", help: "Days of outage at 100% damage, scaled linearly", min: 0, max: 365, step: 5 },
  { key: "bi_recovery_fraction", label: "Revenue truly lost", help: "Share of interrupted revenue not simply deferred", min: 0, max: 1, step: 0.05, as: "pct" },
  { key: "deductible_fraction", label: "Deductible", help: "Share of asset value retained per event", min: 0, max: 0.5, step: 0.005, as: "pct" },
  { key: "limit_fraction", label: "Cover limit", help: "Cap on insurance recovery, share of asset value", min: 0, max: 1, step: 0.05, as: "pct" },
  { key: "coinsurance", label: "Coinsurance", help: "Share of covered loss still retained", min: 0, max: 0.6, step: 0.01, as: "pct" },
  { key: "premium_rate_on_eal", label: "Premium multiple", help: "Premium as a multiple of expected loss", min: 0.5, max: 4, step: 0.05 },
  { key: "premium_escalation", label: "Premium escalation", help: "Annual real premium growth", min: 0, max: 0.25, step: 0.005, as: "pct" },
];

const DEFAULTS: Assumptions = {
  discount_rate: 0.08,
  horizon_years: 15,
  downtime_days_per_damage_unit: 180,
  ebitda_margin: 0.35,
  bi_recovery_fraction: 0.6,
  deductible_fraction: 0.02,
  limit_fraction: 0.8,
  coinsurance: 0.1,
  premium_rate_on_eal: 1.35,
  premium_escalation: 0.06,
  insurable: true,
  hazard_growth: 0.025,
};

export function AssumptionsView({
  value,
  onChange,
  onReset,
}: {
  value: Partial<Assumptions>;
  onChange: (v: Partial<Assumptions>) => void;
  onReset: () => void;
}) {
  const [draft, setDraft] = React.useState<Partial<Assumptions>>(value);
  React.useEffect(() => setDraft(value), [value]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(value);

  const get = (k: keyof Assumptions) =>
    (draft[k] ?? DEFAULTS[k]) as number;

  return (
    <Card>
      <CardHead title="Model assumptions">
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onReset}
            className="rounded-full border border-line-2 px-4 py-2 text-[13px] font-medium hover:bg-canvas transition-colors"
          >
            Reset to defaults
          </button>
          <button
            type="button"
            disabled={!dirty}
            onClick={() => onChange(draft)}
            className="rounded-full bg-brand text-white px-4 py-2 text-[13px] font-medium disabled:opacity-40 hover:bg-brand-600 transition-colors"
          >
            Re-run
          </button>
        </div>
      </CardHead>

      <p className="text-[13.5px] text-ink-2 mb-6 max-w-[72ch]">
        Every lever below belongs to you, not to us. Change one and the whole
        portfolio is recomputed, and the override is written into the provenance
        ledger for this run.
      </p>

      <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2 xl:grid-cols-3">
        {FIELDS.map((f) => {
          const v = get(f.key);
          const changed = draft[f.key] !== undefined && draft[f.key] !== DEFAULTS[f.key];
          return (
            <label key={f.key} className="flex flex-col gap-1.5">
              <span className="flex items-baseline justify-between gap-2">
                <span className="text-[13.5px] font-medium">
                  {f.label}
                  {changed && <span className="text-brand"> ·</span>}
                </span>
                <span className="text-[13px] tabular-nums font-semibold">
                  {f.as === "pct" ? pct(v, 1) : num(v, f.step < 1 ? 2 : 0)}
                </span>
              </span>
              <input
                type="range"
                min={f.min}
                max={f.max}
                step={f.step}
                value={v}
                aria-label={f.label}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, [f.key]: Number(e.target.value) }))
                }
                className="w-full accent-[#0b6be1]"
              />
              <span className="text-[11.5px] text-muted leading-snug">{f.help}</span>
            </label>
          );
        })}

        <label className="flex items-start gap-2.5">
          <input
            type="checkbox"
            checked={(draft.insurable ?? DEFAULTS.insurable) as boolean}
            onChange={(e) =>
              setDraft((d) => ({ ...d, insurable: e.target.checked }))
            }
            className="mt-1 accent-[#0b6be1] w-4 h-4"
          />
          <span>
            <span className="block text-[13.5px] font-medium">Assume cover available</span>
            <span className="block text-[11.5px] text-muted leading-snug">
              Switch off to see the portfolio with insurance withdrawn
            </span>
          </span>
        </label>
      </div>
    </Card>
  );
}

/* ================================================================ drawer */

export function AssetDrawer({
  id,
  scenario,
  assumptions,
  onClose,
}: {
  id: string;
  scenario: string;
  assumptions: Partial<Assumptions>;
  onClose: () => void;
}) {
  const [d, setD] = React.useState<AssetDetail | null>(null);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
    let live = true;
    setD(null);
    setErr(null);
    getAsset(id, scenario, assumptions)
      .then((x) => live && setD(x))
      .catch((e) => live && setErr(e instanceof Error ? e.message : "Failed."));
    return () => {
      live = false;
    };
  }, [id, scenario, assumptions]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const cur = "USD";
  const f = d?.finance;

  const Metric = ({
    k,
    v,
    tone,
  }: {
    k: string;
    v: React.ReactNode;
    tone?: "good" | "bad";
  }) => (
    <div className="flex items-baseline justify-between gap-3 py-2.5 border-t border-line">
      <span className="text-[13px] text-muted">{k}</span>
      <span
        className={
          "text-[13.5px] font-semibold tabular-nums text-right " +
          (tone === "bad" ? "text-danger" : tone === "good" ? "text-good" : "")
        }
      >
        {v}
      </span>
    </div>
  );

  return (
    <>
      <div
        className="fixed inset-0 bg-charcoal/35 z-40"
        onClick={onClose}
        aria-hidden
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Asset detail"
        className="fixed right-0 top-0 h-dvh w-full sm:w-[520px] bg-canvas z-50 overflow-y-auto ac-scroll shadow-2xl"
      >
        <div className="sticky top-0 bg-canvas/95 backdrop-blur px-5 sm:px-6 py-4 flex items-start justify-between gap-4 border-b border-line">
          <div className="min-w-0">
            <p className="text-[11px] uppercase tracking-[0.1em] text-muted font-semibold">
              Asset detail
            </p>
            <h2 className="font-display text-[22px] leading-tight truncate">
              {d?.asset.name ?? "Loading…"}
            </h2>
            {d && (
              <p className="text-[12.5px] text-muted mt-0.5">
                {d.asset.country} · {d.asset.sector} · {d.asset.lat.toFixed(4)},{" "}
                {d.asset.lon.toFixed(4)}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid place-items-center w-9 h-9 rounded-full border border-line-2 bg-card shrink-0 hover:bg-canvas transition-colors"
          >
            <X size={17} aria-hidden />
          </button>
        </div>

        <div className="p-4 sm:p-5 flex flex-col gap-4">
          {err && <ErrorNote message={err} />}
          {!d && !err && <Skeleton className="h-[400px] rounded-[20px]" />}

          {d && f && (
            <>
              <Card>
                <CardHead title="Financial impact" />
                <Metric k="Asset value" v={moneyExact(d.asset.value, cur)} />
                <Metric
                  k="Expected annual damage"
                  v={moneyExact(f.annual_physical_damage, cur)}
                />
                <Metric
                  k="Business interruption"
                  v={moneyExact(f.annual_business_interruption, cur)}
                />
                <Metric
                  k="Insurance recovery"
                  v={`-${moneyExact(f.annual_insurance_recovery, cur)}`}
                  tone="good"
                />
                <Metric k="Premium" v={moneyExact(f.annual_premium, cur)} />
                <Metric
                  k="Net annual cost"
                  v={moneyExact(f.annual_net_cost, cur)}
                  tone="bad"
                />
                <Metric
                  k={`NPV over ${d.provenance.horizon_years} yr`}
                  v={moneyExact(f.npv_climate_cost, cur)}
                  tone="bad"
                />
                <Metric
                  k="Value impairment"
                  v={`${moneyExact(f.value_impairment, cur)} (${pct(f.value_impairment_pct, 1)})`}
                  tone="bad"
                />
                <Metric k="Adjusted value" v={moneyExact(f.adjusted_value, cur)} />
              </Card>

              <Card>
                <CardHead title="Credit metrics" />
                <Metric
                  k="LTV"
                  v={`${pct(f.ltv_before, 1)} → ${f.ltv_after === null ? "—" : pct(f.ltv_after, 1)}`}
                  tone={f.ltv_after !== null && f.ltv_after > 0.75 ? "bad" : undefined}
                />
                <Metric
                  k="DSCR"
                  v={`${ratio(f.dscr_before)} → ${ratio(f.dscr_after)}`}
                  tone={f.dscr_after < 1.25 ? "bad" : undefined}
                />
                <Metric
                  k="Covenant"
                  v={f.covenant_breach ? "breach" : "within covenant"}
                  tone={f.covenant_breach ? "bad" : "good"}
                />
                <Metric
                  k="Insurability"
                  v={f.uninsurable_flag ? "at cover limit" : "insurable"}
                  tone={f.uninsurable_flag ? "bad" : "good"}
                />
                <p className="text-[11.5px] text-muted mt-3 leading-relaxed">
                  Tested against common covenant floors: DSCR 1.25x, LTV 75%.
                </p>
              </Card>

              <Card>
                <CardHead title="Hazard and damage" />
                {d.hazards.length === 0 && (
                  <p className="text-[13px] text-muted">
                    No hazard indicator returned a value at this location.
                  </p>
                )}
                {d.hazards.map((hz) => (
                  <div key={hz.peril} className="border-t border-line pt-3.5 mt-3.5 first:border-0 first:mt-0 first:pt-0">
                    <div className="flex items-baseline justify-between gap-3 mb-2 flex-wrap">
                      <h4 className="text-[14px] font-semibold">
                        {perilLabel(hz.peril)}
                      </h4>
                      <span className="flex items-center gap-2">
                        {hz.extrapolated && <ExtrapolationFlag />}
                        <span className="text-[13px] font-semibold tabular-nums">
                          {money(hz.eal, cur)}/yr
                        </span>
                      </span>
                    </div>
                    <div className="overflow-x-auto ac-scroll">
                      <table className="w-full min-w-[300px] text-[12.5px] tabular-nums">
                        <thead>
                          <tr className="text-muted text-left">
                            <th className="font-medium pb-1.5 pr-3">Return period</th>
                            <th className="font-medium pb-1.5 pr-3">
                              Intensity ({hz.units})
                            </th>
                            <th className="font-medium pb-1.5 pr-3">Damage</th>
                            <th className="font-medium pb-1.5">Loss</th>
                          </tr>
                        </thead>
                        <tbody>
                          {hz.return_periods.map((rp, i) => (
                            <tr key={rp} className="border-t border-line">
                              <td className="py-1.5 pr-3">1 in {rp}</td>
                              <td className="py-1.5 pr-3">
                                {num(hz.intensities[i], 2)}
                              </td>
                              <td className="py-1.5 pr-3">
                                {pct(hz.damage_fractions[i], 1)}
                              </td>
                              <td className="py-1.5">{money(hz.losses[i], cur)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="text-[11.5px] text-muted mt-2.5 leading-relaxed">
                      Curve <code>{hz.curve_id}</code> · {hz.curve_source} ·
                      confidence {hz.curve_confidence}
                      <br />
                      Hazard {hz.source.dataset} · {hz.source.resolution} ·{" "}
                      {hz.source.citation}
                    </p>
                  </div>
                ))}
              </Card>

              {d.adaptation.length > 0 && (
                <Card>
                  <CardHead title="Adaptation options" />
                  {d.adaptation.map((o) => (
                    <div
                      key={o.name}
                      className="border-t border-line py-3 flex items-center justify-between gap-3 flex-wrap"
                    >
                      <div className="min-w-0">
                        <p className="text-[13.5px] font-medium">{o.name}</p>
                        <p className="text-[12px] text-muted">
                          {money(o.capex, cur)} capex · cuts loss {pct(o.loss_reduction, 0)}
                          {o.payback_years ? ` · ${o.payback_years} yr payback` : ""}
                        </p>
                      </div>
                      <span className="flex items-center gap-2 shrink-0">
                        <span
                          className={
                            "text-[13.5px] font-semibold tabular-nums " +
                            (o.net_npv > 0 ? "text-good" : "text-danger")
                          }
                        >
                          {money(o.net_npv, cur)}
                        </span>
                        <Chip tone={o.worth_doing ? "flat" : "danger"}>
                          {num(o.bcr, 2)} BCR
                        </Chip>
                      </span>
                    </div>
                  ))}
                </Card>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
}
