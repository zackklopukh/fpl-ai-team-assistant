"""The as-of view: the only thing a model in the backtest is ever given.

Leakage is the failure that decides whether this whole package is worth running
(ARCHITECTURE.md, "Backtest leakage"). A leaky backtest does not crash; it
produces a confident, wrong answer about which model to ship. So the guarantee is
structural rather than a convention every model author has to remember:

* `History` holds everything — every season, every gameweek, the future included.
  Only the harness ever has one.
* `History.asof(season, gw)` builds an `AsOf`: new DataFrames filtered to what was
  knowable before that gameweek's deadline. It holds no reference to `History`,
  to the unfiltered frames, or to a database connection. A model that inspects
  every attribute it receives finds nothing from the target gameweek onward,
  because nothing from then was copied in.
* Actuals come from `History.actuals`, which models never see.

What "knowable before the deadline" means here, field by field:

* Stat rows: earlier seasons in full, plus the target season's rows with
  `gw < target`. Nothing at or after the target.
* Fixtures: the whole schedule is public in advance, so every fixture is listed —
  but result fields (scores, started, finished, minutes) are blanked for any
  target-season fixture at or after the target, and later seasons are absent.
* Players: identity only (`code`, `element_type`, names). The `players` table's
  price, points, form and ownership columns are never even loaded: for a past
  season they are end-of-season values, and there is no safe way to use them.
  `team_fpl_id` there is the end-of-season club, so the club at the target is
  derived from the player's own fixtures instead.
* Teams: identity only. FPL revises its strength ratings during a season, so a
  past season's stored strengths are end-of-season values too.
* Price and ownership: the player's most recent `value_tenths` / `selected_by`
  from a stat row before the target — point-in-time, and never a `players` column.
* FPL's recorded projection (`fpl_xp`): only on the rows before the target, like
  every other stat column. The target gameweek's own `fpl_xp` is NOT exposed. It
  looks like a pre-deadline projection but the data says otherwise: players who
  haul in gameweek t already show a ~3-point jump in their gameweek-t `fpl_xp`,
  and no further jump afterwards (see `diagnostics`, `fpl_xp_haul_jump_*`). The
  recorded figure was evidently captured after the gameweek it describes, so it
  carries that gameweek's outcome, and a model reading it would be reading the
  answer. The recorded benchmark is read straight from `History` by the harness
  instead (`History.recorded_fpl_xp`), outside the model interface and labelled.

One thing this cannot do is sandbox Python: a model could import `db` and query
Postgres itself. The guarantee is that the interface a model is handed contains
no path to the future, not that a model determined to cheat cannot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

# --- What is loaded --------------------------------------------------------

# Explicit column lists, so that a column added to a table later does not quietly
# flow into models. `updated_at` is excluded everywhere: it records when a row was
# written, which for a historical import is after the season ended.

STAT_COLUMNS = (
    "season",
    "element_id",
    "gw",
    "fixture_id",
    "minutes",
    "total_points",
    "starts",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "defensive_contribution",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "influence",
    "creativity",
    "threat",
    "ict_index",
    "was_home",
    "opponent_team_fpl_id",
    "value_tenths",
    "selected_by",
    "fpl_xp",
    "source",
)

_STAT_INT_COLUMNS = (
    "minutes",
    "total_points",
    "starts",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
)

_STAT_FLOAT_COLUMNS = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "influence",
    "creativity",
    "threat",
    "ict_index",
    "fpl_xp",
)

# Money stays integer tenths (CLAUDE.md invariant 1). Nullable, because a row can
# lack a price, but never float.
_STAT_NULLABLE_INT_COLUMNS = (
    "value_tenths",
    "selected_by",
    "opponent_team_fpl_id",
    "defensive_contribution",
)

# Defensive contribution became a scored stat in 2025-26. Earlier seasons record
# 0 because the stat did not exist, not because nobody made a tackle, so those
# values are set to missing before any model can read them as zeros.
DEFCON_FIRST_SEASON = "2025-26"

# The only `players` columns that are true for the whole season. Everything else
# in that table is as-of the last sync, which for a past season is its final day.
PLAYER_IDENTITY_COLUMNS = (
    "season",
    "element_id",
    "code",
    "element_type",
    "web_name",
    "first_name",
    "second_name",
)

TEAM_IDENTITY_COLUMNS = ("season", "fpl_id", "code", "name", "short_name")

FIXTURE_COLUMNS = (
    "season",
    "fixture_id",
    "gw",
    "team_h_fpl_id",
    "team_a_fpl_id",
    "team_h_difficulty",
    "team_a_difficulty",
    "kickoff_time",
    "started",
    "finished",
    "finished_provisional",
    "minutes",
    "team_h_score",
    "team_a_score",
)

# Blanked for any fixture at or after the target: they are the result.
FIXTURE_RESULT_COLUMNS = (
    "started",
    "finished",
    "finished_provisional",
    "minutes",
    "team_h_score",
    "team_a_score",
)


@dataclass(frozen=True)
class Tables:
    """The four raw tables, as loaded. Held only by `History`."""

    stats: pd.DataFrame
    players: pd.DataFrame
    fixtures: pd.DataFrame
    teams: pd.DataFrame


def _frame(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in rows], columns=list(columns))


def load_tables(conn: Any, seasons: Sequence[str] | None = None) -> Tables:
    """Read the four tables once, selecting only leak-safe columns.

    Read-only: four selects and nothing else. `seasons` narrows the load; by
    default every season in `player_gw_stats` is read.
    """
    from psycopg.rows import dict_row

    where = ""
    params: tuple[Any, ...] = ()
    if seasons:
        where = " where season = any(%s)"
        params = (list(seasons),)

    def select(table: str, columns: Sequence[str]) -> pd.DataFrame:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"select {', '.join(columns)} from {table}{where}", params)
            return _frame(cur.fetchall(), columns)

    return Tables(
        stats=select("player_gw_stats", STAT_COLUMNS),
        players=select("players", PLAYER_IDENTITY_COLUMNS),
        fixtures=select("fixtures", FIXTURE_COLUMNS),
        teams=select("teams", TEAM_IDENTITY_COLUMNS),
    )


# --- The view a model receives ---------------------------------------------


@dataclass(frozen=True, eq=False)
class AsOf:
    """Everything knowable before the deadline of (`season`, `gw`), and nothing else.

    Every frame is a private copy, cut for this target alone. Attributes:

    * `stats` — `player_gw_stats` rows strictly before the target, all earlier
      seasons included, with each row's `code`, `element_type`, and the club the
      player was at for that fixture (`team_fpl_id`, `team_code`).
    * `player_history` — the same rows restricted to players registered at the
      target and re-keyed to their *target-season* `element_id`, linked across
      seasons by `code`. The original id stays in `row_element_id`. This is the
      history a per-player model wants; the cross-season join is done here so
      that no model has to get it right itself.
    * `players` — players registered at the target: identity, club at the target,
      `fixture_count` in the target gameweek (0 blank, 2 double), and
      point-in-time `price_tenths` / `selected_by` from the last row before it.
    * `fixtures` — every fixture of the target and earlier seasons, results
      blanked at and after the target.
    * `teams` — identity only, target and earlier seasons.

    There is deliberately no field holding anything from a target-gameweek stat
    row — including FPL's recorded `fpl_xp`, which carries the outcome (see the
    module docstring).
    """

    season: str
    gw: int
    stats: pd.DataFrame
    player_history: pd.DataFrame
    players: pd.DataFrame
    fixtures: pd.DataFrame
    teams: pd.DataFrame

    @property
    def as_of_gw(self) -> int:
        """The last target-season gameweek whose results are visible."""
        return self.gw - 1

    def target_fixtures(self) -> pd.DataFrame:
        """The target gameweek's fixtures: zero, one or two per team."""
        f = self.fixtures
        return f[(f["season"] == self.season) & (f["gw"] == self.gw)]

    def season_stats(self) -> pd.DataFrame:
        """Target-season rows only, keyed by this season's element ids."""
        return self.stats[self.stats["season"] == self.season]


