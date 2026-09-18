/**
 * /players/[elementId] — one player.
 *
 * Server component. The route segment is the element id alone because a URL
 * only ever addresses the current season; the LOOKUP is still keyed by
 * (season, element_id), which is the invariant that matters — element ids are
 * reassigned between seasons, so the season comes from SEASON, never from the
 * absence of one.
 *
 * Prices stay integer tenths until format.ts turns them into a string.
 *
 * The fixture section is a list per gameweek, not "the next fixture": a blank
 * renders as an empty week and a double renders as two.
 */

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import PlayerHistory from "@/components/PlayerHistory";
import { getCurrentGameweek, getPlayers, getTeams } from "@/lib/db";
import {
  firstScheduledGameweek,
  getAllFixtures,
  getPlayerGameweekStats,
  lastScheduledGameweek,
  teamDifficulty,
  type TeamFixture,
} from "@/lib/fixtures";
import {
  formatNumber,
  formatNews,
  formatPercent,
  formatPrice,
  formatPriceChange,
  formatStatus,
  positionName,
} from "@/lib/format";
import type { PlayerWithTeam } from "@/lib/types";

const HORIZON = 5;

function parseElementId(raw: string): number | null {
  if (!/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

async function findPlayer(raw: string): Promise<PlayerWithTeam | null> {
  const elementId = parseElementId(raw);
  if (elementId === null) return null;
  const players = await getPlayers();
  return players.find((p) => p.element_id === elementId) ?? null;
}

export async function generateMetadata(
  props: PageProps<"/players/[elementId]">,
): Promise<Metadata> {
  const { elementId } = await props.params;
  const player = await findPlayer(elementId);
  if (!player) return { title: "Player not found" };
  return {
    title: player.web_name,
    description: `${player.full_name?.trim() || player.web_name} — ${positionName(
      player.element_type,
    )} for ${player.team_name}. Price, form, expected goals and upcoming fixtures.`,
  };
}

const TONE_CLASSES: Record<string, string> = {
  ok: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900/60 dark:bg-emerald-950/40 dark:text-emerald-200",
  warn: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200",
  bad: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-200",
};

const FDR_CHIP: Record<number, string> = {
  1: "border-emerald-600/40 bg-emerald-100 text-emerald-950 dark:border-emerald-400/30 dark:bg-emerald-950/70 dark:text-emerald-100",
  2: "border-lime-600/40 bg-lime-100 text-lime-950 dark:border-lime-400/30 dark:bg-lime-950/70 dark:text-lime-100",
  3: "border-slate-400/50 bg-slate-100 text-slate-900 dark:border-slate-500/40 dark:bg-slate-800 dark:text-slate-100",
  4: "border-orange-600/40 bg-orange-100 text-orange-950 dark:border-orange-400/30 dark:bg-orange-950/70 dark:text-orange-100",
  5: "border-rose-700/40 bg-rose-100 text-rose-950 dark:border-rose-400/30 dark:bg-rose-950/70 dark:text-rose-100",
};

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
      <dt
        className="text-[0.65rem] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400"
        title={hint}
      >
        {label}
      </dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-100">
        {value}
      </dd>
    </div>
  );
}

