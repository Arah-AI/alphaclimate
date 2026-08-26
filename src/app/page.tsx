"use client";

import * as React from "react";
import { RefreshCw, Search, Plus } from "lucide-react";
import { Rail, type ViewId } from "@/components/Rail";
import {
  Card,
  CardHead,
  Chip,
  Grip,
  LegendDots,
  Skeleton,
  StatBig,
  TickBar,
  ErrorNote,
  ExtrapolationFlag,
} from "@/components/ui";
import { FeatureBars, PerilDonut, Spark, StackBar, YearBand } from "@/components/charts";
import {
  AssetsView,
  DisagreementView,
  AdaptationView,
  LedgerView,
  AssumptionsView,
  AssetDrawer,
} from "@/components/views";
import { LeadBlock, NotModelled, ScenarioBar } from "@/components/landing";
import { Analyst } from "@/components/Analyst";
import { MapView } from "@/components/MapView";
import { getSummary } from "@/lib/api";
import { getCurveGaps, isApiFailure, type CurveGaps } from "@/lib/insight";
import type { Assumptions, Summary } from "@/lib/types";
import {
  money,
  pct,
  perilLabel,
  relativeTime,
  BAND_COLOR,
  PERIL_COLORS,
} from "@/lib/format";

const DEFAULT_HORIZON = 15;
const FALLBACK_SCENARIOS = [{ id: "ssp585", label: "SSP5-8.5 · high emissions" }];