# --- The full history, held by the harness ---------------------------------


class History:
    """All loaded data, future included. Builds `AsOf` views and actuals.

    Never hand one of these to a model. It exists so that the expensive work —
    loading, typing, deriving each row's club, linking seasons — happens once,
    and each `AsOf` is a cheap filter over it.
    """

    def __init__(self, tables: Tables):
        self.teams = _prepare_teams(tables.teams)
        self.fixtures = _prepare_fixtures(tables.fixtures, self.teams)
        self.players = _prepare_players(tables.players)
        self.stats = _prepare_stats(tables.stats, self.players, self.fixtures, self.teams)
        self.seasons: tuple[str, ...] = tuple(sorted(self.stats["season"].unique()))

        # Each player's registration span per season, from their own rows. FPL
        # writes a row for every registered player whose team plays, minutes or
        # not, so first..last row brackets the gameweeks they were on the books.
        self._spans = (
            self.stats.groupby(["season", "element_id"])["gw"]
            .agg(first_gw="min", last_gw="max")
            .reset_index()
        )

    # -- Calendar ---------------------------------------------------------

    def gameweeks(self, season: str) -> list[int]:
        """Gameweeks of `season` that have results, in order."""
        gws = self.stats.loc[self.stats["season"] == season, "gw"].unique()
        return sorted(int(g) for g in gws)

    # -- Who is in the population -----------------------------------------

    def registered(self, season: str, gw: int) -> pd.DataFrame:
        """Players registered at (`season`, `gw`), with their club and fixture count.

        Registered means the target falls within the player's first..last row of
        the season. A player whose team blanks is still registered (rows either
        side), a player sold before the deadline is not (no row at or after it),
        and a January signing is not yet registered before his first gameweek.

        This reads one fact from the target gameweek — which club the player is
        at — and that is pre-deadline knowledge: transfers close before it. It
        reads nothing about how he played.
        """
        spans = self._spans[
            (self._spans["season"] == season)
            & (self._spans["first_gw"] <= gw)
            & (self._spans["last_gw"] >= gw)
        ]
        season_rows = self.stats[
            (self.stats["season"] == season)
            & (self.stats["gw"] <= gw)
            & self.stats["element_id"].isin(spans["element_id"])
        ]
        # The target row's club if there is one, otherwise the latest before it.
        latest = (
            season_rows.sort_values(["element_id", "gw", "fixture_id"])
            .groupby("element_id", as_index=False)
            .last()[["element_id", "team_fpl_id", "team_code"]]
        )

        players = self.players[self.players["season"] == season].merge(
            latest, on="element_id", how="inner"
        )
        counts = _fixture_counts(self.fixtures, season, gw)
        players["fixture_count"] = (
            players["team_fpl_id"].map(counts).fillna(0).astype("int64")
        )
        return players.reset_index(drop=True)

    # -- The view -----------------------------------------------------------

    def asof(self, season: str, gw: int) -> AsOf:
        """Build the leak-free view for predicting (`season`, `gw`)."""
        if season not in self.seasons:
            raise KeyError(f"no data for season {season}")

        s = self.stats
        before = (s["season"] < season) | ((s["season"] == season) & (s["gw"] < gw))
        stats = s[before].copy()

        players = self.registered(season, gw)

        # Point-in-time price and ownership: the last row before the target, this
        # season. A previous season's closing price is not carried over — prices
        # are reset every summer — so gameweek 1 has none.
        prior = stats[stats["season"] == season].sort_values(["element_id", "gw", "fixture_id"])
        last_seen = prior.groupby("element_id", as_index=False).last()[
            ["element_id", "value_tenths", "selected_by"]
        ]
        players = players.merge(
            last_seen.rename(columns={"value_tenths": "price_tenths"}),
            on="element_id",
            how="left",
        )
        players["price_tenths"] = players["price_tenths"].astype("Int64")
        players["selected_by"] = players["selected_by"].astype("Int64")

        # Linked history: every earlier row of any registered player, under the
        # player's target-season element id.
        code_to_element = dict(zip(players["code"], players["element_id"], strict=True))
        history = stats[stats["code"].isin(code_to_element)].copy()
        history = history.rename(columns={"element_id": "row_element_id"})
        history.insert(1, "element_id", history["code"].map(code_to_element).astype("int64"))
        history = history.sort_values(["element_id", "season", "gw", "fixture_id"])

        fixtures = self.fixtures[self.fixtures["season"] <= season].copy()
        unresolved = (fixtures["season"] == season) & (
            fixtures["gw"].isna() | (fixtures["gw"] >= gw)
        )
        for column in FIXTURE_RESULT_COLUMNS:
            fixtures[column] = fixtures[column].astype("object")
            fixtures.loc[unresolved, column] = None

        teams = self.teams[self.teams["season"] <= season].copy()

        return AsOf(
            season=season,
            gw=gw,
            stats=stats.reset_index(drop=True),
            player_history=history.reset_index(drop=True),
            players=players.reset_index(drop=True),
            fixtures=fixtures.reset_index(drop=True),
            teams=teams.reset_index(drop=True),
        )

    def recorded_fpl_xp(self, season: str, gw: int) -> pd.Series:
        """FPL's recorded xP for (`season`, `gw`), one value per registered player.

        Not part of any `AsOf`: the recorded figure contains the gameweek's own
        outcome (module docstring), so only the harness's reference benchmark
        reads it, from here.

        The import stores FPL's figure as a whole-gameweek value on the first
        fixture's row of a double and leaves the second row null, so the
        gameweek's projection is the sum over the player's rows with nulls
        ignored. A player with no non-null row has no projection — that is
        missing coverage (the source did not capture every gameweek), never 0.
        """
        s = self.stats
        element_ids = self.registered(season, gw)["element_id"]
        rows = s[(s["season"] == season) & (s["gw"] == gw) & s["element_id"].isin(element_ids)]
        rows = rows[rows["fpl_xp"].notna()]
        projection = rows.groupby("element_id")["fpl_xp"].sum().astype("float64")
        projection.name = "fpl_xp"
        return projection

    # -- The answer, never shown to a model ---------------------------------

    def actuals(self, season: str, gw: int) -> pd.DataFrame:
        """Actual points for every player registered at the target.

        Points are summed over the player's rows, so a double gameweek is the sum
        of both fixtures. A registered player with no row — a blank, or a team
        that played without him in the squad — scored exactly 0.
        """
        players = self.registered(season, gw)
        s = self.stats
        rows = s[(s["season"] == season) & (s["gw"] == gw)]
        summed = rows.groupby("element_id").agg(
            actual=("total_points", "sum"),
            actual_minutes=("minutes", "sum"),
            rows=("fixture_id", "count"),
        )
        out = players[["element_id", "code", "element_type", "team_fpl_id", "fixture_count"]].merge(
            summed, left_on="element_id", right_index=True, how="left"
        )
        for column in ("actual", "actual_minutes", "rows"):
            out[column] = out[column].fillna(0).astype("int64")
        return out

    # -- Data health ----------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Counts that say whether a metric computed on this data can be trusted."""
        s = self.stats
        f = self.fixtures
        out: dict[str, Any] = {}
        for season in self.seasons:
            rows = s[s["season"] == season]
            fx = f[f["season"] == season][["fixture_id", "gw"]].rename(columns={"gw": "fixture_gw"})
            joined = rows.merge(fx, on="fixture_id", how="left")
            per_gw = rows.groupby(["element_id", "gw"])["fixture_id"].transform("count")
            doubles = rows[per_gw > 1]
            dgw_xp_rows = doubles.groupby(["element_id", "gw"])["fpl_xp"].count()
            xp_gws = sorted(int(g) for g in rows.loc[rows["fpl_xp"].notna(), "gw"].unique())
            out[season] = {
                **_haul_signature(rows),
                "stat_rows": int(len(rows)),
                "gameweeks": len(self.gameweeks(season)),
                "players_with_rows": int(rows["element_id"].nunique()),
                "sources": {str(k): int(v) for k, v in rows["source"].value_counts().items()},
                "fpl_xp_coverage": round(float(rows["fpl_xp"].notna().mean()), 4) if len(rows) else 0.0,
                "value_tenths_missing": int(rows["value_tenths"].isna().sum()),
                "rows_without_fixture": int(joined["fixture_gw"].isna().sum()),
                # A row whose fixture now sits in a different gameweek: a postponed
                # match re-homed after the fact. See report caveats.
                "rows_fixture_gw_mismatch": int(
                    (joined["fixture_gw"].notna() & (joined["fixture_gw"] != joined["gw"])).sum()
                ),
                "fpl_xp_gameweeks": len(xp_gws),
                "fpl_xp_missing_gameweeks": [
                    g for g in self.gameweeks(season) if g not in set(xp_gws)
                ],
                "double_gw_player_gws": int(dgw_xp_rows.size),
                # fpl_xp is expected on exactly one row of a double; more than one
                # would mean the per-gameweek sum double-counts.
                "double_gw_fpl_xp_on_both_rows": int((dgw_xp_rows > 1).sum()),
            }
        return out


