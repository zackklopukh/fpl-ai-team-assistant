-- FPL optimizer — reference schema.
--
-- Everything here is cron-owned reference data. The web app only ever reads it.
-- No table holds anything about an identifiable user (see CLAUDE.md, invariant 4).
--
-- Two conventions run through the whole file:
--   * Money is integer tenths. 55 means £5.5m. Columns are suffixed _tenths.
--   * Players are keyed by (season, element_id). FPL reassigns element ids each
--     season, so a bare element_id silently merges two different players.
--
-- Safe to re-run: every object is created if-not-exists and every write in
-- ingest/ is an upsert on the natural key.

create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- teams
-- ---------------------------------------------------------------------------

create table if not exists teams (
    season              text    not null,
    fpl_id              int     not null,          -- bootstrap teams[].id, 1-20, reassigned each season
    code                int     not null,          -- stable across seasons, unlike fpl_id
    name                text    not null,
    short_name          text    not null,
    strength            int,
    strength_overall_home int,
    strength_overall_away int,
    strength_attack_home  int,
    strength_attack_away  int,
    strength_defence_home int,
    strength_defence_away int,
    updated_at          timestamptz not null default now(),
    primary key (season, fpl_id)
);

-- ---------------------------------------------------------------------------
-- gameweeks
--
-- deadline_time is the clock the entire system derives from: when picks become
-- public, when a cached recommendation goes stale, when traffic arrives.
-- ---------------------------------------------------------------------------

create table if not exists gameweeks (
    season              text    not null,
    gw                  int     not null,          -- bootstrap events[].id
    name                text    not null,
    deadline_time       timestamptz not null,
    is_current          boolean not null default false,
    is_next             boolean not null default false,
    is_previous         boolean not null default false,
    finished            boolean not null default false,
    data_checked        boolean not null default false,  -- true once bonus is settled
    average_entry_score int,
    highest_score       int,
    updated_at          timestamptz not null default now(),
    primary key (season, gw)
);

-- ---------------------------------------------------------------------------
-- players
--
-- Current-value columns (now_cost_tenths, form, selected_by_percent, and the
-- season-to-date expected_* fields) are as-of the last sync. They describe today
-- and must never be used as features when predicting a past gameweek — that is
-- the lookahead leak called out in ARCHITECTURE.md. Train from player_gw_stats,
-- which is snapshotted per gameweek.
-- ---------------------------------------------------------------------------

create table if not exists players (
    season              text    not null,
    element_id          int     not null,          -- bootstrap elements[].id
    code                int     not null,          -- stable across seasons
    team_fpl_id         int     not null,
    element_type        int     not null,          -- 1 GKP, 2 DEF, 3 MID, 4 FWD
    web_name            text    not null,
    first_name          text,
    second_name         text,
    full_name           text generated always as (
        coalesce(first_name, '') || ' ' || coalesce(second_name, '')
    ) stored,

    now_cost_tenths     int     not null,
    cost_change_start_tenths int not null default 0,  -- now_cost - start_cost

    status              char(1) not null default 'a',  -- a available, d doubtful, i injured, s suspended, u unavailable, n on loan
    news                text,
    news_added          timestamptz,
    chance_of_playing_this_round int,
    chance_of_playing_next_round int,

    -- Season-to-date aggregates. Current values — see the note above.
    minutes             int     not null default 0,
    total_points        int     not null default 0,
    form                numeric(6,2),
    points_per_game     numeric(6,2),
    selected_by_percent numeric(6,2),
    expected_goals      numeric(8,2),
    expected_assists    numeric(8,2),
    expected_goal_involvements numeric(8,2),
    expected_goals_conceded    numeric(8,2),

    updated_at          timestamptz not null default now(),
    primary key (season, element_id),
    foreign key (season, team_fpl_id) references teams (season, fpl_id)
);

-- Fuzzy name lookup for screenshot matching. pg_trgm handles the OCR misreads
-- ("Sak4" -> "Saka") that an exact or prefix match would drop on the floor.
create index if not exists players_web_name_trgm on players using gin (web_name gin_trgm_ops);
create index if not exists players_full_name_trgm on players using gin (full_name gin_trgm_ops);
create index if not exists players_season_type_cost on players (season, element_type, now_cost_tenths);

