"use client";

/**
 * Ask the optimizer, and show what came back.
 *
 * Mounted inside the squad builder so a legal fifteen can be turned into advice
 * without leaving the page or storing anything about who asked.
 *
 * Three things from ARCHITECTURE.md shape this component:
 *
 *   - "Hold your transfer is frequently optimal and users will not believe a
 *     tool that never says it." Every plan is rendered the same way and a hold
 *     is never demoted. PlanCard does the work.
 *   - "The explanation is the product, more than the recommendation is." The
 *     reasoning and the per-gameweek numbers are on the card, not in a tooltip.
 *   - "A recommendation computed at 22:00 can be invalid at 02:00 when prices
 *     change." `data_as_of` is a line of text under the answer, always visible.
 *
 * And the differential control, because a correct optimizer converges on the
 * template squad and users read that as the tool being broken.
 *
 * Privacy: this posts a squad and nothing else. No id, no name, no session.
 *
 * Relative imports: the test runner has no path aliases and this component's
 * pieces are rendered in the tests.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import PlanCard from "./PlanCard";
import { buildOptimizeRequest } from "../lib/optimizer";
import {
  DEFAULT_HORIZON,
  MAX_FREE_TRANSFERS,
  MAX_HORIZON,
  MIN_HORIZON,
  parseOptimizeResponse,
  type OptimizeResponse,
} from "../lib/optimizerTypes";
import type { PlayerIndex, SquadState } from "../lib/squad";

export interface AdvicePanelProps {
  squad: SquadState;
  index: PlayerIndex;
  /** Only a complete, legal fifteen can be optimized. */
  squadValid: boolean;
  /** The gameweek to plan from, when the page knows it. */
  currentGw?: number | null;
  /** Injected in tests. Defaults to the global fetch. */
  fetchImpl?: typeof fetch;
}

type Status = "idle" | "loading" | "done" | "error";

/** A UTC as-of time, spelled out. Not a relative "2 hours ago" — the absolute
 *  time is what a user compares against tonight's price change. */
function formatAsOf(iso: string): string {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return "unknown";
  return (
    new Intl.DateTimeFormat("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "UTC",
      hour12: false,
    }).format(date) + " UTC"
  );
}

/** Stale once the overnight price change has run between then and now. */
function looksStale(iso: string): boolean {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return false;
  return Date.now() - then > 12 * 60 * 60 * 1000;
}