function FixtureChip({
  fixture,
  shortNames,
  fullNames,
}: {
  fixture: TeamFixture;
  shortNames: Record<number, string>;
  fullNames: Record<number, string>;
}) {
  const fdr = fixture.difficulty;
  const chip =
    fdr === null
      ? "border-slate-300 bg-white text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
      : FDR_CHIP[fdr] ?? "";
  const venueWord = fixture.is_home ? "at home to" : "away at";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-1 text-xs font-medium tabular-nums ${chip}`}
      title={`${venueWord} ${fullNames[fixture.opponent_fpl_id] ?? "Unknown"} — difficulty ${fdr ?? "unrated"}`}
    >
      <span className="sr-only">{venueWord} </span>
      <span>{shortNames[fixture.opponent_fpl_id] ?? "???"}</span>
      <span
        aria-hidden="true"
        className="rounded-sm bg-black/10 px-1 text-[0.65rem] font-semibold dark:bg-white/15"
      >
        {fixture.is_home ? "H" : "A"}
      </span>
      <span className="sr-only">difficulty </span>
      <span className="font-semibold">{fdr ?? "?"}</span>
    </span>
  );
}

export default async function PlayerPage(props: PageProps<"/players/[elementId]">) {
  const { elementId } = await props.params;
  const player = await findPlayer(elementId);
  // A bad id is a 404, not an empty page pretending the player exists.
  if (!player) notFound();

  const [teams, currentGameweek, allFixtures, history] = await Promise.all([
    getTeams(),
    getCurrentGameweek(),
    getAllFixtures(),
    getPlayerGameweekStats(player.element_id),
  ]);

  const shortNames = Object.fromEntries(teams.map((t) => [t.fpl_id, t.short_name]));
  const fullNames = Object.fromEntries(teams.map((t) => [t.fpl_id, t.name]));

  const preferredGw = currentGameweek?.gw ?? 1;
  const nextScheduled = firstScheduledGameweek(allFixtures, preferredGw);
  const lastScheduled = lastScheduledGameweek(allFixtures);
  const fromGw =
    nextScheduled ??
    (lastScheduled === null ? preferredGw : Math.max(1, lastScheduled - HORIZON + 1));
  const showingHistoricFixtures = nextScheduled === null && lastScheduled !== null;
  // Trim the window to the last gameweek with fixtures in the fallback case —
  // see the same note on /fixtures.
  const effectiveHorizon = showingHistoricFixtures
    ? Math.max(1, Math.min(HORIZON, (lastScheduled as number) - fromGw + 1))
    : HORIZON;

  const run = teamDifficulty(
    allFixtures,
    player.team_fpl_id,
    fromGw,
    fromGw + effectiveHorizon - 1,
  );

  const status = formatStatus(player);
  const news = formatNews(player.news);
  const startPriceTenths = player.now_cost_tenths - player.cost_change_start_tenths;

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 lg:px-8">
      <Link
        href="/players"
        className="text-xs font-medium text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
      >
        ← All players
      </Link>

      <header className="mt-3 flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50 sm:text-4xl">
          {player.web_name}
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {player.full_name?.trim() || player.web_name} ·{" "}
          {positionName(player.element_type)} ·{" "}
          <span title={player.team_name}>{player.team_name}</span>{" "}
          <span className="font-mono text-xs text-slate-500 dark:text-slate-400">
            ({player.team_short_name})
          </span>
        </p>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Season {player.season} · element id {player.element_id} — the key is
          (season, element id); element ids are reassigned each summer.
        </p>
      </header>

      {/* ---------------------------------------------------------------- */}
      {/* Price and availability                                            */}
      {/* ---------------------------------------------------------------- */}
      <section className="mt-6 grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
          <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Price
          </h2>
          <p className="mt-1 flex items-baseline gap-2">
            <span className="text-3xl font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              {formatPrice(player.now_cost_tenths)}
            </span>
            <span
              className={`text-sm font-medium tabular-nums ${
                player.cost_change_start_tenths > 0
                  ? "text-emerald-700 dark:text-emerald-400"
                  : player.cost_change_start_tenths < 0
                    ? "text-rose-700 dark:text-rose-400"
                    : "text-slate-500 dark:text-slate-400"
              }`}
            >
              {formatPriceChange(player.cost_change_start_tenths)} since season
              start
            </span>
          </p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Started the season at {formatPrice(startPriceTenths)}. Selling price
            depends on what you paid — FPL returns only half of a rise, rounded
            down.
          </p>
        </div>

        <div
          className={`rounded-xl border p-4 ${TONE_CLASSES[status.tone] ?? TONE_CLASSES.warn}`}
        >
          <h2 className="text-xs font-medium uppercase tracking-wide opacity-80">
            Availability
          </h2>
          <p className="mt-1 text-xl font-semibold">{status.short}</p>
          <p className="mt-1 text-xs leading-5">{news ?? status.long}</p>
          {player.chance_of_playing_next_round !== null && (
            <p className="mt-1 text-xs leading-5 opacity-90">
              Next round: {player.chance_of_playing_next_round}% chance of
              playing.
            </p>
          )}
        </div>
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Season to date                                                     */}
      {/* ---------------------------------------------------------------- */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          Season to date
        </h2>
        <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
          These are current values as of the last sync. They describe today, and
          are never used as model features for a past gameweek.
        </p>
        <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          <Stat label="Points" value={String(player.total_points)} />
          <Stat label="Minutes" value={String(player.minutes)} />
          <Stat
            label="Form"
            value={formatNumber(player.form)}
            hint="Points per match over the last 30 days"
          />
          <Stat label="Points / game" value={formatNumber(player.points_per_game)} />
          <Stat
            label="Owned"
            value={formatPercent(player.selected_by_percent)}
            hint="Share of managers selecting this player"
          />
          <Stat
            label="xG"
            value={formatNumber(player.expected_goals, 2)}
            hint="Expected goals, season to date"
          />
          <Stat
            label="xA"
            value={formatNumber(player.expected_assists, 2)}
            hint="Expected assists, season to date"
          />
          <Stat
            label="xGI"
            value={formatNumber(player.expected_goal_involvements, 2)}
            hint="Expected goal involvements: xG + xA"
          />
          <Stat
            label="xGC"
            value={formatNumber(player.expected_goals_conceded, 2)}
            hint="Expected goals conceded while on the pitch"
          />
        </dl>
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Fixtures                                                           */}
      {/* ---------------------------------------------------------------- */}
      <section className="mt-8">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
            {player.team_short_name} fixtures, GW{run.fromGw}–{run.toGw}
          </h2>
          <Link
            href="/fixtures"
            className="text-xs font-medium text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
          >
            Full ticker →
          </Link>
        </div>

        <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
          {run.fixtureCount} fixture{run.fixtureCount === 1 ? "" : "s"} over{" "}
          {run.toGw - run.fromGw + 1} gameweeks · total difficulty{" "}
          <strong className="font-semibold text-slate-700 dark:text-slate-200">
            {run.totalDifficulty}
          </strong>
          {run.averageDifficulty !== null && (
            <> · {formatNumber(run.averageDifficulty, 2)} per fixture</>
          )}
          {run.doubleGws.length > 0 && (
            <> · double in GW{run.doubleGws.join(", GW")}</>
          )}
          {run.blankGws.length > 0 && <> · blank in GW{run.blankGws.join(", GW")}</>}
        </p>

        {showingHistoricFixtures && (
          <p className="mt-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
            The checked-in fixture sample stops at gameweek {lastScheduled}, all
            already played, so these are the last gameweeks with data rather than
            the next five. No fixtures have been invented to fill the gap.
          </p>
        )}

        <ul className="mt-3 flex flex-wrap gap-2">
          {run.byGameweek.map((week) => (
            <li
              key={week.gw}
              className="flex min-w-[5rem] flex-col gap-1 rounded-lg border border-slate-200 p-2 dark:border-slate-800"
            >
              <span className="text-[0.65rem] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                GW{week.gw}
                {week.isDouble && (
                  <span className="ml-1 text-indigo-700 dark:text-indigo-300">
                    ×{week.fixtures.length}
                  </span>
                )}
              </span>
              {week.isBlank ? (
                <span
                  className="inline-flex items-center justify-center gap-1 rounded border border-dashed border-slate-300 px-1.5 py-1 text-xs text-slate-400 dark:border-slate-700 dark:text-slate-500"
                  title="Blank gameweek — this club does not play"
                >
                  <span aria-hidden="true">—</span>
                  <span className="sr-only">Blank gameweek, no fixture</span>
                </span>
              ) : (
                week.fixtures.map((f) => (
                  <FixtureChip
                    key={f.fixture_id}
                    fixture={f}
                    shortNames={shortNames}
                    fullNames={fullNames}
                  />
                ))
              )}
            </li>
          ))}
        </ul>
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Per-gameweek history                                               */}
      {/* ---------------------------------------------------------------- */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          Gameweek history
        </h2>
        <p className="mt-1 mb-3 text-xs leading-5 text-slate-500 dark:text-slate-400">
          One row per fixture, from <code className="font-mono">player_gw_stats</code>
          . A double gameweek is two rows under one gameweek number; a blank is no
          row at all.
        </p>
        <PlayerHistory
          stats={history}
          teamShortNames={shortNames}
          emptyNote="This site is currently running on the checked-in seed sample, which contains players, teams, gameweeks and fixtures but no player_gw_stats rows — so there is no per-gameweek history to show. Set DATABASE_URL and run the live/backfill ingestion to populate it."
        />
      </section>

      <p className="mt-8 text-xs leading-5 text-slate-500 dark:text-slate-400">
        Club names and short codes are used descriptively. No crests, kits or
        official marks appear on this site.
      </p>
    </div>
  );
}
