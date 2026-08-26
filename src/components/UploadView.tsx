"use client";

/**
 * Upload a portfolio.
 *
 * The point of this screen is the validation report, not the drop zone. A
 * customer's location file is always messier than they think, and the failure
 * we care about is the silent one: rows quietly dropped, so the totals stay
 * internally consistent while describing a smaller company than the one that
 * uploaded the file. So every input row is listed here with its fate, rejected
 * rows carry a reason, and the counts are shown before the numbers are.
 *
 * The second thing it has to communicate is waiting. Uploaded sites are not in
 * the warmed hazard cache, so their hazard comes live from the public zarr
 * store: tens of seconds for the first asset. That is a job, so this polls and
 * shows progress rather than pretending a request is still in flight.
 */

import * as React from "react";
import { UploadCloud, FileWarning, CheckCircle2, XCircle, AlertTriangle } from "lucide-react";
import { Card, CardHead, Chip, ErrorNote, Skeleton } from "./ui";
import { money } from "@/lib/format";

/* ------------------------------------------------------------------ types */

export type RowStatus = "accepted" | "warning" | "rejected";

export interface RowReport {
  row: number;
  status: RowStatus;
  id: string;
  name: string;
  reason: string;
  warnings: string[];
}

export interface UploadedAsset {
  id: string;
  name: string;
  country: string;
  lon: number;
  lat: number;
  sector: string;
  occupancy: string;
  region: string;
  value: number;
  annual_revenue: number;
  debt: number;
  annual_debt_service: number;
  has_financials: boolean;
  warnings?: string[];
}

export interface UploadedPortfolio {
  id: string;
  name: string;
  currency: string;
  source_format: "oed" | "generic";
  source_spec: string;
  filename: string;
  uploaded: string;
  note: string;
  cached: boolean;
  assets: UploadedAsset[];
  report: {
    counts: { total: number; accepted: number; warning: number; rejected: number };
    rows: RowReport[];
  };
  hazard: {
    status: "pending" | "not_requested" | "warming" | "ready" | "failed";
    done: number;
    total: number;
    misses: string[];
    open_water: string[];
    error: string | null;
    aggregation: string;
    elapsed_s?: number;
  };
}

/* ------------------------------------------------------------------- api */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function postUpload(file: File): Promise<UploadedPortfolio> {
  const body = new FormData();
  body.append("file", file);
  // No Content-Type header: the browser has to set the multipart boundary.
  const res = await fetch(`${BASE}/api/portfolios/upload`, { method: "POST", body });
  const text = await res.text();
  if (!res.ok) {
    let detail = `Upload failed (${res.status}).`;
    try {
      detail = String(JSON.parse(text).detail ?? detail);
    } catch {
      /* a non-JSON error body is still worth the status code */
    }
    throw new Error(detail);
  }
  return JSON.parse(text) as UploadedPortfolio;
}

async function getStatus(id: string): Promise<UploadedPortfolio> {
  const res = await fetch(`${BASE}/api/portfolios/${id}/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Could not read job status (${res.status}).`);
  return (await res.json()) as UploadedPortfolio;
}

/* ------------------------------------------------------------------ bits */

const STATUS_TONE: Record<RowStatus, { tone: "flat" | "warn" | "danger"; label: string }> = {
  accepted: { tone: "flat", label: "Accepted" },
  warning: { tone: "warn", label: "Warning" },
  rejected: { tone: "danger", label: "Rejected" },
};

function StatusChip({ status }: { status: RowStatus }) {
  const s = STATUS_TONE[status];
  return <Chip tone={s.tone}>{s.label}</Chip>;
}

function Count({
  n,
  label,
  color,
  icon,
}: {
  n: number;
  label: string;
  color: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2.5 min-w-0">
      <span aria-hidden style={{ color }} className="shrink-0">
        {icon}
      </span>
      <span className="min-w-0">
        <b className="text-[19px] font-display font-medium tabular-nums">{n}</b>{" "}
        <span className="text-support text-muted">{label}</span>
      </span>
    </div>
  );
}

