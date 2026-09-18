/**
 * /fixtures — the fixture ticker.
 *
 * Server component. It reads Postgres (or the seed) and hands plain rows to one
 * client component. Nothing here calls the FPL API.
 *
 * The window it shows starts at the current gameweek — the deadline clock the
 * whole system derives from. When no fixture exists at or after that gameweek,
 * which is exactly the case on the checked-in seed, the page slides back to the
 * last gameweeks that DO have fixtures and says so in a banner. It does not
 * invent fixtures to fill the gap and it does not render twenty blank rows as
 * though every club had a blank.
 */

import type { Metadata } from "next";
import Link from "next/link";

import FixtureTicker, { type TickerTeamNames } from "@/components/FixtureTicker";
import { getCurrentGameweek, getTeams } from "@/lib/db";
import {
  firstScheduledGameweek,
  getAllFixtures,
  getFixtureTicker,
  lastScheduledGameweek,
} from "@/lib/fixtures";
import { formatDeadline } from "@/lib/format";

export const metadata: Metadata = {
  title: "Fixture ticker",
  description:
    "Upcoming Fantasy Premier League fixtures by club, with difficulty, double gameweeks and blanks shown for what they are.",
};

const HORIZONS = [3, 5, 8] as const;
const DEFAULT_HORIZON = 5;

function parseHorizon(raw: string | string[] | undefined): number {
  const value = Array.isArray(raw) ? raw[0] : raw;
  const n = Number(value);
  if (!Number.isFinite(n)) return DEFAULT_HORIZON;
  return Math.min(12, Math.max(1, Math.trunc(n)));
}

