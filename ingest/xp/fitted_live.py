"""The production `AsOf`: today's view, built the way the backtest builds its views.

`History.asof` cannot serve a live gameweek. It decides who is registered from
the player's stat rows at and after the target, and before a deadline those
rows do not exist yet. So production builds its `AsOf` here instead, from the
same prepared tables, with the one thing that differs taken from where it truly
lives today: the `players` table, which for the *current* season and the
*upcoming* gameweek is exactly right — current club, current price, current
availability. That is the lookahead rule applied correctly rather than broken:
"today's value" is only a leak when predicting the past.

Everything else — stat rows strictly before the target, cross-season history
linked by `code`, fixtures with any result at or after the target blanked — is
cut exactly as `History.asof` cuts it, so the fitted model sees the same shapes
live as it did in every backtest gameweek.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from backtest.data import FIXTURE_RESULT_COLUMNS, AsOf, History, load_tables

from .fitted import availability_from_status

# The live columns read from `players`. Identity, today's club, today's price and
# today's availability — nothing season-to-date.
LIVE_PLAYER_COLUMNS = (
    "element_id",
    "code",
    "element_type",
    "web_name",
    "first_name",
    "second_name",
    "team_fpl_id",
    "now_cost_tenths",
    "status",
    "chance_of_playing_next_round",
)


def live_asof(history: History, season: str, gw: int, live_players: pd.DataFrame) -> AsOf:
    """An `AsOf` for predicting (`season`, `gw`) from today's player list.

    `history` holds the stat rows loaded so far (none can be at or after `gw` for
    a gameweek that has not started; any that exist — a gameweek in progress —
    are cut here). `live_players` has `LIVE_PLAYER_COLUMNS` for `season`.
    """
    s = history.stats
    before = (s["season"] < season) | ((s["season"] == season) & (s["gw"] < gw))
    stats = s[before].copy()

    teams = history.teams[history.teams["season"] <= season].copy()
    team_code = teams[teams["season"] == season].set_index("fpl_id")["code"]

    players = live_players.copy()
    players["season"] = season
    for column in ("element_id", "code", "element_type", "team_fpl_id"):
        players[column] = players[column].astype("int64")
    players["team_code"] = players["team_fpl_id"].map(team_code).astype("Int64")
    f = history.fixtures
    target = f[(f["season"] == season) & (f["gw"] == gw)]
    counts = pd.concat([target["team_h_fpl_id"], target["team_a_fpl_id"]]).value_counts()
    players["fixture_count"] = players["team_fpl_id"].map(counts).fillna(0).astype("int64")
    # Today's price is the point-in-time price for a forward prediction.
    players["price_tenths"] = pd.to_numeric(players["now_cost_tenths"]).astype("Int64")
    players["selected_by"] = pd.Series(pd.NA, index=players.index, dtype="Int64")
    players = players[
        [
            "season",
            "element_id",
            "code",
            "element_type",
            "web_name",
            "first_name",
            "second_name",
            "team_fpl_id",
            "team_code",
            "fixture_count",
            "price_tenths",
            "selected_by",
        ]
    ].reset_index(drop=True)

    code_to_element = dict(zip(players["code"], players["element_id"], strict=True))
    player_history = stats[stats["code"].isin(code_to_element)].copy()
    player_history = player_history.rename(columns={"element_id": "row_element_id"})
    player_history.insert(
        1, "element_id", player_history["code"].map(code_to_element).astype("int64")
    )
    player_history = player_history.sort_values(["element_id", "season", "gw", "fixture_id"])

    fixtures = f[f["season"] <= season].copy()
    unresolved = (fixtures["season"] == season) & (fixtures["gw"].isna() | (fixtures["gw"] >= gw))
    for column in FIXTURE_RESULT_COLUMNS:
        fixtures[column] = fixtures[column].astype("object")
        fixtures.loc[unresolved, column] = None

    return AsOf(
        season=season,
        gw=gw,
        stats=stats.reset_index(drop=True),
        player_history=player_history.reset_index(drop=True),
        players=players,
        fixtures=fixtures.reset_index(drop=True),
        teams=teams.reset_index(drop=True),
    )


def live_availability(live_players: pd.DataFrame) -> dict[int, float]:
    """Element id -> probability fit, from FPL's live status and chance of playing."""
    out = {}
    for row in live_players.itertuples(index=False):
        chance = row.chance_of_playing_next_round
        chance = None if chance is None or pd.isna(chance) else int(chance)
        out[int(row.element_id)] = availability_from_status(row.status, chance)
    return out


def load_live_asof(conn: Any, season: str, gw: int) -> tuple[AsOf, dict[int, float]]:
    """Read Postgres (selects only) and build today's `AsOf` plus live availability."""
    from psycopg.rows import dict_row

    tables = load_tables(conn)
    tables = type(tables)(
        stats=tables.stats[tables.stats["season"] <= season],
        players=tables.players[tables.players["season"] <= season],
        fixtures=tables.fixtures[tables.fixtures["season"] <= season],
        teams=tables.teams[tables.teams["season"] <= season],
    )
    history = History(tables)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"select {', '.join(LIVE_PLAYER_COLUMNS)} from players where season = %s", (season,)
        )
        live = pd.DataFrame(cur.fetchall(), columns=list(LIVE_PLAYER_COLUMNS))
    return live_asof(history, season, gw, live), live_availability(live)


def build_live_rows(conn: Any, season: str, target_gws: Sequence[int]) -> list[dict[str, Any]]:
    """What the nightly job would call: fit on everything so far, project the horizon.

    Returns rows in the `xpoints` shape (see `xp.fitted.xp_rows`), `model_version`
    `fitted-0.1`. `target_gws` comes from `compute_xp.plan_gameweeks`.
    """
    from .fitted import FittedXPModel, xp_rows

    if not target_gws:
        return []
    asof, availability = load_live_asof(conn, season, min(target_gws))
    model = FittedXPModel()
    model.fit(asof)
    return xp_rows(asof, model, target_gws, availability)
