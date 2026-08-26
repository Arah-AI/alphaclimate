"use client";

import * as React from "react";
import { clsx } from "clsx";
import { CornerDownLeft, ShieldCheck, Ban } from "lucide-react";
import { Card, CardHead, Chip, Skeleton } from "./ui";
import {
  askAnalyst,
  getAnalystStatus,
  isApiFailure,
  type AnalystAnswer,
  type AnalystStatus,
} from "@/lib/insight";

const SUGGESTED = [
  "What is driving most of the expected annual loss?",
  "How wide is the disagreement between models, and why?",
  "Which assets breach a covenant, and by how much?",
  "What is missing from these numbers?",
];

/** Renders [headline.eal] style citations as inline paths rather than noise. */
function WithCitations({ text }: { text: string }) {
  const parts = text.split(/(\[[A-Za-z0-9_.[\]]+\])/g);
  return (
    <p className="text-prose text-ink-2 leading-relaxed whitespace-pre-wrap ac-prose">
      {parts.map((p, i) =>
        /^\[[A-Za-z0-9_.[\]]+\]$/.test(p) ? (
          <code
            key={i}
            className="mx-[2px] rounded-full bg-brand-tint text-brand px-1.5 py-[1px] text-micro font-medium align-baseline"
          >
            {p.slice(1, -1)}
          </code>
        ) : (
          <React.Fragment key={i}>{p}</React.Fragment>
        ),
      )}
    </p>
  );
}

