/**
 * The Postgres read layer.
 *
 * Plain SQL over `pg`. No ORM — that is a deliberate choice recorded in
 * CLAUDE.md and ARCHITECTURE.md, and a schema this small does not earn one.
 *
 * Two rules this file exists to enforce:
 *   - The web app never calls the FPL API. Cron writes Postgres, the app reads
 *     it. Everything here reads Postgres or the checked-in seed, nothing else.
 *   - Money stays integer tenths all the way to format.ts.
 *
 * When DATABASE_URL is unset (a fresh clone, or a Supabase project whose
 * password has not been filled in yet) every function falls back to
 * lib/seed/players.json and logs once that it did. That keeps the site
 * buildable and reviewable without credentials.
 */

import { Pool, type PoolClient, type QueryResultRow } from "pg";

import seed from "@/lib/seed/players.json";
import type {
  DataSource,
  ElementType,
  Gameweek,
  Player,
  PlayerData,
  PlayerStatus,
  PlayerWithTeam,
  Team,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

const CONNECTION_STRING = process.env.DATABASE_URL?.trim();

/**
 * A connection string still holding the placeholder from .env.example counts as
 * unconfigured — better a working seed page than a confusing auth error.
 */
function isConfigured(url: string | undefined): url is string {
  if (!url) return false;
  if (/PASSWORD|PROJECT_REF|REPLACE_WITH/i.test(url)) return false;
  return url.startsWith("postgres://") || url.startsWith("postgresql://");
}

/**
 * Strip `sslmode` from the connection string.
 *
 * node-postgres builds its own TLS config from an `sslmode` in the URL, and that
 * takes precedence over the `ssl` option passed to the Pool — so a string
 * carrying `?sslmode=require` fails against Supabase with "self-signed
 * certificate in certificate chain" no matter what the Pool says. psycopg wants
 * the parameter and node-postgres cannot live with it, so the one string in .env
 * keeps it and this strips it on the way past.
 */
function withoutSslMode(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete("sslmode");
    return parsed.toString();
  } catch {
    // A string too malformed to parse is one the Pool will reject anyway, with a
    // better message than anything invented here.
    return url;
  }
}

export const SEASON = process.env.FPL_SEASON?.trim() || seed.season;

let pool: Pool | null = null;

function getPool(): Pool | null {
  if (!isConfigured(CONNECTION_STRING)) return null;
  if (!pool) {
    pool = new Pool({
      connectionString: withoutSslMode(CONNECTION_STRING),
      max: 3,
      idleTimeoutMillis: 10_000,
      connectionTimeoutMillis: 5_000,
      // Supabase terminates TLS with its own CA; the data is public reference
      // data, so verification is not the security boundary here.
      ssl: { rejectUnauthorized: false },
    });
    pool.on("error", (err) => {
      console.error("[db] idle client error:", err.message);
    });
  }
  return pool;
}

let seedNoticeLogged = false;

function fallbackToSeed(reason: string): "seed" {
  if (!seedNoticeLogged) {
    seedNoticeLogged = true;
    console.warn(
      `[db] Using seed data from src/lib/seed/players.json (${reason}). ` +
        "Set DATABASE_URL to read the real database.",
    );
  }
  return "seed";
}

