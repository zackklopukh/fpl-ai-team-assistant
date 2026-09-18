"use client";

/**
 * The fixture ticker: clubs down the side, gameweeks across, each cell holding
 * a LIST of fixtures.
 *
 * Three things this component exists to get right, in order of how expensive
 * they are to get wrong:
 *
 * 1. A double gameweek renders as two chips in one cell, with an explicit "×2"
 *    marker. Rendering a double as a single is worse than having no ticker —
 *    the user makes a transfer on a fixture that is not the only one.
 * 2. A blank renders as a visibly empty cell: dashed outline, a dash, and the
 *    word "Blank" for a screen reader. Not a gap, not a neutral grey box that
 *    reads as an ordinary fixture.
 * 3. Difficulty is never communicated by colour alone. Every chip carries its
 *    FDR digit, so the ticker is readable in greyscale, in both themes, and to
 *    a colour-blind user. Colour is the fast path, the digit is the truth.
 *
 * Layout: below `sm` this is a stack of club cards with wrapped chips, because
 * a twenty-by-five grid at 375px either overflows or becomes unreadable. From
 * `sm` up it is the grid, with a sticky club column.
 *
 * No crests, no kits, no club colours borrowed from anyone — club short codes
 * and this file's own difficulty scale only (CLAUDE.md, trademarks).
 */

import { useMemo, useState } from "react";

import type { FixtureTickerRow, TeamFixture } from "@/lib/fixtures";

type SortKey = "club" | "difficulty";

/**
 * The FDR scale. Colour AND a digit AND a word — the digit is rendered in every
 * chip, so nothing here depends on distinguishing green from red.
 */
const FDR_STYLES: Record<number, { cell: string; label: string }> = {
  1: {
    cell: "border-emerald-600/40 bg-emerald-100 text-emerald-950 dark:border-emerald-400/30 dark:bg-emerald-950/70 dark:text-emerald-100",
    label: "very easy",
  },
  2: {
    cell: "border-lime-600/40 bg-lime-100 text-lime-950 dark:border-lime-400/30 dark:bg-lime-950/70 dark:text-lime-100",
    label: "easy",
  },
  3: {
    cell: "border-slate-400/50 bg-slate-100 text-slate-900 dark:border-slate-500/40 dark:bg-slate-800 dark:text-slate-100",
    label: "average",
  },
  4: {
    cell: "border-orange-600/40 bg-orange-100 text-orange-950 dark:border-orange-400/30 dark:bg-orange-950/70 dark:text-orange-100",
    label: "hard",
  },
  5: {
    cell: "border-rose-700/40 bg-rose-100 text-rose-950 dark:border-rose-400/30 dark:bg-rose-950/70 dark:text-rose-100",
    label: "very hard",
  },
};

const UNKNOWN_FDR = {
  cell: "border-slate-300 bg-white text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300",
  label: "unrated",
};

function fdrStyle(difficulty: number | null) {
  if (difficulty === null) return UNKNOWN_FDR;
  return FDR_STYLES[difficulty] ?? UNKNOWN_FDR;
}

export interface TickerTeamNames {
  /** fpl_id -> short code, for naming opponents without a second lookup. */
  short: Record<number, string>;
  full: Record<number, string>;
}

