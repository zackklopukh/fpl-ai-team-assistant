/**
 * The fixtures read layer.
 *
 * Same shape as lib/db.ts: plain SQL over `pg`, with lib/seed/fixtures.json as
 * the fallback when DATABASE_URL is unconfigured. No ORM, no FPL API call, and
 * money never appears here at all.
 *
 * ---------------------------------------------------------------------------
 * The one rule this file exists to enforce
 * ---------------------------------------------------------------------------
 *
 * A team plays ZERO, ONE or TWO times in a gameweek. Blanks and doubles are
 * normal, not edge cases (CLAUDE.md invariant 6; ARCHITECTURE.md, "Double and
 * blank gameweeks"). So:
 *
 *   - Every lookup keyed by (team, gameweek) returns a LIST. An empty list is a
 *     blank and is a real answer, not a missing one.
 *   - Nothing in this file is named, typed or shaped like "the fixture this
 *     gameweek". If you find yourself wanting `[0]`, you have found the bug
 *     that surfaces in December.
 *   - Aggregate difficulty sums over fixtures, so a double counts twice and a
 *     blank contributes zero — not a neutral or average value, which would make
 *     a blank look like an ordinary week.
 *
 * Fixtures with a null `gw` are not yet scheduled (a postponement waiting on a
 * cup replay). They are excluded from every gameweek-keyed view rather than
 * crashing it, and surface separately through `unscheduledFixtures`.
 *
 * `team_h_difficulty` is the FDR *for the home side* and `team_a_difficulty`
 * the FDR for the away side. Reading them off the wrong side inverts the whole
 * ticker while still looking plausible, so the swap happens in exactly one
 * place: `toTeamFixture`.
 *
 * Imports here are relative rather than "@/..." on purpose: the unit tests run
 * under vitest with no path-alias config, and another agent owns the config
 * files this round.
 */

import type { QueryResultRow } from "pg";

import { DatabaseUnavailableError, getPool, isConfigured } from "./pg";
import seed from "./seed/fixtures.json";
import type { DataSource, Team } from "./types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A row of `fixtures`, exactly as the schema stores it. */
export interface Fixture {
  season: string;
  fixture_id: number;
  /** Null until FPL assigns the match to a gameweek. */
  gw: number | null;
  team_h_fpl_id: number;
  team_a_fpl_id: number;
  /** FDR for the HOME side. */
  team_h_difficulty: number | null;
  /** FDR for the AWAY side. */
  team_a_difficulty: number | null;
  kickoff_time: string | null;
  started: boolean;
  finished: boolean;
  finished_provisional: boolean;
  minutes: number;
  team_h_score: number | null;
  team_a_score: number | null;
}

/**
 * One fixture seen from one team's side.
 *
 * `gw` is a number here, never null: a fixture with no gameweek cannot appear
 * in a per-gameweek view, so it is filtered out before this type is built.
 */
export interface TeamFixture {
  season: string;
  fixture_id: number;
  gw: number;
  /** The team this fixture is being viewed from. */
  team_fpl_id: number;
  opponent_fpl_id: number;
  is_home: boolean;
  /** FDR for `team_fpl_id`, read off the correct side of the fixture. */
  difficulty: number | null;
  kickoff_time: string | null;
  started: boolean;
  finished: boolean;
  /** Goals for `team_fpl_id`, once the match has a score. */
  goals_for: number | null;
  goals_against: number | null;
}

/**
 * What one team has in one gameweek. `fixtures` holds 0, 1 or 2 entries;
 * `isBlank` and `isDouble` are derived from its length so no caller has to
 * remember which count means what.
 */
export interface TeamGameweekFixtures {
  team_fpl_id: number;
  gw: number;
  fixtures: TeamFixture[];
  isBlank: boolean;
  isDouble: boolean;
}