async function query<T extends QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T[] | null> {
  const p = getPool();
  if (!p) {
    fallbackToSeed("DATABASE_URL is not configured");
    return null;
  }
  let client: PoolClient | undefined;
  try {
    client = await p.connect();
    const result = await client.query<T>(sql, params);
    return result.rows;
  } catch (err) {
    fallbackToSeed(
      `query failed: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  } finally {
    client?.release();
  }
}

// ---------------------------------------------------------------------------
// Row coercion
//
// pg returns `numeric` as a string to avoid silent precision loss. Integer
// columns (every *_tenths one) come back as numbers already.
// ---------------------------------------------------------------------------

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isNaN(n) ? null : n;
}

function int(value: unknown, fallback = 0): number {
  return num(value) ?? fallback;
}

function str(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
}

function iso(value: unknown): string | null {
  if (!value) return null;
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

function toPlayerWithTeam(row: Record<string, unknown>): PlayerWithTeam {
  return {
    season: String(row.season),
    element_id: int(row.element_id),
    code: int(row.code),
    team_fpl_id: int(row.team_fpl_id),
    element_type: int(row.element_type, 3) as ElementType,
    web_name: String(row.web_name),
    first_name: str(row.first_name),
    second_name: str(row.second_name),
    full_name: str(row.full_name),
    now_cost_tenths: int(row.now_cost_tenths),
    cost_change_start_tenths: int(row.cost_change_start_tenths),
    status: (str(row.status) ?? "a") as PlayerStatus,
    news: str(row.news),
    news_added: iso(row.news_added),
    chance_of_playing_this_round: num(row.chance_of_playing_this_round),
    chance_of_playing_next_round: num(row.chance_of_playing_next_round),
    minutes: int(row.minutes),
    total_points: int(row.total_points),
    form: num(row.form),
    points_per_game: num(row.points_per_game),
    selected_by_percent: num(row.selected_by_percent),
    expected_goals: num(row.expected_goals),
    expected_assists: num(row.expected_assists),
    expected_goal_involvements: num(row.expected_goal_involvements),
    expected_goals_conceded: num(row.expected_goals_conceded),
    team_name: String(row.team_name ?? ""),
    team_short_name: String(row.team_short_name ?? ""),
    team_code: int(row.team_code),
  };
}

function toTeam(row: Record<string, unknown>): Team {
  return {
    season: String(row.season),
    fpl_id: int(row.fpl_id),
    code: int(row.code),
    name: String(row.name),
    short_name: String(row.short_name),
    strength: num(row.strength),
    strength_overall_home: num(row.strength_overall_home),
    strength_overall_away: num(row.strength_overall_away),
    strength_attack_home: num(row.strength_attack_home),
    strength_attack_away: num(row.strength_attack_away),
    strength_defence_home: num(row.strength_defence_home),
    strength_defence_away: num(row.strength_defence_away),
  };
}

function toGameweek(row: Record<string, unknown>): Gameweek {
  return {
    season: String(row.season),
    gw: int(row.gw),
    name: String(row.name),
    deadline_time: iso(row.deadline_time) ?? "",
    is_current: Boolean(row.is_current),
    is_next: Boolean(row.is_next),
    is_previous: Boolean(row.is_previous),
    finished: Boolean(row.finished),
    data_checked: Boolean(row.data_checked),
    average_entry_score: num(row.average_entry_score),
    highest_score: num(row.highest_score),
  };
}

// ---------------------------------------------------------------------------
// Seed fallback
// ---------------------------------------------------------------------------

const seedTeams: Team[] = (seed.teams as unknown as Record<string, unknown>[]).map(toTeam);
const seedGameweeks: Gameweek[] = (
  seed.gameweeks as unknown as Record<string, unknown>[]
).map(toGameweek);

const seedPlayers: PlayerWithTeam[] = (
  seed.players as unknown as Record<string, unknown>[]
).map((row) => {
  const team = seedTeams.find((t) => t.fpl_id === int(row.team_fpl_id));
  return toPlayerWithTeam({
    ...row,
    team_name: team?.name ?? "Unknown",
    team_short_name: team?.short_name ?? "—",
    team_code: team?.code ?? 0,
  });
});

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

/**
 * Every player for a season, joined to their team.
 *
 * The join is players -> teams on (season, fpl_id), which is one-to-one.
 * Fixtures are the one-to-many join and deliberately are not touched here.
 */
export async function getPlayers(season: string = SEASON): Promise<PlayerWithTeam[]> {
  const rows = await query<QueryResultRow>(
    `select p.season,
            p.element_id,
            p.code,
            p.team_fpl_id,
            p.element_type,
            p.web_name,
            p.first_name,
            p.second_name,
            p.full_name,
            p.now_cost_tenths,
            p.cost_change_start_tenths,
            p.status,
            p.news,
            p.news_added,
            p.chance_of_playing_this_round,
            p.chance_of_playing_next_round,
            p.minutes,
            p.total_points,
            p.form,
            p.points_per_game,
            p.selected_by_percent,
            p.expected_goals,
            p.expected_assists,
            p.expected_goal_involvements,
            p.expected_goals_conceded,
            t.name       as team_name,
            t.short_name as team_short_name,
            t.code       as team_code
       from players p
       join teams t on t.season = p.season and t.fpl_id = p.team_fpl_id
      where p.season = $1
      order by p.total_points desc, p.web_name asc`,
    [season],
  );
  if (!rows) return seedPlayers;
  return rows.map((row) => toPlayerWithTeam(row as Record<string, unknown>));
}

/** The 20 teams for a season. */
export async function getTeams(season: string = SEASON): Promise<Team[]> {
  const rows = await query<QueryResultRow>(
    `select season,
            fpl_id,
            code,
            name,
            short_name,
            strength,
            strength_overall_home,
            strength_overall_away,
            strength_attack_home,
            strength_attack_away,
            strength_defence_home,
            strength_defence_away
       from teams
      where season = $1
      order by name asc`,
    [season],
  );
  if (!rows) return seedTeams;
  return rows.map((row) => toTeam(row as Record<string, unknown>));
}

/**
 * The gameweek the site is "in": the current one, or the next one once the
 * current gameweek has finished. Its deadline is the clock the rest of the
 * system derives from, so this is the value pages should show.
 */
export async function getCurrentGameweek(
  season: string = SEASON,
): Promise<Gameweek | null> {
  const rows = await query<QueryResultRow>(
    `select season, gw, name, deadline_time, is_current, is_next, is_previous,
            finished, data_checked, average_entry_score, highest_score
       from gameweeks
      where season = $1
        and (is_current or is_next)
      -- A finished current gameweek is history; the deadline everything derives
      -- from is then the next one.
      order by case
                 when is_current and not finished then 0
                 when is_next then 1
                 else 2
               end,
               gw asc
      limit 1`,
    [season],
  );
  if (!rows) return currentFromSeed();
  if (rows.length === 0) return null;
  return toGameweek(rows[0] as Record<string, unknown>);
}

function currentFromSeed(): Gameweek | null {
  return (
    seedGameweeks.find((g) => g.is_current && !g.finished) ??
    seedGameweeks.find((g) => g.is_next) ??
    seedGameweeks.find((g) => g.is_current) ??
    null
  );
}

/**
 * Whether a page is looking at the real database or the checked-in seed.
 * Only meaningful after the queries have run — a configured connection that
 * fails at request time also ends up on the seed.
 */
export function dataSource(): DataSource {
  return isConfigured(CONNECTION_STRING) && !seedNoticeLogged
    ? "database"
    : "seed";
}

/** Everything the player table needs, in one call. */
export async function getPlayerData(season: string = SEASON): Promise<PlayerData> {
  const [players, teams, currentGameweek] = await Promise.all([
    getPlayers(season),
    getTeams(season),
    getCurrentGameweek(season),
  ]);
  return { players, teams, currentGameweek, source: dataSource() };
}

export type { Player, PlayerWithTeam };
