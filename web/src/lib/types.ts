/**
 * Row types mirroring db/schema.sql.
 *
 * Two conventions carry over from the schema and must survive into the UI:
 *   - Money is integer tenths. 55 means £5.5m. Every money field is named
 *     `*_tenths` so nobody divides by ten twice. Tenths become a string in
 *     exactly one place: lib/format.ts.
 *   - Players are keyed by (season, element_id). Element ids are reassigned
 *     between seasons, so a bare element_id is never a key.
 */

/** `players.element_type`: 1 GKP, 2 DEF, 3 MID, 4 FWD. */
export type ElementType = 1 | 2 | 3 | 4;

/** `players.status`, one char. */
export type PlayerStatus =
  | "a" // available
  | "d" // doubtful
  | "i" // injured
  | "s" // suspended
  | "u" // unavailable
  | "n"; // on loan / not in squad

/** A row of `teams`. */
export interface Team {
  season: string;
  /** bootstrap `teams[].id`, 1-20, reassigned each season. */
  fpl_id: number;
  /** Stable across seasons, unlike fpl_id. */
  code: number;
  name: string;
  short_name: string;
  strength: number | null;
  strength_overall_home: number | null;
  strength_overall_away: number | null;
  strength_attack_home: number | null;
  strength_attack_away: number | null;
  strength_defence_home: number | null;
  strength_defence_away: number | null;
}

/** A row of `gameweeks`. `deadline_time` is an ISO-8601 UTC string. */
export interface Gameweek {
  season: string;
  gw: number;
  name: string;
  deadline_time: string;
  is_current: boolean;
  is_next: boolean;
  is_previous: boolean;
  finished: boolean;
  /** True once bonus points are settled. */
  data_checked: boolean;
  average_entry_score: number | null;
  highest_score: number | null;
}

/**
 * A row of `players`.
 *
 * The current-value fields (now_cost_tenths, form, selected_by_percent and the
 * season-to-date expected_* numbers) describe today, as of the last sync. They
 * are fine to display and must never be used as model features for a past
 * gameweek — that is the lookahead leak called out in CLAUDE.md.
 */
export interface Player {
  season: string;
  element_id: number;
  /** Stable across seasons. */
  code: number;
  team_fpl_id: number;
  element_type: ElementType;
  web_name: string;
  first_name: string | null;
  second_name: string | null;
  /** Generated column in Postgres: first + second name. */
  full_name: string | null;

  now_cost_tenths: number;
  /** now_cost - start_cost, also in tenths. */
  cost_change_start_tenths: number;

  status: PlayerStatus;
  news: string | null;
  news_added: string | null;
  chance_of_playing_this_round: number | null;
  chance_of_playing_next_round: number | null;

  minutes: number;
  total_points: number;
  form: number | null;
  points_per_game: number | null;
  selected_by_percent: number | null;
  expected_goals: number | null;
  expected_assists: number | null;
  expected_goal_involvements: number | null;
  expected_goals_conceded: number | null;
}

/** A row of `xpoints`. Model output, precomputed; never rewritten in place. */
export interface XPoints {
  season: string;
  element_id: number;
  gw: number;
  model_version: string;
  xp: number;
  /** Expected minutes — the dominant term. */
  xmins: number | null;
  p_start: number | null;
  p_play: number | null;
  /** Per-term breakdown, for showing reasoning. */
  components: Record<string, unknown> | null;
  computed_at: string;
}

/**
 * A player joined to their team, which is what every table and picker renders.
 * Kept flat deliberately: the join is one-to-one, unlike fixtures.
 */
export interface PlayerWithTeam extends Player {
  team_name: string;
  team_short_name: string;
  team_code: number;
}

/** Where a page's data came from, so the UI can say so honestly. */
export type DataSource = "database" | "seed";

export interface PlayerData {
  players: PlayerWithTeam[];
  teams: Team[];
  currentGameweek: Gameweek | null;
  source: DataSource;
}