-- ---------------------------------------------------------------------------
-- fixtures
--
-- A team plays zero, one or two times in a gameweek. Double gameweeks and blanks
-- are normal. Every query that joins players to fixtures must treat this as
-- one-to-many — "the fixture this gameweek" is a bug waiting for December.
-- Unscheduled fixtures have a null gw until FPL assigns one.
-- ---------------------------------------------------------------------------

create table if not exists fixtures (
    season              text    not null,
    fixture_id          int     not null,          -- fixtures[].id
    gw                  int,                       -- null when not yet scheduled
    team_h_fpl_id       int     not null,
    team_a_fpl_id       int     not null,
    team_h_difficulty   int,                       -- FDR for the home side
    team_a_difficulty   int,
    kickoff_time        timestamptz,
    started             boolean not null default false,
    finished            boolean not null default false,
    finished_provisional boolean not null default false,
    minutes             int     not null default 0,
    team_h_score        int,
    team_a_score        int,
    updated_at          timestamptz not null default now(),
    primary key (season, fixture_id)
);

create index if not exists fixtures_season_gw on fixtures (season, gw);
create index if not exists fixtures_home on fixtures (season, team_h_fpl_id, gw);
create index if not exists fixtures_away on fixtures (season, team_a_fpl_id, gw);

-- ---------------------------------------------------------------------------
-- player_gw_stats
--
-- The training set. One row per player per gameweek, holding what was true in
-- that gameweek and nothing else. Rows are rewritten once after the bonus
-- settles, then never again.
--
-- value_tenths is the player's price during that gameweek, snapshotted here so a
-- backtest can read a historical price without reaching into today's players row.
-- ---------------------------------------------------------------------------

create table if not exists player_gw_stats (
    season              text    not null,
    element_id          int     not null,
    gw                  int     not null,
    -- Part of the primary key, so implicitly NOT NULL: a blank gameweek produces
    -- no row at all rather than a null-fixture one, which is correct because a
    -- player who did not play has no stats. Read blanks off `fixtures` instead.
    -- A double gameweek produces two rows.
    fixture_id          int     not null,

    minutes             int     not null default 0,
    total_points        int     not null default 0,
    starts              int     not null default 0,
    goals_scored        int     not null default 0,
    assists             int     not null default 0,
    clean_sheets        int     not null default 0,
    goals_conceded      int     not null default 0,
    own_goals           int     not null default 0,
    penalties_saved     int     not null default 0,
    penalties_missed    int     not null default 0,
    yellow_cards        int     not null default 0,
    red_cards           int     not null default 0,
    saves               int     not null default 0,
    bonus               int     not null default 0,
    bps                 int     not null default 0,
    defensive_contribution int  not null default 0,

    expected_goals      numeric(8,2),
    expected_assists    numeric(8,2),
    expected_goal_involvements numeric(8,2),
    expected_goals_conceded    numeric(8,2),
    influence           numeric(8,2),
    creativity          numeric(8,2),
    threat              numeric(8,2),
    ict_index           numeric(8,2),

    was_home            boolean,
    opponent_team_fpl_id int,
    value_tenths        int,                       -- price during this gameweek, not today
    selected_by         int,                       -- ownership count at the time
    bonus_settled       boolean not null default false,

    updated_at          timestamptz not null default now(),
    primary key (season, element_id, gw, fixture_id),
    foreign key (season, element_id) references players (season, element_id)
);

create index if not exists player_gw_stats_season_gw on player_gw_stats (season, gw);

-- ---------------------------------------------------------------------------
-- price_history
--
-- One row per player per day. Needed to reconstruct purchase prices from a
-- transfer log and, later, to predict overnight rises. FPL changes prices at
-- ~01:30 UTC, so the daily snapshot runs after that.
-- ---------------------------------------------------------------------------

create table if not exists price_history (
    season              text    not null,
    element_id          int     not null,
    as_of_date          date    not null,
    cost_tenths         int     not null,
    cost_change_event_tenths int not null default 0,
    selected_by_percent numeric(6,2),
    transfers_in_event  int,
    transfers_out_event int,

    -- Availability as it was on this date. The daily snapshot is the only place
    -- it is ever recorded, and expected minutes is the dominant term in xP — so
    -- without these two columns no backtest can reproduce the model's largest
    -- input, and every evaluation number computed later is fiction. They are
    -- cheap here and unrecoverable after the fact.
    status              char(1),
    chance_of_playing_next_round int,

    captured_at         timestamptz not null default now(),
    primary key (season, element_id, as_of_date),
    foreign key (season, element_id) references players (season, element_id)
);