/** Live progress for the cold hazard reads. */
function WarmProgress({ h }: { h: UploadedPortfolio["hazard"] }) {
  if (h.status === "not_requested") return null;

  if (h.status === "failed") {
    return (
      <div className="rounded-[14px] border border-danger/25 bg-danger-tint px-4 py-3">
        <p className="text-ui font-medium text-ink">Hazard warming failed</p>
        <p className="text-support text-ink-2 mt-0.5">
          {h.error ?? "The public hazard store did not answer."} No hazard values
          were invented in its place.
        </p>
      </div>
    );
  }

  const pctDone = h.total > 0 ? Math.round((h.done / h.total) * 100) : 100;
  const done = h.status === "ready";

  return (
    <div className="rounded-[14px] border border-line bg-canvas px-4 py-3">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-ui font-medium">
          {done ? "Hazard read from the live store" : "Reading live hazard data"}
        </span>
        <span className="text-support text-muted tabular-nums">
          {h.done} / {h.total} assets
          {h.elapsed_s !== undefined && ` · ${h.elapsed_s.toFixed(0)}s`}
        </span>
      </div>
      <span className="block h-[8px] rounded-full bg-line overflow-hidden">
        <span
          className="block h-full rounded-full transition-[width] duration-500"
          style={{
            width: `${pctDone}%`,
            background: done ? "var(--color-good)" : "var(--color-brand)",
          }}
        />
      </span>
      <p className="text-support text-muted mt-2 leading-snug">
        {done
          ? h.aggregation
          : "Uploaded sites are not in the warmed cache, so each one is fetched " +
            "from the public OS-Climate store. Roughly 30 seconds for the first " +
            "asset, then seconds each. Re-uploading the same file is instant."}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ view */

export function UploadView({
  onReady,
}: {
  /** Fired once the hazard warm finishes, so the shell can switch portfolios. */
  onReady?: (p: UploadedPortfolio) => void;
}) {
  const [over, setOver] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const [pf, setPf] = React.useState<UploadedPortfolio | null>(null);
  const [only, setOnly] = React.useState<RowStatus | "all">("all");
  const input = React.useRef<HTMLInputElement>(null);
  const fired = React.useRef<string | null>(null);

  const send = React.useCallback(async (file: File) => {
    setBusy(true);
    setErr(null);
    setPf(null);
    setOnly("all");
    fired.current = null;
    try {
      setPf(await postUpload(file));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }, []);

  // Poll while the background job runs. Stops itself on ready/failed.
  const id = pf?.id;
  const warming = pf?.hazard.status === "pending" || pf?.hazard.status === "warming";
  React.useEffect(() => {
    if (!id || !warming) return;
    let live = true;
    const t = setInterval(() => {
      getStatus(id)
        .then((p) => live && setPf(p))
        .catch(() => {
          /* a dropped poll is not a failed job; the next tick retries */
        });
    }, 2000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [id, warming]);

  React.useEffect(() => {
    if (pf && pf.hazard.status === "ready" && fired.current !== pf.id) {
      fired.current = pf.id;
      onReady?.(pf);
    }
  }, [pf, onReady]);

  const counts = pf?.report.counts;
  const rows = (pf?.report.rows ?? []).filter((r) => only === "all" || r.status === only);
  const kept = pf?.assets ?? [];
  const noFinancials = kept.filter((a) => !a.has_financials).length;

  return (
    <div className="flex flex-col gap-4">
      {/* ------------------------------------------------------- drop zone */}
      <Card>
        <CardHead title="Upload a portfolio" />

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            const f = e.dataTransfer.files?.[0];
            if (f) void send(f);
          }}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              input.current?.click();
            }
          }}
          role="button"
          tabIndex={0}
          aria-label="Choose or drop a portfolio file"
          className={[
            "rounded-[16px] border-2 border-dashed px-6 py-10 text-center cursor-pointer",
            "transition-colors flex flex-col items-center gap-2",
            over ? "border-brand bg-brand-tint" : "border-line-2 hover:border-brand-soft",
          ].join(" ")}
        >
          <UploadCloud
            size={28}
            aria-hidden
            className={over ? "text-brand" : "text-muted"}
          />
          <p className="text-prose font-medium">
            Drop an OED location file or a CSV, or click to choose
          </p>
          <p className="text-support text-muted max-w-[46ch] leading-snug">
            Oasis OED (LocNumber, CountryCode, Latitude, Longitude, OccupancyCode,
            BuildingTIV, ContentsTIV, BITIV) or a plain CSV with name, lat, lon,
            value, sector, country.
          </p>
          <input
            ref={input}
            type="file"
            accept=".csv,text/csv,text/plain"
            className="sr-only"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void send(f);
              e.target.value = "";
            }}
          />
        </div>

        <p className="text-support text-muted mt-3 leading-snug ac-prose">
          Financials are read from the file and nowhere else. Rows that carry no
          revenue, debt or debt service are kept at zero and flagged, never
          estimated.
        </p>
      </Card>

      {err && <ErrorNote message={err} onRetry={() => input.current?.click()} />}
      {busy && <Skeleton className="h-[220px]" />}

      {/* ---------------------------------------------------------- report */}
      {pf && counts && (
        <>
          <Card>
            <CardHead title={pf.name}>
              <span className="flex items-center gap-2">
                <Chip tone="flat">
                  {pf.source_format === "oed" ? "Oasis OED" : "Generic CSV"}
                </Chip>
                {pf.cached && <Chip tone="flat">already ingested</Chip>}
              </span>
            </CardHead>

            <div className="flex flex-wrap gap-x-8 gap-y-3 mb-5">
              <Count
                n={counts.accepted}
                label="accepted"
                color="var(--color-good)"
                icon={<CheckCircle2 size={18} />}
              />
              <Count
                n={counts.warning}
                label="accepted with warnings"
                color="var(--color-warn)"
                icon={<AlertTriangle size={18} />}
              />
              <Count
                n={counts.rejected}
                label="rejected"
                color="var(--color-danger)"
                icon={<XCircle size={18} />}
              />
              <Count
                n={noFinancials}
                label="with no financial data"
                color="var(--color-muted)"
                icon={<FileWarning size={18} />}
              />
            </div>

            <WarmProgress h={pf.hazard} />

            <p className="text-support text-muted mt-3 leading-snug ac-prose">
              {kept.length} of {counts.total} rows became assets, worth{" "}
              {money(
                kept.reduce((s, a) => s + a.value, 0),
                pf.currency,
              )}
              . Nothing was dropped silently: every rejected row is listed below
              with its reason.
              {pf.hazard.open_water.length > 0 &&
                ` ${pf.hazard.open_water.length} site${
                  pf.hazard.open_water.length === 1 ? "" : "s"
                } sit on no land pixel in the hazard grids.`}
            </p>
          </Card>

          <Card>
            <CardHead title={`${rows.length} rows`}>
              <span className="flex gap-1.5 flex-wrap">
                {(["all", "rejected", "warning", "accepted"] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setOnly(k)}
                    aria-pressed={only === k}
                    className={[
                      "rounded-full px-3 py-[5px] text-support font-medium capitalize",
                      "border transition-colors",
                      only === k
                        ? "bg-ink text-white border-ink"
                        : "bg-card text-ink-2 border-line-2 hover:border-brand-soft",
                    ].join(" ")}
                  >
                    {k}
                  </button>
                ))}
              </span>
            </CardHead>

            <div className="overflow-x-auto ac-scroll -mx-1 px-1">
              <table className="w-full min-w-[880px] border-collapse">
                <thead>
                  <tr className="text-left">
                    {["Row", "Status", "Id", "Name", "What we did"].map((th) => (
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
                  {rows.map((r) => (
                    <tr key={r.row} className="border-t border-line align-top">
                      <td className="py-3 pr-4 text-ui tabular-nums text-muted">
                        {r.row}
                      </td>
                      <td className="py-3 pr-4">
                        <StatusChip status={r.status} />
                      </td>
                      <td className="py-3 pr-4 text-ui font-mono text-[12.5px] whitespace-nowrap">
                        {r.id || "—"}
                      </td>
                      <td className="py-3 pr-4 text-ui font-medium">
                        {r.name || <span className="text-muted">unnamed</span>}
                      </td>
                      <td className="py-3 pr-1 text-support leading-snug">
                        {r.status === "rejected" ? (
                          <span className="text-danger">{r.reason}</span>
                        ) : r.warnings.length ? (
                          <ul className="flex flex-col gap-1">
                            {r.warnings.map((w, i) => (
                              <li key={i} className="text-ink-2">
                                {w}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <span className="text-muted">
                            mapped cleanly, no warnings
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!rows.length && (
                    <tr>
                      <td colSpan={5} className="py-10 text-center text-ui text-muted">
                        No {only === "all" ? "" : only} rows.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>

          {/* -------------------------------------------------- mapped assets */}
          {kept.length > 0 && (
            <Card>
              <CardHead title="How each asset was mapped" />
              <div className="overflow-x-auto ac-scroll -mx-1 px-1">
                <table className="w-full min-w-[820px] border-collapse">
                  <thead>
                    <tr className="text-left">
                      {[
                        "Asset",
                        "Coordinates",
                        "Region",
                        "Occupancy",
                        "Value",
                        "Financials",
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
                    {kept.map((a) => (
                      <tr key={a.id} className="border-t border-line">
                        <td className="py-3 pr-4">
                          <span className="block text-ui font-medium">{a.name}</span>
                          <span className="block text-support text-muted">
                            {a.country || "country not given"} · {a.sector}
                          </span>
                        </td>
                        <td className="py-3 pr-4 text-support tabular-nums text-muted whitespace-nowrap">
                          {a.lat.toFixed(4)}, {a.lon.toFixed(4)}
                        </td>
                        <td className="py-3 pr-4 text-ui whitespace-nowrap">
                          {a.region}
                        </td>
                        <td className="py-3 pr-4 text-ui whitespace-nowrap">
                          {a.occupancy}
                        </td>
                        <td className="py-3 pr-4 text-ui tabular-nums whitespace-nowrap">
                          {a.value > 0 ? (
                            money(a.value, pf.currency)
                          ) : (
                            <span className="text-warn">not given</span>
                          )}
                        </td>
                        <td className="py-3 pr-1">
                          {a.has_financials ? (
                            <Chip tone="flat">supplied</Chip>
                          ) : (
                            <Chip tone="warn">not supplied</Chip>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-support text-muted mt-4 leading-snug ac-prose">
                {pf.note} Region is derived from the coordinates and selects the
                JRC continental damage function; occupancy is mapped from{" "}
                {pf.source_format === "oed"
                  ? "the OED occupancy code"
                  : "the sector column"}{" "}
                onto the JRC/HAZUS classes the curve library holds.
              </p>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

export default UploadView;