export default function AdvicePanel({
  squad,
  index,
  squadValid,
  currentGw,
  fetchImpl,
}: AdvicePanelProps) {
  const [freeTransfers, setFreeTransfers] = useState(1);
  const [horizon, setHorizon] = useState(DEFAULT_HORIZON);
  const [gw, setGw] = useState(currentGw ?? 1);
  const [differential, setDifferential] = useState(false);
  const [maxOwnership, setMaxOwnership] = useState(15);

  const [status, setStatus] = useState<Status>("idle");
  const [response, setResponse] = useState<OptimizeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // A cold start can take seconds. A counter is the difference between "slow"
  // and "hung" from the outside, and this service is allowed to be slow.
  // The counter is reset where the request starts, not here: resetting in the
  // effect body would be a setState on every render into "loading".
  useEffect(() => {
    if (status !== "loading") return;
    const timer = window.setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [status]);

  const ask = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setStatus("loading");
    setError(null);
    setElapsed(0);

    const request = buildOptimizeRequest(squad, index, {
      freeTransfers,
      currentGw: gw,
      horizon,
      // The route overrides this with the app's own season: players are keyed
      // by (season, element_id) and the client does not get to pick the year.
      season: "",
      maxOwnership: differential ? maxOwnership : null,
    });

    const doFetch = fetchImpl ?? fetch;
    try {
      const res = await doFetch("/api/optimize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal: controller.signal,
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setError(
          (body as { error?: string } | null)?.error ??
            "The optimizer could not be reached. Try again in a moment.",
        );
        setStatus("error");
        return;
      }
      setResponse(parseOptimizeResponse(body));
      setStatus("done");
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(
        err instanceof Error && err.message
          ? `Could not reach the optimizer: ${err.message}`
          : "Could not reach the optimizer.",
      );
      setStatus("error");
    }
  }, [squad, index, freeTransfers, gw, horizon, differential, maxOwnership, fetchImpl]);

  const loading = status === "loading";

  return (
    <section
      aria-label="Transfer advice"
      className="flex flex-col gap-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800"
    >
      <div>
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          What should I do?
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-slate-600 dark:text-slate-300">
          The solver ranks a handful of courses of action over the next few
          gameweeks and explains each one. Holding your transfer is one of them,
          and it is often the right answer.
        </p>
      </div>

      {/* Controls. */}
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <label
            htmlFor="advice-gw"
            className="block text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Gameweek
          </label>
          <input
            id="advice-gw"
            type="number"
            min={1}
            max={38}
            value={gw}
            onChange={(event) => setGw(Number(event.target.value))}
            className="mt-1 w-20 rounded border border-slate-300 bg-white px-2 py-1.5 text-sm tabular-nums text-slate-900 focus:border-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-50"
          />
        </div>

        <div>
          <label
            htmlFor="advice-ft"
            className="block text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Free transfers
          </label>
          <input
            id="advice-ft"
            type="number"
            min={0}
            max={MAX_FREE_TRANSFERS}
            value={freeTransfers}
            onChange={(event) => setFreeTransfers(Number(event.target.value))}
            className="mt-1 w-20 rounded border border-slate-300 bg-white px-2 py-1.5 text-sm tabular-nums text-slate-900 focus:border-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-50"
          />
        </div>

        <div>
          <label
            htmlFor="advice-horizon"
            className="block text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Gameweeks ahead
          </label>
          <select
            id="advice-horizon"
            value={horizon}
            onChange={(event) => setHorizon(Number(event.target.value))}
            className="mt-1 rounded border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-50"
          >
            {Array.from({ length: MAX_HORIZON - MIN_HORIZON + 1 }, (_, i) => i + MIN_HORIZON).map(
              (value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ),
            )}
          </select>
        </div>

        <button
          type="button"
          onClick={ask}
          disabled={!squadValid || loading}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {loading ? "Working…" : status === "done" ? "Ask again" : "Get advice"}
        </button>
      </div>

      {/* Differential mode. ARCHITECTURE.md: a correct optimizer converges on
          the template, and users read that as the tool being broken. */}
      <div className="rounded border border-slate-200 p-3 dark:border-slate-800">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
          <input
            type="checkbox"
            checked={differential}
            onChange={(event) => setDifferential(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-sky-600 focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-600"
          />
          Differential mode
        </label>
        <p className="mt-1 max-w-2xl text-xs text-slate-500 dark:text-slate-400">
          A correct optimizer converges on the template squad, because the
          template is popular precisely because it is close to optimal. Cap
          ownership to ask for an answer fewer people have.
        </p>
        {differential && (
          <div className="mt-3 flex items-center gap-3">
            <label
              htmlFor="advice-ownership"
              className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
            >
              Max ownership
            </label>
            <input
              id="advice-ownership"
              type="range"
              min={1}
              max={100}
              step={1}
              value={maxOwnership}
              onChange={(event) => setMaxOwnership(Number(event.target.value))}
              className="w-48"
            />
            <span className="tabular-nums text-sm text-slate-800 dark:text-slate-100">
              {maxOwnership}%
            </span>
          </div>
        )}
      </div>

      {!squadValid && (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Finish a legal fifteen above and this turns on.
        </p>
      )}

      {/* Status and results. */}
      <div aria-live="polite" className="flex flex-col gap-4">
        {loading && (
          <p className="rounded border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
            Solving{elapsed > 0 ? ` — ${elapsed}s` : ""}.
            {elapsed >= 4 &&
              " The solver sleeps when nobody is using it, so the first request after a quiet spell takes a few seconds to wake it."}
          </p>
        )}

        {status === "error" && error && (
          <p
            role="alert"
            className="rounded border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900 dark:border-rose-500/40 dark:bg-rose-500/10 dark:text-rose-200"
          >
            {error}
          </p>
        )}

        {status === "done" && response && (
          <>
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-200 pb-2 dark:border-slate-800">
              <p className="text-sm text-slate-700 dark:text-slate-200">
                Holding scores about{" "}
                <span className="font-semibold tabular-nums">
                  {response.baselineXp.toFixed(1)}
                </span>{" "}
                points over the next {response.plans[0]?.perGwBreakdown.length || horizon}{" "}
                gameweeks. Everything below is measured against that.
              </p>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {response.plans.length} plan{response.plans.length === 1 ? "" : "s"}
              </p>
            </div>

            {response.truncated && (
              <p className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
                The solver hit its time limit and returned the best answer it had
                found. It is a good one, but it is not proven best.
              </p>
            )}

            {response.plans.length === 0 ? (
              <p className="text-sm text-slate-600 dark:text-slate-300">
                No plans came back for that squad.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {response.plans.map((plan, rank) => (
                  <PlanCard
                    key={`${plan.label}-${rank}`}
                    plan={plan}
                    baselineXp={response.baselineXp}
                    index={index}
                    rank={rank}
                  />
                ))}
              </div>
            )}

            {/* As-of time, in the open. A recommendation computed at 22:00 can
                be invalid at 02:00 once prices move — so this is a line of
                text, not a tooltip. */}
            <p
              data-testid="data-as-of"
              className={[
                "rounded border px-3 py-2 text-xs",
                looksStale(response.dataAsOf)
                  ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
                  : "border-slate-200 text-slate-500 dark:border-slate-800 dark:text-slate-400",
              ].join(" ")}
            >
              Prices and expected points as of{" "}
              <strong className="font-semibold">{formatAsOf(response.dataAsOf)}</strong>.
              {looksStale(response.dataAsOf) &&
                " Prices change overnight, so this answer may already be out of date — check before you transfer."}{" "}
              Model {response.modelVersion}, solver {response.solverVersion}, solved in{" "}
              {response.solveMs}ms.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
