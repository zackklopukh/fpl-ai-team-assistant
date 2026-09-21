"use client";

/**
 * One ranked plan, with its working shown.
 *
 * ARCHITECTURE.md: "the explanation is the product, more than the
 * recommendation is." So the reasoning sentence sits directly under the label
 * at full size, and the per-gameweek numbers the answer was computed from are
 * one click away rather than absent. A bare verdict with no visible working is
 * the failure mode this component exists to avoid.
 *
 * And: "'Hold your transfer' is frequently optimal and users will not believe a
 * tool that never says it." A hold is drawn as a plan, in the same card, with
 * the same weight. It is never greyed out and never labelled as nothing
 * happening.
 *
 * Relative imports: the test runner has no path aliases and this component is
 * rendered in the tests.
 *
 * Money is integer tenths; every money string comes from lib/format.ts.
 */

import { useId, useState } from "react";

import PlayerChip from "./PlayerChip";
import { formatPrice } from "../lib/format";
import type { NextFixturesByTeam } from "../lib/nextFixtures";
import { isHoldPlan, type Plan, type Transfer } from "../lib/optimizerTypes";
import type { PlayerIndex } from "../lib/squad";

export interface PlanCardProps {
  plan: Plan;
  /** Expected points from holding, over the horizon. The number to beat. */
  baselineXp: number;
  /** For turning element ids in the XI and bench into names. */
  index: PlayerIndex;
  /** 0 is the recommended plan. */
  rank: number;
  /**
   * Each club's opponents next gameweek, so a named player shows who he plays.
   * Optional: without it the chips simply leave the opponent out.
   */
  nextFixtures?: NextFixturesByTeam;
}

/** "GW6-8" from the plan's own breakdown, so the xP figures say what they cover. */
function horizonOf(plan: Plan): string | undefined {
  const gws = plan.perGwBreakdown.map((w) => w.gw);
  if (gws.length === 0) return undefined;
  const lo = Math.min(...gws);
  const hi = Math.max(...gws);
  return lo === hi ? `in GW${lo}` : `over GW${lo}–${hi}`;
}

/** Total projected points for a side of the move, or null if any is unknown. */
function sideXp(transfers: readonly Transfer[]): number | null {
  if (transfers.length === 0 || transfers.some((t) => t.xp === null)) return null;
  return transfers.reduce((sum, t) => sum + (t.xp as number), 0);
}

/** A name that links to the player's page, with who he is on hover. */
function PlayerLink({ index, elementId }: { index: PlayerIndex; elementId: number }) {
  const meta = playerMeta(index, elementId);
  return (
    <a
      href={`/players/${elementId}`}
      target="_blank"
      rel="noopener"
      title={meta ? `${meta} — opens his stats in a new tab` : "Opens his stats in a new tab"}
      className="underline decoration-slate-300 underline-offset-2 hover:decoration-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:decoration-slate-600"
    >
      {playerName(index, elementId)}
    </a>
  );
}

/** One decimal, signed, for a points delta. Points are not money. */
function formatPoints(value: number, decimals = 1): string {
  if (!Number.isFinite(value)) return "—";
  return value.toFixed(decimals);
}