export default function Page() {
  const [view, setView] = React.useState<ViewId>("dashboard");
  const [scenario, setScenario] = React.useState("ssp585");
  const [assumptions, setAssumptions] = React.useState<Partial<Assumptions>>({});
  const [openAsset, setOpenAsset] = React.useState<string | null>(null);
  const [focusYear, setFocusYear] = React.useState<number | undefined>();
  const [nonce, setNonce] = React.useState(0);

  /* Hold period: the slider writes `draft` on every pixel, and only the settled
     value reaches the engine, so a drag costs one run rather than thirty. */
  const [draftHorizon, setDraftHorizon] = React.useState(DEFAULT_HORIZON);
  const [horizon, setHorizon] = React.useState(DEFAULT_HORIZON);
  React.useEffect(() => {
    if (draftHorizon === horizon) return;
    const t = setTimeout(() => setHorizon(draftHorizon), 350);
    return () => clearTimeout(t);
  }, [draftHorizon, horizon]);

  const params = React.useMemo<Partial<Assumptions>>(
    () => ({ ...assumptions, horizon_years: horizon }),
    [assumptions, horizon],
  );

  /* One state for the result, keyed by the request that produced it. `busy` is
     derived from the key rather than set at the top of the effect, which keeps
     the fetch out of the render path and the cascading-render lint honest. */
  const key = React.useMemo(
    () => JSON.stringify([scenario, params, nonce]),
    [scenario, params, nonce],
  );
  const [res, setRes] = React.useState<{
    key: string;
    data?: Summary;
    error?: string;
  }>({ key: "" });

  React.useEffect(() => {
    let live = true;
    getSummary("demo", scenario, params)
      .then((d) => live && setRes({ key, data: d }))
      .catch(
        (e) =>
          live &&
          setRes({
            key,
            error: e instanceof Error ? e.message : "Unknown failure.",
          }),
      );
    return () => {
      live = false;
    };
  }, [key, scenario, params]);

  const busy = res.key !== key;
  const data = res.data ?? null;
  const error = res.error ?? null;

  /* The refusal ledger does not depend on the scenario, so it is fetched once. */
  const [gaps, setGaps] = React.useState<CurveGaps | null>(null);
  const [gapsError, setGapsError] = React.useState<string | null>(null);
  React.useEffect(() => {
    let live = true;
    getCurveGaps()
      .then((g) => live && setGaps(g))
      .catch(
        (e) => live && setGapsError(isApiFailure(e) ? e.detail : "Request failed."),
      );
    return () => {
      live = false;
    };
  }, []);

  const h = data?.headline;
  const currency = data?.portfolio.currency ?? "USD";

  return (
    <div className="min-h-dvh p-3 sm:p-4 flex gap-3 sm:gap-4">
      <Rail view={view} onView={setView} assetCount={h?.asset_count ?? 0} />

      <main className="flex-1 min-w-0 flex flex-col gap-4 pb-6">
        {/* ------------------------------------------------ top bar */}
        <header className="flex flex-wrap items-start justify-between gap-4 pt-2 px-1">
          <div className="min-w-0">
            <h1 className="font-display text-display-sm sm:text-display leading-[1.05] font-medium tracking-[-0.02em]">
              {VIEW_TITLE[view]}
            </h1>
            <p className="text-ui text-muted mt-1">
              {data
                ? `${data.portfolio.name} · ${h?.asset_count} assets · ${money(
                    h?.total_value ?? 0,
                    currency,
                  )} insured value`
                : "Loading portfolio…"}
            </p>
          </div>

          <div className="flex items-center gap-2.5 shrink-0">
            <button
              type="button"
              aria-label="Search assets"
              onClick={() => setView("assets")}
              className="grid place-items-center w-[42px] h-[42px] rounded-full bg-card border border-line-2 text-ink-2 hover:text-ink transition-colors"
            >
              <Search size={18} aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => setNonce((n) => n + 1)}
              disabled={busy}
              className="flex items-center gap-2.5 rounded-full bg-card border border-line-2 pl-3.5 pr-4 h-[42px] hover:border-brand-soft transition-colors disabled:opacity-60"
            >
              <RefreshCw
                size={16}
                aria-hidden
                className={busy ? "animate-spin text-brand" : "text-ink-2"}
              />
              <span className="text-left leading-tight">
                <span className="block text-micro text-muted">Last run</span>
                <span className="block text-support font-semibold text-brand">
                  {busy
                    ? "running…"
                    : data
                      ? relativeTime(data.provenance.computed_at)
                      : "—"}
                </span>
              </span>
            </button>
            <div
              className="grid place-items-center w-[42px] h-[42px] rounded-full bg-charcoal text-white text-ui font-semibold shrink-0"
              title="Signed in"
            >
              AC
            </div>
          </div>
        </header>

        {/* ------------------------------------------------ run control */}
        <ScenarioBar
          scenarios={data?.scenarios ?? FALLBACK_SCENARIOS}
          scenario={scenario}
          onScenario={setScenario}
          horizon={draftHorizon}
          onHorizon={setDraftHorizon}
          busy={busy || draftHorizon !== horizon}
        />

        {/* ------------------------------------------------ degraded */}
        {data?.provenance.degraded && (
          <div
            role="status"
            className="rounded-[16px] bg-warn-tint border border-warn/25 px-4 py-3 text-ui text-ink-2"
          >
            <b className="text-warn font-semibold">Degraded run. </b>
            {data.provenance.degraded_reason}
          </div>
        )}

        {error && <ErrorNote message={error} onRetry={() => setNonce((n) => n + 1)} />}

        {/* ------------------------------------------------ body */}
        {!error && view === "dashboard" && (
          <>
            <LeadBlock data={data} busy={busy && !data} gaps={gaps} />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_336px] items-start">
              {/* ---- left region ---- */}
              <div className="grid gap-4 sm:grid-cols-2 min-w-0">
                {/* Who absorbs it */}
                <Card>
                  <CardHead title="Who absorbs the loss" />
                  {busy || !h ? (
                    <Skeleton className="h-[104px]" />
                  ) : (
                    <>
                      <div className="flex flex-wrap items-baseline gap-2.5 mb-1">
                        <StatBig>{money(h.protection_gap, currency)}</StatBig>
                        <Chip tone="danger">retained by you</Chip>
                        <button
                          type="button"
                          aria-label="See adaptation options"
                          onClick={() => setView("adaptation")}
                          className="ml-auto grid place-items-center w-11 h-11 rounded-full bg-charcoal text-white hover:bg-charcoal-2 transition-colors shrink-0"
                        >
                          <Plus size={20} aria-hidden />
                        </button>
                      </div>
                      <p className="text-support text-muted mb-4">
                        {money(h.eal - h.protection_gap, currency)} of the annual loss
                        is recovered from cover, the rest lands on the balance sheet
                      </p>
                      <TickBar
                        label={`${pct(h.insured_share)} of expected loss is insured, the rest is retained`}
                        segments={[
                          { value: h.insured_share, color: "#3fd9c6" },
                          { value: h.retained_share, color: "#0b6be1" },
                        ]}
                      />
                      <div className="mt-3.5">
                        <LegendDots
                          items={[
                            {
                              label: "Insured",
                              color: "#3fd9c6",
                              value: pct(h.insured_share, 0),
                            },
                            {
                              label: "Retained",
                              color: "#0b6be1",
                              value: pct(h.retained_share, 0),
                            },
                          ]}
                        />
                      </div>
                    </>
                  )}
                </Card>

                {/* Assets */}
                <Card>
                  <CardHead title="Assets modelled" />
                  {busy || !h ? (
                    <Skeleton className="h-[104px]" />
                  ) : (
                    <>
                      <div className="flex flex-wrap items-baseline gap-2.5 mb-5">
                        <StatBig>{h.asset_count}</StatBig>
                        {h.covenant_breaches > 0 ? (
                          <Chip tone="danger">
                            {h.covenant_breaches} covenant breach
                            {h.covenant_breaches > 1 ? "es" : ""}
                          </Chip>
                        ) : (
                          <Chip tone="flat">no covenant breach</Chip>
                        )}
                        {h.uninsurable_count > 0 && (
                          <Chip tone="warn">{h.uninsurable_count} at cover limit</Chip>
                        )}
                      </div>
                      <TickBar
                        label="Assets by risk band"
                        segments={(["severe", "high", "moderate", "low"] as const).map(
                          (b) => ({ value: h.bands[b] ?? 0, color: BAND_COLOR[b] }),
                        )}
                      />
                      <div className="mt-3.5">
                        <LegendDots
                          items={(["severe", "high", "moderate", "low"] as const)
                            .filter((b) => (h.bands[b] ?? 0) > 0)
                            .map((b) => ({
                              label: b[0].toUpperCase() + b.slice(1),
                              color: BAND_COLOR[b],
                              value: String(h.bands[b]),
                            }))}
                        />
                      </div>
                    </>
                  )}
                </Card>

                {/* Top assets */}
                <Card>
                  <CardHead title="Top assets at risk" />
                  {busy || !data ? (
                    <Skeleton className="h-[170px]" />
                  ) : (
                    <div className="flex gap-4">
                      <StackBar
                        segments={[
                          ...data.assets.slice(0, 3).map((a, i) => ({
                            value: a.eal,
                            color: PERIL_COLORS[i],
                          })),
                          {
                            value: data.assets.slice(3).reduce((s, a) => s + a.eal, 0),
                            color: "#e8eaee",
                            hatch: true,
                          },
                        ]}
                      />
                      <ul className="flex-1 min-w-0 flex flex-col gap-2.5">
                        {data.assets.slice(0, 3).map((a, i) => (
                          <li key={a.id}>
                            <button
                              type="button"
                              onClick={() => setOpenAsset(a.id)}
                              className="w-full flex items-center gap-2.5 text-left rounded-lg -mx-1 px-1 py-1 hover:bg-canvas transition-colors"
                            >
                              <i
                                aria-hidden
                                className="w-[9px] h-[9px] rounded-full shrink-0"
                                style={{ background: PERIL_COLORS[i] }}
                              />
                              <span className="text-ui font-medium truncate">
                                {a.name}
                              </span>
                              <span className="text-support text-muted truncate hidden sm:inline">
                                {a.country}
                              </span>
                              <span className="ml-auto text-support text-muted tabular-nums shrink-0">
                                {pct(a.eal_pct, 2)}
                              </span>
                              <span className="text-ui font-semibold tabular-nums shrink-0 w-[62px] text-right">
                                {money(a.eal, currency)}
                              </span>
                            </button>
                          </li>
                        ))}
                        <li className="flex items-center gap-2.5 pt-0.5">
                          <i
                            aria-hidden
                            className="w-[9px] h-[9px] rounded-full border border-line-2 shrink-0"
                          />
                          <span className="text-ui text-muted">
                            Other {Math.max(0, data.assets.length - 3)} assets
                          </span>
                          <span className="ml-auto text-ui font-semibold tabular-nums">
                            {money(
                              data.assets.slice(3).reduce((s, a) => s + a.eal, 0),
                              currency,
                            )}
                          </span>
                        </li>
                        <li className="pt-1">
                          <button
                            type="button"
                            onClick={() => setView("assets")}
                            className="text-support font-medium text-brand hover:underline"
                          >
                            See all assets →
                          </button>
                        </li>
                      </ul>
                    </div>
                  )}
                </Card>

                {/* Peril donut */}
                <Card>
                  <CardHead title="Loss by peril" />
                  {busy || !data ? (
                    <Skeleton className="h-[190px]" />
                  ) : (
                    <>
                      <div className="flex flex-wrap items-center gap-5">
                        <PerilDonut data={data.perils} />
                        <ul className="flex-1 min-w-[130px] flex flex-col gap-2.5">
                          {data.perils.slice(0, 5).map((p, i) => (
                            <li key={p.peril} className="flex items-center gap-2.5">
                              <i
                                aria-hidden
                                className="w-[9px] h-[9px] rounded-full shrink-0"
                                style={{
                                  background: PERIL_COLORS[i % PERIL_COLORS.length],
                                }}
                              />
                              <span className="text-ui truncate">
                                {perilLabel(p.peril)}
                              </span>
                              <span className="ml-auto shrink-0">
                                <Chip tone="flat">{pct(p.share, 0)}</Chip>
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                      <p className="text-support text-muted mt-4 ml-auto">
                        Share of modelled annual loss
                      </p>
                    </>
                  )}
                </Card>

                {/* Year band */}
                <Card className="sm:col-span-2">
                  <CardHead title="Expected loss by year" stack>
                    <Grip />
                  </CardHead>
                  <div className="-mt-2 mb-2">
                    <LegendDots
                      items={[
                        { label: "Median estimate", color: "#0b6be1" },
                        { label: "Up to highest defensible", color: "#aecdf5" },
                      ]}
                    />
                  </div>
                  {busy || !data ? (
                    <Skeleton className="h-[230px]" />
                  ) : (
                    <YearBand
                      data={data.yearly}
                      focusYear={focusYear}
                      onFocus={(y) => setFocusYear(y === focusYear ? undefined : y)}
                    />
                  )}
                </Card>
              </div>

              {/* ---- right column ---- */}
              <div className="flex flex-col gap-4 min-w-0">
                <Card tone="brand" className="min-h-[330px]">
                  <CardHead title="Climate cost, present value" tone="dark">
                    <Grip tone="dark" />
                  </CardHead>
                  {busy || !h ? (
                    <Skeleton className="h-[220px] bg-white/20" />
                  ) : (
                    <>
                      <StatBig className="text-white">
                        {money(h.npv_climate_cost, currency)}
                      </StatBig>
                      <p className="text-ui text-white/75 mt-2 flex items-center gap-2 flex-wrap">
                        <span>{pct(h.value_impairment_pct, 1)} of portfolio value</span>
                        <Chip tone="ghost" arrow="up">
                          NPV over {data?.provenance.horizon_years}yr
                        </Chip>
                      </p>
                      <FeatureBars data={data?.perils ?? []} />
                    </>
                  )}
                </Card>

                <Card tone="dark">
                  <CardHead title="Value impairment" tone="dark">
                    <Chip tone="dark">{horizon} yr hold</Chip>
                  </CardHead>
                  {busy || !h ? (
                    <Skeleton className="h-[140px] bg-white/15" />
                  ) : (
                    <>
                      <StatBig className="text-white">
                        {money(h.value_impairment, currency)}
                      </StatBig>
                      <p className="text-ui text-white/70 mt-2 flex items-center gap-2 flex-wrap">
                        <span>{pct(h.value_impairment_pct, 1)} of value</span>
                        <Chip tone="dark" arrow="down">
                          {money(h.total_value - h.value_impairment, currency)} adjusted
                        </Chip>
                      </p>
                      <Spark data={data?.impairment_path ?? []} />
                    </>
                  )}
                </Card>

                <NotModelled gaps={gaps} error={gapsError} />
              </div>

              {/* ---- disagreement band ---- */}
              <Card className="lg:col-span-2">
                <CardHead title="Where the models disagree" />
                {busy || !h ? (
                  <Skeleton className="h-[90px]" />
                ) : (
                  <div className="flex flex-wrap gap-6 items-start">
                    <div className="min-w-[210px]">
                      <div className="flex items-baseline gap-2">
                        <StatBig>{h.eal_spread.range_ratio.toFixed(2)}x</StatBig>
                        {h.eal_spread.range_ratio > 2 && (
                          <ExtrapolationFlag title="A spread this wide means the choice of model changes the answer more than the hazard does" />
                        )}
                      </div>
                      <p className="text-ui text-muted mt-1.5">
                        between the lowest and highest defensible answer
                        <br />
                        {money(h.eal_spread.low, currency)} to{" "}
                        {money(h.eal_spread.high, currency)} a year, across{" "}
                        {h.eal_spread.n} runs
                      </p>
                    </div>
                    <ul className="flex-1 min-w-[240px] flex flex-col gap-3">
                      {Object.entries(h.eal_spread.by_driver)
                        .sort((a, b) => b[1].share - a[1].share)
                        .map(([k, v]) => (
                          <li key={k} className="flex items-center gap-3">
                            <span className="text-ui w-[104px] shrink-0 capitalize">
                              {k.replace(/_/g, " ")}
                            </span>
                            <span className="flex-1 h-[7px] rounded-full bg-line overflow-hidden">
                              <span
                                className="block h-full rounded-full bg-brand"
                                style={{ width: `${Math.min(100, v.share * 100)}%` }}
                              />
                            </span>
                            <span className="text-support tabular-nums text-muted w-[92px] text-right shrink-0">
                              {pct(v.share, 0)} of spread
                            </span>
                          </li>
                        ))}
                    </ul>
                    <button
                      type="button"
                      onClick={() => setView("disagreement")}
                      className="text-support font-medium text-brand hover:underline shrink-0"
                    >
                      Full breakdown →
                    </button>
                  </div>
                )}
              </Card>
            </div>

            {/* ---- what the engine will not claim ---- */}
            <Analyst scenario={scenario} />
          </>
        )}

        {!error && view === "assets" && data && (
          <AssetsView data={data} onOpen={setOpenAsset} />
        )}
        {!error && view === "map" && data && (
          <MapView data={data} onOpenAsset={setOpenAsset} />
        )}
        {!error && view === "disagreement" && data && <DisagreementView data={data} />}
        {!error && view === "adaptation" && data && (
          <AdaptationView data={data} scenario={scenario} assumptions={params} />
        )}
        {!error && view === "ledger" && data && <LedgerView data={data} />}
        {!error && view === "assumptions" && (
          <AssumptionsView
            value={assumptions}
            onChange={setAssumptions}
            onReset={() => setAssumptions({})}
          />
        )}

        {!error && !data && busy && view !== "dashboard" && (
          <Skeleton className="h-[320px] rounded-[20px]" />
        )}

        <footer className="text-micro text-muted px-1 pt-1 leading-relaxed">
          {data ? (
            <>
              Run {data.provenance.run_id} · engine {data.provenance.engine_version} ·{" "}
              {data.provenance.scenario} ·{" "}
              {data.provenance.hazard_sources.map((s) => s.dataset).join(", ") ||
                "no hazard source"}{" "}
              · curves: {data.provenance.curve_sources.join("; ") || "none"}
            </>
          ) : (
            "—"
          )}
        </footer>
      </main>

      {openAsset && (
        <AssetDrawer
          id={openAsset}
          scenario={scenario}
          assumptions={params}
          onClose={() => setOpenAsset(null)}
        />
      )}
    </div>
  );
}

const VIEW_TITLE: Record<ViewId, string> = {
  dashboard: "Portfolio climate risk",
  assets: "Assets",
  map: "Hazard map",
  disagreement: "Model disagreement",
  adaptation: "Adaptation options",
  ledger: "Provenance ledger",
  assumptions: "Assumptions",
};
