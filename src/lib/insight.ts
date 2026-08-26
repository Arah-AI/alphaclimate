/**
 * The two endpoints the landing view added: what the engine refuses to model,
 * and the grounded analyst.
 *
 * These live outside lib/api.ts on purpose: that file is being edited by
 * another workstream, and a ten line fetch helper is cheaper than a merge.
 */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export interface ApiFailure {
  status: number;
  detail: string;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    const f: ApiFailure = { status: 0, detail: "Cannot reach the risk engine." };
    throw f;
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the default */
    }
    const f: ApiFailure = { status: res.status, detail };
    throw f;
  }
  return (await res.json()) as T;
}

export function isApiFailure(e: unknown): e is ApiFailure {
  return typeof e === "object" && e !== null && "status" in e && "detail" in e;
}

/* ------------------------------------------------------- curve gap ledger */

export interface CurveGap {
  hazard: string;
  exposure: string;
  why: string;
  what_would_close_it: string;
}

export interface CurveGaps {
  gaps: CurveGap[];
  curves_loaded: number;
}

export function getCurveGaps(): Promise<CurveGaps> {
  return call<CurveGaps>("/api/curves/gaps");
}

/* ------------------------------------------------------------- analyst */

export interface AnalystStatus {
  available: boolean;
  reason: string | null;
  model?: string;
}

export interface AnalystAnswer {
  answer: string | null;
  grounded: boolean | null;
  withheld?: boolean;
  refused: boolean;
  reason: string | null;
  ungrounded_numbers: number[];
  model?: string;
  usage?: { input_tokens: number; output_tokens: number };
}

export function getAnalystStatus(): Promise<AnalystStatus> {
  return call<AnalystStatus>("/api/analyst/status");
}

export function askAnalyst(
  question: string,
  scenario: string,
): Promise<AnalystAnswer> {
  return call<AnalystAnswer>("/api/analyst/ask", {
    method: "POST",
    body: JSON.stringify({ question, scenario }),
  });
}

/* --------------------------------------------------------- headline extras
   The engine gained a permanent-inundation split after lib/types.ts was
   written. Widened here rather than editing a shared file. */

export interface HeadlineExtras {
  permanent_writedown?: number;
  permanent_writedown_pct?: number;
  permanently_inundated_count?: number;
}