/**
 * Difficulty over a window of gameweeks for one team.
 *
 * `totalDifficulty` sums every fixture in the window: a double adds both of its
 * FDRs, a blank adds nothing. That is the point of the whole file — a team with
 * two 2s is a better week than a team with one 2, and a blank is not an average
 * week, it is no week.
 */
export interface TeamDifficulty {
  team_fpl_id: number;
  fromGw: number;
  toGw: number;
  /** Number of fixtures, not of gameweeks. A 5-week window can hold 0..10. */
  fixtureCount: number;
  totalDifficulty: number;
  /** totalDifficulty / fixtureCount, or null when the window is all blanks. */
  averageDifficulty: number | null;
  /** Gameweeks in the window with no fixture at all. */
  blankGws: number[];
  /** Gameweeks in the window with two or more fixtures. */
  doubleGws: number[];
  /** Per gameweek, in ascending order, including the blanks. */
  byGameweek: TeamGameweekFixtures[];
}

export interface FixtureTickerRow {
  team: Team;
  gameweeks: TeamGameweekFixtures[];
  difficulty: TeamDifficulty;
}

export interface FixtureTickerData {
  fromGw: number;
  toGw: number;
  gws: number[];
  rows: FixtureTickerRow[];
  /** Fixtures FPL has not yet assigned to a gameweek; shown, not hidden. */
  unscheduled: Fixture[];
  source: DataSource;
}

// ---------------------------------------------------------------------------
// Connection — mirrors lib/db.ts
// ---------------------------------------------------------------------------

const CONNECTION_STRING = process.env.DATABASE_URL?.trim();

export const SEASON = process.env.FPL_SEASON?.trim() || seed.season;

let seedNoticeLogged = false;

function fallbackToSeed(reason: string): void {
  if (!seedNoticeLogged) {
    seedNoticeLogged = true;
    console.warn(
      `[fixtures] Using seed data from src/lib/seed/fixtures.json (${reason}). ` +
        "Set DATABASE_URL to read the real database.",
    );
  }
}

/**
 * Run a query on the shared pool (lib/pg.ts). Null — use the seed — only when
 * no database is configured; a configured database that fails throws. See the
 * matching note in lib/db.ts for why a silent fallback was worse than an error.
 */
async function query<T extends QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T[] | null> {
  const p = getPool();
  if (!p) {
    fallbackToSeed("DATABASE_URL is not configured");
    return null;
  }
  try {
    const result = await p.query<T>(sql, params);
    return result.rows;
  } catch (err) {
    console.error("[fixtures] query failed:", err instanceof Error ? err.message : err);
    throw new DatabaseUnavailableError(err);
  }
}

/** Whether the fixture pages are reading Postgres or the checked-in seed. */
export function fixturesSource(): DataSource {
  return isConfigured(CONNECTION_STRING) && !seedNoticeLogged
    ? "database"
    : "seed";
}

// ---------------------------------------------------------------------------
// Row coercion
// ---------------------------------------------------------------------------

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isNaN(n) ? null : n;
}

function int(value: unknown, fallback = 0): number {
  return num(value) ?? fallback;
}