def _haul_signature(rows: pd.DataFrame, haul: int = 10) -> dict[str, Any]:
    """How much a hauler's recorded fpl_xp moves into, and after, the haul week.

    An honest pre-deadline projection cannot know a haul is coming, so it should
    barely move into the haul week and rise only afterwards, when form updates.
    A large jump *into* the week with none after it means the recorded figure was
    captured after the gameweek and carries its outcome.
    """
    gw = rows.groupby(["element_id", "gw"]).agg(
        pts=("total_points", "sum"), xp=("fpl_xp", "sum"), nxp=("fpl_xp", "count")
    )
    gw.loc[gw["nxp"] == 0, "xp"] = float("nan")
    gw = gw.reset_index().sort_values(["element_id", "gw"])
    by = gw.groupby("element_id")
    consecutive = (by["gw"].shift(1) == gw["gw"] - 1) & (by["gw"].shift(-1) == gw["gw"] + 1)
    into = gw["xp"] - by["xp"].shift(1)
    after = by["xp"].shift(-1) - gw["xp"]
    mask = consecutive & (gw["pts"] >= haul) & into.notna() & after.notna()
    if not mask.any():
        return {"fpl_xp_haul_weeks": 0}
    return {
        "fpl_xp_haul_weeks": int(mask.sum()),
        "fpl_xp_haul_jump_into_gw": round(float(into[mask].mean()), 3),
        "fpl_xp_haul_jump_after_gw": round(float(after[mask].mean()), 3),
    }