export default async function FixturesPage(props: PageProps<"/fixtures">) {
  const horizon = parseHorizon((await props.searchParams).horizon);

  const [teams, currentGameweek, allFixtures] = await Promise.all([
    getTeams(),
    getCurrentGameweek(),
    getAllFixtures(),
  ]);

  const preferredGw = currentGameweek?.gw ?? 1;
  const nextScheduled = firstScheduledGameweek(allFixtures, preferredGw);
  const lastScheduled = lastScheduledGameweek(allFixtures);

  // Three cases, all real: fixtures ahead of us (normal), no fixtures ahead but
  // some behind (the seed, and the summer), and no fixtures at all.
  const fromGw =
    nextScheduled ??
    (lastScheduled === null ? preferredGw : Math.max(1, lastScheduled - horizon + 1));
  const showingHistory = nextScheduled === null && lastScheduled !== null;
  const noFixturesAtAll = lastScheduled === null;

  // In the fallback case the window is trimmed to end at the last gameweek that
  // has fixtures. Otherwise a 5-week horizon over a 4-week sample would show a
  // column where every club blanks, which is technically true of the data and
  // completely misleading about the season.
  const effectiveHorizon = showingHistory
    ? Math.max(1, Math.min(horizon, (lastScheduled as number) - fromGw + 1))
    : horizon;

  const ticker = await getFixtureTicker(teams, fromGw, effectiveHorizon);

  const names: TickerTeamNames = {
    short: Object.fromEntries(teams.map((t) => [t.fpl_id, t.short_name])),
    full: Object.fromEntries(teams.map((t) => [t.fpl_id, t.name])),
  };

  const doubleGws = [
    ...new Set(ticker.rows.flatMap((r) => r.difficulty.doubleGws)),
  ].sort((a, b) => a - b);
  const blankGws = [
    ...new Set(ticker.rows.flatMap((r) => r.difficulty.blankGws)),
  ].sort((a, b) => a - b);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
      <header className="mb-8 flex flex-col gap-3">
        <Link
          href="/"
          className="text-xs font-medium text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          ← Back to overview
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50 sm:text-4xl">
          Fixture ticker
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          Who each club faces, how hard it is, and — the part most tickers get
          wrong — how many times they play. A club plays zero, one or two times
          in a gameweek. Two fixtures show as two chips, no fixture shows as an
          empty cell, and the total adds up every fixture rather than assuming
          one per week.
        </p>

        <dl className="flex flex-wrap gap-x-8 gap-y-2 text-xs text-slate-500 dark:text-slate-400">
          <div className="flex gap-2">
            <dt className="font-medium text-slate-700 dark:text-slate-200">
              Showing
            </dt>
            <dd className="tabular-nums">
              GW{ticker.fromGw}–{ticker.toGw}
            </dd>
          </div>
          {currentGameweek && (
            <>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700 dark:text-slate-200">
                  Current gameweek
                </dt>
                <dd className="tabular-nums">{currentGameweek.gw}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700 dark:text-slate-200">
                  Deadline
                </dt>
                <dd className="tabular-nums">
                  {formatDeadline(currentGameweek.deadline_time)}
                </dd>
              </div>
            </>
          )}
          <div className="flex gap-2">
            <dt className="font-medium text-slate-700 dark:text-slate-200">
              Source
            </dt>
            <dd>
              {ticker.source === "database" ? "Live database" : "Local seed sample"}
            </dd>
          </div>
        </dl>

        <nav
          aria-label="Horizon"
          className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400"
        >
          <span className="mr-1 font-medium uppercase tracking-wide">Horizon</span>
          {HORIZONS.map((h) => (
            <Link
              key={h}
              href={`/fixtures?horizon=${h}`}
              aria-current={h === horizon ? "page" : undefined}
              className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
                h === horizon
                  ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
            >
              {h} GW
            </Link>
          ))}
        </nav>

        {showingHistory && (
          <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
            No fixture in this dataset is scheduled at or after gameweek{" "}
            {preferredGw}. The sample checked into the repo covers gameweeks 1–
            {lastScheduled} only, all already played, so the ticker is showing
            GW{ticker.fromGw}–{ticker.toGw} instead of the next{" "}
            {horizon} gameweeks. Nothing has been invented to fill the gap — set{" "}
            <code className="font-mono">DATABASE_URL</code> and run the fixtures
            sync for the real forward schedule.
          </p>
        )}

        {noFixturesAtAll && (
          <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
            No fixtures are loaded at all. Between late May and mid-August the
            schedule does not exist yet; in development, run the fixtures sync.
          </p>
        )}

        {ticker.source === "seed" && !showingHistory && !noFixturesAtAll && (
          <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
            No database connection is configured, so this page is showing the
            checked-in fixture sample.
          </p>
        )}

        <p className="text-xs leading-5 text-slate-500 dark:text-slate-400">
          {doubleGws.length > 0 ? (
            <>
              Double gameweeks in this window:{" "}
              <strong className="font-semibold text-slate-700 dark:text-slate-200">
                GW{doubleGws.join(", GW")}
              </strong>
              .{" "}
            </>
          ) : (
            "No double gameweek falls in this window. "
          )}
          {blankGws.length > 0 ? (
            <>
              Blanks:{" "}
              <strong className="font-semibold text-slate-700 dark:text-slate-200">
                GW{blankGws.join(", GW")}
              </strong>
              .
            </>
          ) : (
            "No club blanks in this window."
          )}
        </p>
      </header>

      <FixtureTicker rows={ticker.rows} gws={ticker.gws} names={names} />

      {ticker.unscheduled.length > 0 && (
        <section className="mt-8 rounded-xl border border-slate-200 p-4 dark:border-slate-800">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Not yet scheduled
          </h2>
          <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
            {ticker.unscheduled.length} fixture
            {ticker.unscheduled.length === 1 ? " has" : "s have"} no gameweek
            assigned yet — usually a postponement waiting on a cup result. They
            are excluded from the grid above and will create a double somewhere
            once they are rescheduled.
          </p>
          <ul className="mt-2 flex flex-wrap gap-1.5 text-xs">
            {ticker.unscheduled.map((f) => (
              <li
                key={f.fixture_id}
                className="rounded border border-dashed border-slate-300 px-2 py-1 text-slate-600 dark:border-slate-700 dark:text-slate-300"
              >
                {names.short[f.team_h_fpl_id] ?? "???"} v{" "}
                {names.short[f.team_a_fpl_id] ?? "???"}
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="mt-8 text-xs leading-5 text-slate-500 dark:text-slate-400">
        Difficulty is FPL&apos;s own fixture difficulty rating, read from the
        side of the fixture the club is actually on. Club names and short codes
        are used descriptively; no crests, kits or official marks appear here.
      </p>
    </div>
  );
}