function iso(value: unknown): string | null {
  if (!value) return null;
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

export function toFixture(row: Record<string, unknown>): Fixture {
  return {
    season: String(row.season),
    fixture_id: int(row.fixture_id),
    // Deliberately nullable: an unscheduled fixture keeps a null gw rather than
    // being coerced to 0, which would file it under a gameweek that exists.
    gw: num(row.gw),
    team_h_fpl_id: int(row.team_h_fpl_id),
    team_a_fpl_id: int(row.team_a_fpl_id),
    team_h_difficulty: num(row.team_h_difficulty),
    team_a_difficulty: num(row.team_a_difficulty),
    kickoff_time: iso(row.kickoff_time),
    started: Boolean(row.started),
    finished: Boolean(row.finished),
    finished_provisional: Boolean(row.finished_provisional),
    minutes: int(row.minutes),
    team_h_score: num(row.team_h_score),
    team_a_score: num(row.team_a_score),
  };
}

const seedFixtures: Fixture[] = (
  seed.fixtures as unknown as Record<string, unknown>[]
).map(toFixture);

/** The seed sample, for pages that need to explain what they are showing. */
export function seedFixtureSample(): Fixture[] {
  return seedFixtures;
}

// ---------------------------------------------------------------------------
// Pure transforms — the part worth testing
// ---------------------------------------------------------------------------

/** True when the fixture is scheduled into a gameweek at all. */
export function isScheduled(
  fixture: Fixture,
): fixture is Fixture & { gw: number } {
  return typeof fixture.gw === "number" && Number.isFinite(fixture.gw);
}

/**
 * One fixture, viewed from one team's side.
 *
 * Returns null when the team is not in this fixture, or when the fixture has no
 * gameweek. Both are ordinary answers, not errors.
 */
export function toTeamFixture(
  fixture: Fixture,
  teamFplId: number,
): TeamFixture | null {
  if (!isScheduled(fixture)) return null;
  const isHome = fixture.team_h_fpl_id === teamFplId;
  const isAway = fixture.team_a_fpl_id === teamFplId;
  if (!isHome && !isAway) return null;

  return {
    season: fixture.season,
    fixture_id: fixture.fixture_id,
    gw: fixture.gw,
    team_fpl_id: teamFplId,
    opponent_fpl_id: isHome ? fixture.team_a_fpl_id : fixture.team_h_fpl_id,
    is_home: isHome,
    // The side swap. team_h_difficulty is how hard the fixture is FOR THE HOME
    // TEAM, so an away view must read team_a_difficulty.
    difficulty: isHome ? fixture.team_h_difficulty : fixture.team_a_difficulty,
    kickoff_time: fixture.kickoff_time,
    started: fixture.started,
    finished: fixture.finished,
    goals_for: isHome ? fixture.team_h_score : fixture.team_a_score,
    goals_against: isHome ? fixture.team_a_score : fixture.team_h_score,
  };
}

/** Inclusive gameweek range as an array. Empty when the range is inverted. */
export function gameweekRange(fromGw: number, toGw: number): number[] {
  const gws: number[] = [];
  for (let gw = fromGw; gw <= toGw; gw += 1) gws.push(gw);
  return gws;
}

function byKickoff(a: TeamFixture, b: TeamFixture): number {
  const at = a.kickoff_time ?? "";
  const bt = b.kickoff_time ?? "";
  if (at !== bt) return at < bt ? -1 : 1;
  return a.fixture_id - b.fixture_id;
}

/**
 * Every fixture one team has in each gameweek of a window.
 *
 * Returns one entry per gameweek in the window — including the ones the team
 * does not play in, whose `fixtures` array is empty. A caller iterating this
 * cannot silently skip a blank, which is the whole reason it is shaped this way
 * rather than as a sparse map.
 */
export function teamFixturesByGameweek(
  fixtures: Fixture[],
  teamFplId: number,
  fromGw: number,
  toGw: number,
): TeamGameweekFixtures[] {
  const perGw = new Map<number, TeamFixture[]>();
  for (const gw of gameweekRange(fromGw, toGw)) perGw.set(gw, []);

  for (const fixture of fixtures) {
    const view = toTeamFixture(fixture, teamFplId);
    if (!view) continue;
    const bucket = perGw.get(view.gw);
    // A fixture outside the window is not an error, it is just not asked for.
    if (!bucket) continue;
    bucket.push(view);
  }

  return gameweekRange(fromGw, toGw).map((gw) => {
    const list = (perGw.get(gw) ?? []).sort(byKickoff);
    return {
      team_fpl_id: teamFplId,
      gw,
      fixtures: list,
      isBlank: list.length === 0,
      isDouble: list.length > 1,
    };
  });
}

/**
 * Aggregate difficulty for one team over a window.
 *
 * Sum, not mean, over fixtures. A double gameweek contributes both fixtures and
 * a blank contributes zero — which means a blank-heavy run scores *low*, and so
 * the average is reported alongside it for the comparison that wants to be
 * per-fixture rather than per-window.
 *
 * A fixture whose FDR is null contributes 0 to the total but still counts as a
 * fixture; FPL has always populated FDR, and inventing a value would be worse
 * than a slightly low sum.
 */
export function teamDifficulty(
  fixtures: Fixture[],
  teamFplId: number,
  fromGw: number,
  toGw: number,
): TeamDifficulty {
  const byGameweek = teamFixturesByGameweek(fixtures, teamFplId, fromGw, toGw);

  let fixtureCount = 0;
  let totalDifficulty = 0;
  const blankGws: number[] = [];
  const doubleGws: number[] = [];

  for (const week of byGameweek) {
    if (week.isBlank) blankGws.push(week.gw);
    if (week.isDouble) doubleGws.push(week.gw);
    for (const f of week.fixtures) {
      fixtureCount += 1;
      totalDifficulty += f.difficulty ?? 0;
    }
  }

  return {
    team_fpl_id: teamFplId,
    fromGw,
    toGw,
    fixtureCount,
    totalDifficulty,
    averageDifficulty: fixtureCount === 0 ? null : totalDifficulty / fixtureCount,
    blankGws,
    doubleGws,
    byGameweek,
  };
}

/** Fixtures FPL has not yet assigned to a gameweek. */
export function unscheduledFixtures(fixtures: Fixture[]): Fixture[] {
  return fixtures.filter((f) => !isScheduled(f));
}

/**
 * The first gameweek at or after `preferredGw` that any fixture is scheduled
 * in, or null when none is.
 *
 * The seed sample stops at gameweek 4, so a page asking for "the next five
 * gameweeks" would otherwise render twenty blank rows and look broken rather
 * than empty. Pages use this to fall back to a window that has data, and say so.
 */
export function firstScheduledGameweek(
  fixtures: Fixture[],
  preferredGw: number,
): number | null {
  let best: number | null = null;
  for (const f of fixtures) {
    if (!isScheduled(f)) continue;
    if (f.gw < preferredGw) continue;
    if (best === null || f.gw < best) best = f.gw;
  }
  return best;
}

/** The last gameweek any fixture is scheduled in, or null when none is. */
export function lastScheduledGameweek(fixtures: Fixture[]): number | null {
  let best: number | null = null;
  for (const f of fixtures) {
    if (!isScheduled(f)) continue;
    if (best === null || f.gw > best) best = f.gw;
  }
  return best;
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

const FIXTURE_COLUMNS = `season,
            fixture_id,
            gw,
            team_h_fpl_id,
            team_a_fpl_id,
            team_h_difficulty,
            team_a_difficulty,
            kickoff_time,
            started,
            finished,
            finished_provisional,
            minutes,
            team_h_score,
            team_a_score`;

/**
 * Every fixture in a gameweek range, both sides, unsplit.
 *
 * Returns rows, not a per-team map: a fixture belongs to two teams and
 * flattening it here would double-count it. The per-team split happens in
 * `teamFixturesByGameweek`.
 *
 * Unscheduled fixtures (null gw) fall out of the range predicate in SQL and out
 * of `isScheduled` in the seed path, so both paths agree.
 */
export async function getFixturesInRange(
  fromGw: number,
  toGw: number,
  season: string = SEASON,
): Promise<Fixture[]> {
  const rows = await query<QueryResultRow>(
    `select ${FIXTURE_COLUMNS}
       from fixtures
      where season = $1
        and gw is not null
        and gw between $2 and $3
      order by gw asc, kickoff_time asc nulls last, fixture_id asc`,
    [season, fromGw, toGw],
  );
  if (!rows) {
    return seedFixtures.filter(
      (f) => f.season === season && isScheduled(f) && f.gw >= fromGw && f.gw <= toGw,
    );
  }
  return rows.map((row) => toFixture(row as Record<string, unknown>));
}

/** Every fixture for a season, scheduled or not. */
export async function getAllFixtures(season: string = SEASON): Promise<Fixture[]> {
  const rows = await query<QueryResultRow>(
    `select ${FIXTURE_COLUMNS}
       from fixtures
      where season = $1
      order by gw asc nulls last, kickoff_time asc nulls last, fixture_id asc`,
    [season],
  );
  if (!rows) return seedFixtures.filter((f) => f.season === season);
  return rows.map((row) => toFixture(row as Record<string, unknown>));
}

/**
 * One team's fixtures over a horizon, one entry per gameweek including blanks.
 *
 * The SQL matches the team on either side, which is what makes a double return
 * two rows rather than one.
 */
export async function getTeamFixtures(
  teamFplId: number,
  fromGw: number,
  toGw: number,
  season: string = SEASON,
): Promise<TeamGameweekFixtures[]> {
  const rows = await query<QueryResultRow>(
    `select ${FIXTURE_COLUMNS}
       from fixtures
      where season = $1
        and gw is not null
        and gw between $2 and $3
        and (team_h_fpl_id = $4 or team_a_fpl_id = $4)
      order by gw asc, kickoff_time asc nulls last, fixture_id asc`,
    [season, fromGw, toGw, teamFplId],
  );
  const fixtures = rows
    ? rows.map((row) => toFixture(row as Record<string, unknown>))
    : seedFixtures.filter((f) => f.season === season);
  return teamFixturesByGameweek(fixtures, teamFplId, fromGw, toGw);
}

// ---------------------------------------------------------------------------
// player_gw_stats
//
// PARKED HERE ON PURPOSE. This is not fixture data and it belongs in lib/db.ts
// beside the other player reads; it lives here only because db.ts is owned by
// another agent this round. Move it when that lands — the export name and shape
// are meant to survive the move unchanged.
// ---------------------------------------------------------------------------

/**
 * A row of `player_gw_stats`, one per player per fixture.
 *
 * A double gameweek produces TWO rows for one `gw`, and a blank produces none
 * at all — the primary key includes fixture_id for exactly that reason. So this
 * is returned as a flat list and grouped by gameweek for display, never keyed
 * by gameweek alone.
 */
export interface PlayerGameweekStat {
  season: string;
  element_id: number;
  gw: number;
  fixture_id: number;
  minutes: number;
  total_points: number;
  starts: number;
  goals_scored: number;
  assists: number;
  clean_sheets: number;
  goals_conceded: number;
  yellow_cards: number;
  red_cards: number;
  saves: number;
  bonus: number;
  bps: number;
  defensive_contribution: number;
  expected_goals: number | null;
  expected_assists: number | null;
  expected_goal_involvements: number | null;
  expected_goals_conceded: number | null;
  was_home: boolean | null;
  opponent_team_fpl_id: number | null;
  /** The player's price during that gameweek, not today's. */
  value_tenths: number | null;
  bonus_settled: boolean;
}

function toPlayerGameweekStat(row: Record<string, unknown>): PlayerGameweekStat {
  return {
    season: String(row.season),
    element_id: int(row.element_id),
    gw: int(row.gw),
    fixture_id: int(row.fixture_id),
    minutes: int(row.minutes),
    total_points: int(row.total_points),
    starts: int(row.starts),
    goals_scored: int(row.goals_scored),
    assists: int(row.assists),
    clean_sheets: int(row.clean_sheets),
    goals_conceded: int(row.goals_conceded),
    yellow_cards: int(row.yellow_cards),
    red_cards: int(row.red_cards),
    saves: int(row.saves),
    bonus: int(row.bonus),
    bps: int(row.bps),
    defensive_contribution: int(row.defensive_contribution),
    expected_goals: num(row.expected_goals),
    expected_assists: num(row.expected_assists),
    expected_goal_involvements: num(row.expected_goal_involvements),
    expected_goals_conceded: num(row.expected_goals_conceded),
    was_home: row.was_home === null || row.was_home === undefined
      ? null
      : Boolean(row.was_home),
    opponent_team_fpl_id: num(row.opponent_team_fpl_id),
    value_tenths: num(row.value_tenths),
    bonus_settled: Boolean(row.bonus_settled),
  };
}

/**
 * Per-gameweek history for one player, keyed by (season, element_id) because
 * element ids are reassigned between seasons.
 *
 * Returns an empty list when there is no history — including on the seed path,
 * which has no `player_gw_stats` sample at all. Pages say so rather than
 * rendering an empty table with no explanation.
 */
export async function getPlayerGameweekStats(
  elementId: number,
  season: string = SEASON,
): Promise<PlayerGameweekStat[]> {
  const rows = await query<QueryResultRow>(
    `select season, element_id, gw, fixture_id, minutes, total_points, starts,
            goals_scored, assists, clean_sheets, goals_conceded, yellow_cards,
            red_cards, saves, bonus, bps, defensive_contribution,
            expected_goals, expected_assists, expected_goal_involvements,
            expected_goals_conceded, was_home, opponent_team_fpl_id,
            value_tenths, bonus_settled
       from player_gw_stats
      where season = $1
        and element_id = $2
      order by gw asc, fixture_id asc`,
    [season, elementId],
  );
  if (!rows) return [];
  return rows.map((row) => toPlayerGameweekStat(row as Record<string, unknown>));
}

/**
 * History grouped by gameweek, preserving the one-to-many shape: the value is a
 * list, so a double gameweek shows both matches and a gameweek the player has
 * no row for simply does not appear.
 */
export function groupStatsByGameweek(
  stats: PlayerGameweekStat[],
): { gw: number; stats: PlayerGameweekStat[] }[] {
  const byGw = new Map<number, PlayerGameweekStat[]>();
  for (const stat of stats) {
    const bucket = byGw.get(stat.gw);
    if (bucket) bucket.push(stat);
    else byGw.set(stat.gw, [stat]);
  }
  return [...byGw.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([gw, list]) => ({ gw, stats: list }));
}

/** Aggregate difficulty for one team over the next `horizon` gameweeks. */
export async function getTeamDifficulty(
  teamFplId: number,
  fromGw: number,
  horizon: number,
  season: string = SEASON,
): Promise<TeamDifficulty> {
  const toGw = fromGw + horizon - 1;
  const fixtures = await getFixturesInRange(fromGw, toGw, season);
  return teamDifficulty(fixtures, teamFplId, fromGw, toGw);
}

/**
 * Everything the ticker renders: one row per team, one cell per gameweek, each
 * cell a list.
 *
 * `teams` is passed in rather than queried here so the caller keeps one source
 * of truth for the team list (lib/db.ts owns `teams`).
 */
export async function getFixtureTicker(
  teams: Team[],
  fromGw: number,
  horizon: number,
  season: string = SEASON,
): Promise<FixtureTickerData> {
  const toGw = fromGw + horizon - 1;
  const [inRange, all] = await Promise.all([
    getFixturesInRange(fromGw, toGw, season),
    getAllFixtures(season),
  ]);

  const rows: FixtureTickerRow[] = teams.map((team) => {
    const difficulty = teamDifficulty(inRange, team.fpl_id, fromGw, toGw);
    return { team, gameweeks: difficulty.byGameweek, difficulty };
  });

  return {
    fromGw,
    toGw,
    gws: gameweekRange(fromGw, toGw),
    rows,
    unscheduled: unscheduledFixtures(all),
    source: fixturesSource(),
  };
}