function formatDelta(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${formatPoints(Math.abs(value))}`;
}

export function playerName(index: PlayerIndex, elementId: number): string {
  return index.get(elementId)?.webName ?? `Player ${elementId}`;
}

/** "GKP · ARS", when we know the player. Nothing when we do not. */
function playerMeta(index: PlayerIndex, elementId: number): string | null {
  const player = index.get(elementId);
  return player ? `${player.position} · ${player.teamShortName}` : null;
}

export default function PlanCard({
  plan,
  baselineXp,
  index,
  rank,
  nextFixtures,
}: PlanCardProps) {
  const [open, setOpen] = useState(false);
  const breakdownId = useId();

  const hold = isHoldPlan(plan);
  const recommended = rank === 0;
  const better = plan.deltaXp > 0.0001;
  const worse = plan.deltaXp < -0.0001;

  // The horizon total this plan expects. Δ is stated after hits, so adding it to
  // the baseline gives the number the manager would actually score.
  const planXp = baselineXp + plan.deltaXp;

  const horizon = horizonOf(plan);
  const outXp = sideXp(plan.transfersOut);
  const inXp = sideXp(plan.transfersIn);
  const opponentsOf = (elementId: number) => {
    const player = index.get(elementId);
    return nextFixtures && player ? nextFixtures[player.teamFplId] : undefined;
  };

  return (
    <article
      data-testid="plan-card"
      data-plan-label={plan.label}
      data-plan-hold={hold ? "true" : "false"}
      className={[
        "rounded-lg border p-4",
        recommended
          ? "border-sky-400 bg-sky-50/60 dark:border-sky-500/50 dark:bg-sky-500/5"
          : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950",
      ].join(" ")}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-slate-900 dark:text-slate-50">
              {plan.label}
            </h3>
            {recommended && (
              <span className="rounded-full bg-sky-600 px-2 py-0.5 text-xs font-medium text-white dark:bg-sky-500">
                Recommended
              </span>
            )}
            {hold && (
              // Said out loud, not implied by an empty transfer list. A hold
              // that looks like a missing answer is read as a broken tool.
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-900 dark:bg-emerald-500/20 dark:text-emerald-200">
                No transfer
              </span>
            )}
            {plan.hitCost > 0 && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-500/20 dark:text-amber-200">
                −{plan.hitCost} point hit
              </span>
            )}
          </div>
        </div>

        <div className="text-right">
          <div
            className={[
              "text-2xl font-semibold tabular-nums",
              better
                ? "text-emerald-700 dark:text-emerald-300"
                : worse
                  ? "text-rose-700 dark:text-rose-300"
                  : "text-slate-700 dark:text-slate-200",
            ].join(" ")}
          >
            {formatDelta(plan.deltaXp)}
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            {hold && Math.abs(plan.deltaXp) < 0.0001
              ? "this is the baseline"
              : "points vs holding"}
            {plan.hitCost > 0 ? ", after the hit" : ""}
          </div>
        </div>
      </header>

      {/* The explanation, at full size and above the detail. */}
      {plan.reasoning && (
        <p className="mt-3 text-sm leading-6 text-slate-700 dark:text-slate-200">
          {plan.reasoning}
        </p>
      )}

      {/* The moves. A hold says so in a sentence rather than showing two empty lists. */}
      <div className="mt-4">
        {hold ? (
          <p className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
            Keep all fifteen. Your free transfer rolls to next week, and rolling is
            worth more than a move that gains less than it costs.
          </p>
        ) : (
          <dl className="grid gap-3 sm:grid-cols-2">
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-rose-700 dark:text-rose-300">
                Out
              </dt>
              <dd>
                <ul className="mt-1 flex flex-col gap-1">
                  {plan.transfersOut.map((transfer) => (
                    <li
                      key={`out-${transfer.elementId}`}
                      className="text-sm text-slate-800 dark:text-slate-100"
                    >
                      <PlayerChip
                        elementId={transfer.elementId}
                        index={index}
                        fallbackName={transfer.webName}
                        opponents={opponentsOf(transfer.elementId)}
                        priceTenths={transfer.priceTenths}
                        priceLabel="What you bank: your selling price, not the list price"
                        xp={transfer.xp}
                        horizonLabel={horizon}
                      />
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-300">
                In
              </dt>
              <dd>
                <ul className="mt-1 flex flex-col gap-1">
                  {plan.transfersIn.map((transfer) => (
                    <li
                      key={`in-${transfer.elementId}`}
                      className="text-sm text-slate-800 dark:text-slate-100"
                    >
                      <PlayerChip
                        elementId={transfer.elementId}
                        index={index}
                        fallbackName={transfer.webName}
                        opponents={opponentsOf(transfer.elementId)}
                        priceTenths={transfer.priceTenths}
                        priceLabel="What he costs you: today's list price"
                        xp={transfer.xp}
                        horizonLabel={horizon}
                      />
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          </dl>
        )}
        {!hold && outXp !== null && inXp !== null && (
          // The reason for the move, in one line: what leaves against what
          // arrives, before the hit. delta_xp above is the net after hits and
          // lineup changes, so the two need not match.
          <p className="mt-3 text-xs text-slate-600 dark:text-slate-300">
            Projected {horizon ?? "over the horizon"}:{" "}
            <span className="tabular-nums">{outXp.toFixed(1)}</span> out,{" "}
            <span className="tabular-nums">{inXp.toFixed(1)}</span> in (
            <span className="tabular-nums font-medium">
              {formatDelta(inXp - outXp)}
            </span>{" "}
            before any hit, if everyone played every week).
          </p>
        )}
      </div>

      {/* Headline numbers. bank_after only when the service supplied it — zero
          would be a claim about the bank rather than the absence of one. */}
      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-slate-200 pt-3 text-sm sm:grid-cols-4 dark:border-slate-800">
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">Captain</dt>
          <dd className="font-medium text-slate-900 dark:text-slate-50">
            <PlayerLink index={index} elementId={plan.captain} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">Vice</dt>
          <dd className="font-medium text-slate-900 dark:text-slate-50">
            <PlayerLink index={index} elementId={plan.viceCaptain} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">
            Expected over the horizon
          </dt>
          <dd className="font-medium tabular-nums text-slate-900 dark:text-slate-50">
            {formatPoints(planXp)} pts
          </dd>
        </div>
        {plan.bankAfterTenths !== null && (
          <div>
            <dt className="text-xs text-slate-500 dark:text-slate-400">Bank after</dt>
            <dd className="font-medium tabular-nums text-slate-900 dark:text-slate-50">
              {formatPrice(plan.bankAfterTenths)}
            </dd>
          </div>
        )}
      </dl>

      {/* The working. Collapsed, because eleven names and five gameweeks is a
          lot on screen; present, because a recommendation you cannot inspect is
          a recommendation you cannot argue with. */}
      <div className="mt-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-controls={breakdownId}
          className="rounded text-sm font-medium text-sky-700 underline underline-offset-2 hover:text-sky-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-sky-300 dark:hover:text-sky-200"
        >
          {open ? "Hide the working" : "Show the working"}
        </button>

        <div id={breakdownId} hidden={!open} className="mt-3 flex flex-col gap-4">
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Starting eleven
            </h4>
            <ul className="mt-1 flex flex-wrap gap-1.5">
              {plan.xi.map((elementId) => (
                <li
                  key={`xi-${elementId}`}
                  title={playerMeta(index, elementId) ?? undefined}
                  className={[
                    "rounded border px-2 py-0.5 text-xs",
                    elementId === plan.captain
                      ? "border-sky-400 bg-sky-100 font-semibold text-sky-900 dark:border-sky-500/50 dark:bg-sky-500/20 dark:text-sky-100"
                      : "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300",
                  ].join(" ")}
                >
                  <PlayerLink index={index} elementId={elementId} />
                  {elementId === plan.captain && " (C)"}
                  {elementId === plan.viceCaptain && " (V)"}
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Bench, in autosub order
            </h4>
            <ol className="mt-1 flex flex-wrap gap-1.5">
              {plan.benchOrder.map((elementId, position) => (
                <li
                  key={`bench-${elementId}`}
                  title={playerMeta(index, elementId) ?? undefined}
                  className="rounded border border-dashed border-slate-300 px-2 py-0.5 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-400"
                >
                  <span className="tabular-nums text-slate-400">{position + 1}.</span>{" "}
                  <PlayerLink index={index} elementId={elementId} />
                </li>
              ))}
            </ol>
          </div>

          {plan.perGwBreakdown.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Gameweek by gameweek
              </h4>
              <div className="mt-1 overflow-x-auto">
                <table className="w-full min-w-[26rem] text-left text-sm">
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      <th scope="col" className="py-1 pr-3 font-medium">
                        GW
                      </th>
                      <th scope="col" className="py-1 pr-3 font-medium">
                        Expected
                      </th>
                      <th scope="col" className="py-1 pr-3 font-medium">
                        Captain
                      </th>
                      <th scope="col" className="py-1 font-medium">
                        Fixtures
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.perGwBreakdown.map((week) => {
                      // Fixtures join one-to-many (CLAUDE.md invariant 6). A
                      // blank and a double are the two facts that change a
                      // gameweek's shape, so they are named, not counted away.
                      const blanks: number[] = [];
                      const doubles: number[] = [];
                      for (const elementId of plan.xi) {
                        const count = week.nFixtures.get(elementId);
                        if (count === undefined) continue;
                        if (count === 0) blanks.push(elementId);
                        else if (count > 1) doubles.push(elementId);
                      }
                      return (
                        <tr
                          key={week.gw}
                          data-testid="breakdown-row"
                          className="border-t border-slate-100 dark:border-slate-800"
                        >
                          <th
                            scope="row"
                            className="py-1.5 pr-3 font-medium tabular-nums text-slate-900 dark:text-slate-100"
                          >
                            {week.gw}
                          </th>
                          <td className="py-1.5 pr-3 tabular-nums text-slate-700 dark:text-slate-200">
                            {formatPoints(week.xp, 2)}
                          </td>
                          <td className="py-1.5 pr-3 text-slate-700 dark:text-slate-200">
                            {playerName(index, week.captainElementId)}
                          </td>
                          <td className="py-1.5 text-xs text-slate-600 dark:text-slate-300">
                            {week.nFixtures.size === 0 ? (
                              <span className="text-slate-400">—</span>
                            ) : blanks.length === 0 && doubles.length === 0 ? (
                              <span className="text-slate-400">
                                all {plan.xi.length} play once
                              </span>
                            ) : (
                              <span className="flex flex-wrap gap-x-2">
                                {doubles.length > 0 && (
                                  <span className="text-emerald-700 dark:text-emerald-300">
                                    double:{" "}
                                    {doubles
                                      .map((id) => playerName(index, id))
                                      .join(", ")}
                                  </span>
                                )}
                                {blanks.length > 0 && (
                                  <span className="text-rose-700 dark:text-rose-300">
                                    blank:{" "}
                                    {blanks.map((id) => playerName(index, id)).join(", ")}
                                  </span>
                                )}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