-- ---------------------------------------------------------------------------
-- xpoints
--
-- Model output, precomputed nightly so no user request ever waits on inference.
-- model_version is part of the key: a new version writes new rows beside the old
-- ones and never overwrites them, so past recommendations stay explainable.
-- ---------------------------------------------------------------------------

create table if not exists xpoints (
    season              text    not null,
    element_id          int     not null,
    gw                  int     not null,
    model_version       text    not null,
    xp                  numeric(8,3) not null,
    xmins               numeric(6,2),              -- expected minutes, the dominant term
    p_start             numeric(5,4),
    p_play              numeric(5,4),
    components          jsonb,                     -- per-term breakdown, for showing reasoning

    -- The last gameweek whose results fed this projection. Unrecoverable after
    -- the fact, and the one column that lets you audit a suspected leak.
    as_of_gw            int,
    -- Fixtures this player has in `gw`: 0 is a blank, 2 a double. Saves the UI a
    -- join to say so.
    fixture_count       int,

    computed_at         timestamptz not null default now(),
    primary key (season, element_id, gw, model_version),
    foreign key (season, element_id) references players (season, element_id)
);

create index if not exists xpoints_lookup on xpoints (season, model_version, gw, xp desc);

-- ---------------------------------------------------------------------------
-- recommendation_log
--
-- Anonymous by construction: a hash of the squad, never the squad's owner. This
-- is the only way the model is ever evaluated — whether the advice beat holding.
-- Unreconstructable after the fact, so it is written from the first solver on.
-- Do not add a column that could identify a person.
-- ---------------------------------------------------------------------------

create table if not exists recommendation_log (
    id                  bigint generated always as identity primary key,
    squad_hash          text    not null,          -- hash of sorted element ids + bank
    season              text    not null,
    gw                  int     not null,
    model_version       text    not null,
    solver_version      text    not null,
    horizon             int     not null,
    baseline_xp         numeric(8,3),
    payload             jsonb   not null,          -- the plans returned
    solve_ms            int,
    data_as_of          timestamptz,               -- as-of time of the prices used
    created_at          timestamptz not null default now()
);

create index if not exists recommendation_log_gw on recommendation_log (season, gw, created_at desc);
create index if not exists recommendation_log_squad on recommendation_log (squad_hash);

-- ---------------------------------------------------------------------------
-- ingest_runs
--
-- Operational log for the cron jobs. Tells you which sync last succeeded and
-- what the app's data is as-of, which every recommendation gets stamped with.
-- ---------------------------------------------------------------------------

create table if not exists ingest_runs (
    id                  bigint generated always as identity primary key,
    job                 text    not null,          -- 'bootstrap', 'fixtures', 'live', ...
    season              text    not null,
    started_at          timestamptz not null default now(),
    finished_at         timestamptz,
    ok                  boolean,
    rows_written        int,
    error               text
);

create index if not exists ingest_runs_job on ingest_runs (job, started_at desc);

-- ---------------------------------------------------------------------------
-- Additive changes
--
-- `create table if not exists` does not alter a table that already exists, so
-- columns added after a database was first built go here as idempotent ALTERs.
-- Re-running this file on an existing database brings it up to date.
-- ---------------------------------------------------------------------------

-- FPL's own expected points for a player-gameweek, as recorded by the
-- historical dataset (vaastav/Fantasy-Premier-League, `xP` in merged_gw.csv).
-- Null for rows synced live from the API, which exposes no historical projection.
--
-- NOT A FAIR BENCHMARK FOR ITS OWN GAMEWEEK. It was captured after the gameweek
-- was played and already reflects the result: players who score 10+ show an xP
-- about 3 points higher that week than the week before, and no rise after,
-- which a genuine forecast cannot do. Correlation with same-week points is 0.73
-- against 0.46 for the previous week's figure, in every season. Never use a
-- row's fpl_xp to predict or score that row's own gameweek. The previous
-- gameweek's value is the honest proxy for what FPL showed before a deadline;
-- the backtest calls it `fpl_xp_lag`.
alter table player_gw_stats add column if not exists fpl_xp numeric(8,3);

-- Where a row came from: 'api' for the live and backfill jobs, 'history' for
-- the season import. Lets a backtest reason about provenance, and lets a bad
-- import be deleted without touching live data.
alter table player_gw_stats add column if not exists source text not null default 'api';