# --- Preparation -----------------------------------------------------------


def _prepare_teams(teams: pd.DataFrame) -> pd.DataFrame:
    teams = teams[list(TEAM_IDENTITY_COLUMNS)].copy()
    teams["fpl_id"] = teams["fpl_id"].astype("int64")
    teams["code"] = teams["code"].astype("int64")
    return teams


def _prepare_fixtures(fixtures: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    fixtures = fixtures[list(FIXTURE_COLUMNS)].copy()
    for column in ("fixture_id", "team_h_fpl_id", "team_a_fpl_id"):
        fixtures[column] = fixtures[column].astype("int64")
    for column in ("gw", "team_h_difficulty", "team_a_difficulty", "team_h_score", "team_a_score"):
        fixtures[column] = pd.to_numeric(fixtures[column]).astype("Int64")
    fixtures["minutes"] = pd.to_numeric(fixtures["minutes"]).astype("Int64")
    for column in ("started", "finished", "finished_provisional"):
        fixtures[column] = fixtures[column].astype("boolean")

    # Team codes, so a model can follow a club across seasons as it follows a
    # player: fpl_id is reassigned every summer, code is not.
    code = teams.set_index(["season", "fpl_id"])["code"]
    fixtures["team_h_code"] = [
        code.get((s, t)) for s, t in zip(fixtures["season"], fixtures["team_h_fpl_id"], strict=True)
    ]
    fixtures["team_a_code"] = [
        code.get((s, t)) for s, t in zip(fixtures["season"], fixtures["team_a_fpl_id"], strict=True)
    ]
    fixtures["team_h_code"] = pd.to_numeric(fixtures["team_h_code"]).astype("Int64")
    fixtures["team_a_code"] = pd.to_numeric(fixtures["team_a_code"]).astype("Int64")
    return fixtures


def _prepare_players(players: pd.DataFrame) -> pd.DataFrame:
    players = players[list(PLAYER_IDENTITY_COLUMNS)].copy()
    for column in ("element_id", "code", "element_type"):
        players[column] = players[column].astype("int64")
    return players


def _prepare_stats(
    stats: pd.DataFrame, players: pd.DataFrame, fixtures: pd.DataFrame, teams: pd.DataFrame
) -> pd.DataFrame:
    """Type the stat rows and attach code, position and the club for each fixture."""
    stats = stats[list(STAT_COLUMNS)].copy()
    for column in ("element_id", "gw", "fixture_id", *_STAT_INT_COLUMNS):
        stats[column] = pd.to_numeric(stats[column]).fillna(0).astype("int64")
    for column in _STAT_FLOAT_COLUMNS:
        stats[column] = pd.to_numeric(stats[column], errors="coerce").astype("float64")
    for column in _STAT_NULLABLE_INT_COLUMNS:
        stats[column] = pd.to_numeric(stats[column]).astype("Int64")
    stats["was_home"] = stats["was_home"].astype("boolean")
    stats.loc[stats["season"] < DEFCON_FIRST_SEASON, "defensive_contribution"] = pd.NA

    stats = stats.merge(
        players[["season", "element_id", "code", "element_type"]],
        on=["season", "element_id"],
        how="left",
    )
    missing = stats["code"].isna()
    if missing.any():
        # The foreign key makes this impossible in Postgres; in a hand-built frame
        # it is a bug worth failing on, since an unlinked row is silently lost.
        raise ValueError(f"{int(missing.sum())} stat rows have no players row")
    stats["code"] = stats["code"].astype("int64")
    stats["element_type"] = stats["element_type"].astype("int64")

    # The club for each row comes from the row's own fixture: home side if
    # was_home, else away. `players.team_fpl_id` is only the end-of-season club,
    # which is wrong for anyone who moved in January.
    sides = fixtures[["season", "fixture_id", "team_h_fpl_id", "team_a_fpl_id"]]
    stats = stats.merge(sides, on=["season", "fixture_id"], how="left")
    # With was_home missing, fall back to the opponent column: facing the home
    # side means playing away. With both missing the club stays unknown.
    opponent_is_home = stats["opponent_team_fpl_id"] == stats["team_h_fpl_id"]
    is_home = stats["was_home"].fillna(~opponent_is_home)
    team = pd.Series(pd.NA, index=stats.index, dtype="Int64")
    home_mask = is_home.fillna(False).astype(bool)
    away_mask = (~is_home).fillna(False).astype(bool)
    team[home_mask] = stats.loc[home_mask, "team_h_fpl_id"]
    team[away_mask] = stats.loc[away_mask, "team_a_fpl_id"]
    stats["team_fpl_id"] = team
    stats = stats.drop(columns=["team_h_fpl_id", "team_a_fpl_id"])

    code = teams.set_index(["season", "fpl_id"])["code"]
    stats["team_code"] = pd.to_numeric(
        pd.Series(
            [
                code.get((s, t)) if pd.notna(t) else None
                for s, t in zip(stats["season"], stats["team_fpl_id"], strict=True)
            ],
            index=stats.index,
            dtype="object",
        )
    ).astype("Int64")

    return stats.sort_values(["season", "gw", "element_id", "fixture_id"]).reset_index(drop=True)


def _fixture_counts(fixtures: pd.DataFrame, season: str, gw: int) -> dict[int, int]:
    """How many fixtures each team plays in (`season`, `gw`): 0, 1 or 2."""
    f = fixtures[(fixtures["season"] == season) & (fixtures["gw"] == gw)]
    counts = pd.concat([f["team_h_fpl_id"], f["team_a_fpl_id"]]).value_counts()
    return {int(k): int(v) for k, v in counts.items()}