/** One opponent chip: short code, H/A, and the FDR digit. */
function FixtureChip({
  fixture,
  names,
  size = "md",
}: {
  fixture: TeamFixture;
  names: TickerTeamNames;
  size?: "sm" | "md";
}) {
  const style = fdrStyle(fixture.difficulty);
  const opponent = names.short[fixture.opponent_fpl_id] ?? "???";
  const opponentFull = names.full[fixture.opponent_fpl_id] ?? "Unknown club";
  const venue = fixture.is_home ? "H" : "A";
  const venueWord = fixture.is_home ? "at home to" : "away at";
  const fdrWord = fixture.difficulty === null ? "unrated" : `${fixture.difficulty} (${style.label})`;

  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 font-medium tabular-nums ${
        size === "sm" ? "py-0.5 text-[0.7rem]" : "py-1 text-xs"
      } ${style.cell}`}
      title={`${venueWord} ${opponentFull} — difficulty ${fdrWord}`}
    >
      <span className="sr-only">{venueWord} </span>
      <span>{opponent}</span>
      <span
        aria-hidden="true"
        className="rounded-sm bg-black/10 px-1 text-[0.65rem] font-semibold uppercase dark:bg-white/15"
      >
        {venue}
      </span>
      <span className="sr-only">difficulty </span>
      <span className="font-semibold">{fixture.difficulty ?? "?"}</span>
    </span>
  );
}

/**
 * One cell: zero, one or two chips.
 *
 * The zero case is the whole reason this is a component and not a `.map()`
 * inline — a blank has to look deliberately empty.
 */
function GameweekCell({
  fixtures,
  names,
  size = "md",
}: {
  fixtures: TeamFixture[];
  names: TickerTeamNames;
  size?: "sm" | "md";
}) {
  if (fixtures.length === 0) {
    return (
      <span
        className="inline-flex w-full items-center justify-center gap-1 rounded border border-dashed border-slate-300 bg-[repeating-linear-gradient(135deg,transparent,transparent_4px,rgba(100,116,139,0.14)_4px,rgba(100,116,139,0.14)_5px)] px-1.5 py-1 text-xs text-slate-400 dark:border-slate-700 dark:text-slate-500"
        title="Blank gameweek — this club does not play"
      >
        <span aria-hidden="true">—</span>
        <span className="sr-only">Blank gameweek, no fixture</span>
      </span>
    );
  }

  return (
    <span className="flex flex-col items-stretch gap-0.5">
      {fixtures.length > 1 && (
        <span className="text-[0.6rem] font-semibold uppercase tracking-wide text-indigo-700 dark:text-indigo-300">
          <span aria-hidden="true">×{fixtures.length} double</span>
          <span className="sr-only">
            Double gameweek, {fixtures.length} fixtures
          </span>
        </span>
      )}
      {fixtures.map((f) => (
        <FixtureChip key={f.fixture_id} fixture={f} names={names} size={size} />
      ))}
    </span>
  );
}

function SummaryBadges({ row }: { row: FixtureTickerRow }) {
  const { difficulty } = row;
  return (
    <span className="flex flex-wrap items-center gap-1 text-[0.65rem]">
      <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-200">
        {difficulty.fixtureCount} fixture{difficulty.fixtureCount === 1 ? "" : "s"}
      </span>
      <span
        className="rounded bg-slate-100 px-1.5 py-0.5 font-medium tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-200"
        title="Total FDR over the window: a double adds both fixtures, a blank adds nothing"
      >
        FDR {difficulty.totalDifficulty}
      </span>
      {difficulty.doubleGws.length > 0 && (
        <span className="rounded bg-indigo-100 px-1.5 py-0.5 font-medium text-indigo-900 dark:bg-indigo-950 dark:text-indigo-200">
          DGW {difficulty.doubleGws.join(", ")}
        </span>
      )}
      {difficulty.blankGws.length > 0 && (
        <span className="rounded border border-dashed border-slate-400 px-1.5 py-0.5 font-medium text-slate-600 dark:border-slate-600 dark:text-slate-300">
          Blank {difficulty.blankGws.join(", ")}
        </span>
      )}
    </span>
  );
}

export default function FixtureTicker({
  rows,
  gws,
  names,
}: {
  rows: FixtureTickerRow[];
  gws: number[];
  names: TickerTeamNames;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("club");

  const sorted = useMemo(() => {
    const copy = [...rows];
    if (sortKey === "club") {
      copy.sort((a, b) => a.team.name.localeCompare(b.team.name));
    } else {
      // Easiest run first. A blank-heavy run scores low on total FDR precisely
      // because a blank adds nothing, so total alone would flatter it — count
      // of fixtures breaks the tie the other way.
      copy.sort((a, b) => {
        const at = a.difficulty.totalDifficulty;
        const bt = b.difficulty.totalDifficulty;
        if (at !== bt) return at - bt;
        if (a.difficulty.fixtureCount !== b.difficulty.fixtureCount) {
          return b.difficulty.fixtureCount - a.difficulty.fixtureCount;
        }
        return a.team.name.localeCompare(b.team.name);
      });
    }
    return copy;
  }, [rows, sortKey]);

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div
          role="group"
          aria-label="Sort clubs"
          className="flex gap-1 rounded-lg border border-slate-200 p-1 dark:border-slate-800"
        >
          <SortButton
            active={sortKey === "club"}
            onClick={() => setSortKey("club")}
            label="By club"
          />
          <SortButton
            active={sortKey === "difficulty"}
            onClick={() => setSortKey("difficulty")}
            label="Easiest run first"
          />
        </div>
        <Legend />
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Phone: one card per club. No horizontal scroll, no squeezed grid.  */}
      {/* ------------------------------------------------------------------ */}
      <ul className="flex flex-col gap-2 sm:hidden">
        {sorted.map((row) => (
          <li
            key={row.team.fpl_id}
            className="rounded-xl border border-slate-200 p-3 dark:border-slate-800"
          >
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                <span className="font-mono text-xs text-slate-500 dark:text-slate-400">
                  {row.team.short_name}
                </span>{" "}
                {row.team.name}
              </h3>
            </div>
            <div className="mt-1.5">
              <SummaryBadges row={row} />
            </div>
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {row.gameweeks.map((week) => (
                <li
                  key={week.gw}
                  className="flex min-w-[4.5rem] flex-col gap-0.5"
                >
                  <span className="text-[0.6rem] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    GW{week.gw}
                  </span>
                  <GameweekCell
                    fixtures={week.fixtures}
                    names={names}
                    size="sm"
                  />
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>

      {/* ------------------------------------------------------------------ */}
      {/* Tablet and up: the grid, with a sticky club column.                */}
      {/* ------------------------------------------------------------------ */}
      <div className="hidden overflow-x-auto rounded-xl border border-slate-200 sm:block dark:border-slate-800">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">
            Upcoming fixtures by club. Each cell lists every fixture that club
            plays in that gameweek: none for a blank, two for a double.
          </caption>
          <thead>
            <tr className="bg-slate-50 dark:bg-slate-900/60">
              <th
                scope="col"
                className="sticky left-0 z-10 border-b border-slate-200 bg-slate-50 px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400"
              >
                Club
              </th>
              {gws.map((gw) => (
                <th
                  key={gw}
                  scope="col"
                  className="border-b border-slate-200 px-2 py-2 text-center text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400"
                >
                  GW{gw}
                </th>
              ))}
              <th
                scope="col"
                title="Total FDR over the window: a double adds both fixtures, a blank adds nothing"
                className="border-b border-slate-200 px-2 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400"
              >
                Total
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr
                key={row.team.fpl_id}
                className="border-b border-slate-100 last:border-0 dark:border-slate-900"
              >
                <th
                  scope="row"
                  className="sticky left-0 z-10 max-w-[10rem] bg-white px-3 py-2 text-left dark:bg-slate-950"
                >
                  <span className="block font-mono text-xs text-slate-500 dark:text-slate-400">
                    {row.team.short_name}
                  </span>
                  <span className="block truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                    {row.team.name}
                  </span>
                </th>
                {row.gameweeks.map((week) => (
                  <td key={week.gw} className="px-1.5 py-2 align-middle">
                    <GameweekCell fixtures={week.fixtures} names={names} />
                  </td>
                ))}
                <td className="px-2 py-2 text-right align-middle">
                  <span className="block text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-100">
                    {row.difficulty.totalDifficulty}
                  </span>
                  <span className="block text-[0.65rem] tabular-nums text-slate-500 dark:text-slate-400">
                    {row.difficulty.fixtureCount} fx
                    {row.difficulty.doubleGws.length > 0 && " · DGW"}
                    {row.difficulty.blankGws.length > 0 && " · BGW"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[0.65rem] text-slate-500 dark:text-slate-400">
      <span className="font-medium uppercase tracking-wide">Difficulty</span>
      {[1, 2, 3, 4, 5].map((fdr) => (
        <span
          key={fdr}
          className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-medium tabular-nums ${FDR_STYLES[fdr].cell}`}
        >
          {fdr}
          <span className="hidden font-normal md:inline">
            {FDR_STYLES[fdr].label}
          </span>
        </span>
      ))}
      <span className="inline-flex items-center gap-1 rounded border border-dashed border-slate-300 px-1.5 py-0.5 dark:border-slate-700">
        — blank
      </span>
      <span className="font-semibold text-indigo-700 dark:text-indigo-300">
        ×2 double
      </span>
    </div>
  );
}

function SortButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
        active
          ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
          : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
      }`}
    >
      {label}
    </button>
  );
}