export function Analyst({ scenario }: { scenario: string }) {
  const [status, setStatus] = React.useState<AnalystStatus | null>(null);
  const [q, setQ] = React.useState("");
  const [asked, setAsked] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [answer, setAnswer] = React.useState<AnalystAnswer | null>(null);
  /** 503 means "no key", or the provider is down. A normal state, not a crash. */
  const [offline, setOffline] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let live = true;
    getAnalystStatus()
      .then((s) => live && setStatus(s))
      .catch(
        () =>
          live &&
          setStatus({
            available: false,
            reason: "The analyst service did not answer a status check.",
          }),
      );
    return () => {
      live = false;
    };
  }, []);

  const unconfigured = offline ?? (status && !status.available ? status.reason : null);

  const ask = React.useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || busy) return;
      setBusy(true);
      setAsked(text);
      setAnswer(null);
      setError(null);
      try {
        setAnswer(await askAnalyst(text, scenario));
        setOffline(null);
      } catch (e) {
        if (isApiFailure(e) && e.status === 503) setOffline(e.detail);
        else setError(isApiFailure(e) ? e.detail : "The analyst call failed.");
      } finally {
        setBusy(false);
      }
    },
    [busy, scenario],
  );

  return (
    <Card>
      <CardHead title="Ask the analyst">
        <Chip tone="flat">every figure traced</Chip>
      </CardHead>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask(q);
        }}
        className="flex gap-2 items-center"
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          disabled={!!unconfigured}
          aria-label="Ask a question about this run"
          placeholder={
            unconfigured
              ? "The analyst is not configured"
              : "Ask about this run, in plain English"
          }
          className="flex-1 min-w-0 rounded-full border border-line-2 bg-card px-4 py-2.5 text-prose placeholder:text-muted disabled:bg-canvas disabled:text-muted"
        />
        <button
          type="submit"
          disabled={busy || !!unconfigured || !q.trim()}
          className="flex items-center gap-2 rounded-full bg-charcoal text-white px-4 py-2.5 text-ui font-medium disabled:opacity-40 hover:bg-charcoal-2 transition-colors shrink-0"
        >
          <CornerDownLeft size={15} aria-hidden />
          Ask
        </button>
      </form>

      <ul className="flex flex-wrap gap-1.5 mt-3">
        {SUGGESTED.map((s) => (
          <li key={s}>
            <button
              type="button"
              disabled={!!unconfigured || busy}
              onClick={() => {
                setQ(s);
                void ask(s);
              }}
              className="rounded-full border border-line-2 px-3 py-[6px] text-support text-ink-2 hover:border-brand-soft hover:text-ink transition-colors disabled:opacity-45 disabled:hover:border-line-2"
            >
              {s}
            </button>
          </li>
        ))}
      </ul>

      {/* --------------------------------------------------------- output */}
      <div className="mt-4" aria-live="polite">
        {!status && !offline && <Skeleton className="h-[60px]" />}

        {unconfigured && (
          <div className="rounded-[16px] border border-line-2 bg-canvas px-4 py-3.5">
            <p className="text-ui font-semibold text-ink mb-1">
              The analyst is not configured
            </p>
            <p className="text-ui text-ink-2 leading-relaxed ac-prose">{unconfigured}</p>
          </div>
        )}

        {busy && <Skeleton className="h-[92px]" />}

        {!busy && error && (
          <div className="rounded-[16px] border border-danger/25 bg-danger-tint px-4 py-3.5">
            <p className="text-ui text-ink-2">{error}</p>
          </div>
        )}

        {!busy && answer && asked && (
          <div className="flex flex-col gap-3">
            <p className="text-support text-muted">
              <span className="font-semibold text-ink-2">Asked: </span>
              {asked}
            </p>

            {/* the guard fired: this is the product, shown as such */}
            {answer.withheld && (
              <div className="rounded-[16px] border border-brand/30 bg-brand-tint px-4 py-4">
                <p className="flex items-center gap-2 text-ui font-semibold text-brand mb-1.5">
                  <ShieldCheck size={17} aria-hidden />
                  Answer withheld by the numeric guard
                </p>
                <p className="text-ui text-ink-2 leading-relaxed ac-prose mb-3">
                  {answer.reason ??
                    "The answer contained figures that could not be traced to this run."}
                </p>
                {answer.ungrounded_numbers.length > 0 && (
                  <>
                    <p className="text-micro uppercase tracking-[0.1em] text-muted font-semibold mb-1.5">
                      Figures with no source in this run
                    </p>
                    <ul className="flex flex-wrap gap-1.5">
                      {answer.ungrounded_numbers.map((n, i) => (
                        <li key={`${n}-${i}`}>
                          <span className="inline-flex items-center gap-1 rounded-full bg-card border border-brand/25 px-2.5 py-[3px] text-support font-medium tabular-nums text-ink">
                            {n.toLocaleString("en-US")}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}

            {answer.refused && (
              <div className="rounded-[16px] border border-line-2 bg-canvas px-4 py-3.5">
                <p className="flex items-center gap-2 text-ui font-semibold text-ink mb-1">
                  <Ban size={16} aria-hidden className="text-muted" />
                  The model declined to answer
                </p>
                <p className="text-ui text-ink-2">{answer.reason}</p>
              </div>
            )}

            {answer.answer && (
              <div className="rounded-[16px] border border-line bg-canvas px-4 py-3.5">
                <WithCitations text={answer.answer} />
                <p className="text-micro text-muted mt-2.5 pt-2.5 border-t border-line-2">
                  Every figure above was checked against this run before it was shown
                  {answer.usage
                    ? ` · ${answer.usage.input_tokens.toLocaleString("en-US")} in / ${answer.usage.output_tokens.toLocaleString("en-US")} out`
                    : ""}
                  {answer.model ? ` · ${answer.model}` : ""}
                </p>
              </div>
            )}
          </div>
        )}
      </div>

      {/* the standing claim, true whether or not a key is present */}
      <p
        className={clsx(
          "text-support text-muted leading-snug ac-prose mt-4 pt-3.5 border-t border-line",
        )}
      >
        <b className="text-ink-2 font-semibold">How this differs. </b>
        Every number in an answer is matched against the computed run before you see
        it. A figure the model invented, rounded, or derived by arithmetic of its own
        has no source, and the whole answer is withheld rather than shown with a
        footnote. The refusal is the feature.
      </p>
    </Card>
  );
}
