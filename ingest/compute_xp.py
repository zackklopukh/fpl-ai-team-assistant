"""Nightly job: recompute expected points for the next five gameweeks.

Runs at ~05:00 UTC, after the evening's live results and the price snapshot, so
that every user request is served from a precomputed `xpoints` row and no page
ever waits on inference. The horizon is capped at five because beyond that the
fixture information is too noisy to be worth the rows.

Three things this job is careful about:

* **Doubles and blanks.** Fixtures are joined to players one-to-many, through the
  team, per gameweek. Two fixtures produce roughly double the points; zero
  fixtures produce exactly zero. Nothing here is shaped like "the fixture".
* **Leakage.** Features come from `player_gw_stats` as of the last finished
  gameweek, never from the `players` table's season-to-date columns, which
  describe today. Only availability — `status` and `chance_of_playing_next_round`
  — is read from `players`, because there is no historical snapshot of it and it
  is the correct value when predicting forward.
* **Versioning.** Rows are keyed by `model_version`, so a bumped version writes a
  new set beside the old one and past recommendations stay explainable.

Run locally with the same command GitHub Actions uses:

    python ingest/compute_xp.py
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from config import SEASON
from xp.features import MINUTES_WINDOW, build_features, group_stat_rows
from xp.model import MODEL_VERSION, FixtureContext, expected_points

log = logging.getLogger(__name__)

JOB = "xpoints"
HORIZON = 5

XPOINTS_KEYS = ("season", "element_id", "gw", "model_version")


# --- Pure planning and computation ----------------------------------------


def plan_gameweeks(
    gameweek_rows: Sequence[Mapping[str, Any]], horizon: int = HORIZON
) -> tuple[int, list[int]]:
    """Return (as_of_gw, target gameweeks).

    `as_of_gw` is the last finished gameweek and is the cutoff every feature is
    built against. The targets are the next `horizon` unfinished gameweeks —
    taken from the gameweek list rather than from `as_of_gw + 1` so that a
    postponed or renumbered event does not silently shift the window.
    """
    finished = [int(r["gw"]) for r in gameweek_rows if r["finished"]]
    upcoming = sorted(int(r["gw"]) for r in gameweek_rows if not r["finished"])

    as_of_gw = max(finished) if finished else 0
    return as_of_gw, upcoming[:horizon]


def matches_played_by_team(
    fixture_rows: Sequence[Mapping[str, Any]], as_of_gw: int, window: int = MINUTES_WINDOW
) -> dict[int, int]:
    """Count each team's finished fixtures in the recent window.

    The minutes model needs a denominator — starts out of how many chances — and
    `player_gw_stats` cannot supply it: a blank leaves no row, and neither does a
    player left out of the squad, and those mean opposite things. Fixtures are the
    only honest source. A double gameweek counts twice, which is correct.
    """
    counts: dict[int, int] = defaultdict(int)

    for row in fixture_rows:
        if row.get("gw") is None or not row.get("finished"):
            continue
        gw = int(row["gw"])
        if not (as_of_gw - window < gw <= as_of_gw):
            continue
        counts[int(row["team_h_fpl_id"])] += 1
        counts[int(row["team_a_fpl_id"])] += 1

    return dict(counts)


def fixtures_by_team_gw(
    fixture_rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[int, int], list[FixtureContext]]:
    """Index fixtures by (team, gameweek), many per key.

    A team appears under a key zero times (blank), once, or twice (double). The
    difficulty stored is the one that team faces, so the model never has to work
    out which side of the fixture row a player is on.
    """
    index: dict[tuple[int, int], list[FixtureContext]] = defaultdict(list)

    for row in fixture_rows:
        if row.get("gw") is None:
            continue  # not yet scheduled; it belongs to no gameweek
        gw = int(row["gw"])
        fixture_id = row.get("fixture_id")

        index[(int(row["team_h_fpl_id"]), gw)].append(
            FixtureContext(
                fixture_id=fixture_id,
                gw=gw,
                is_home=True,
                difficulty=row.get("team_h_difficulty"),
            )
        )
        index[(int(row["team_a_fpl_id"]), gw)].append(
            FixtureContext(
                fixture_id=fixture_id,
                gw=gw,
                is_home=False,
                difficulty=row.get("team_a_difficulty"),
            )
        )

    return dict(index)


def build_xp_rows(
    *,
    season: str,
    player_rows: Sequence[Mapping[str, Any]],
    stat_rows: Sequence[Mapping[str, Any]],
    fixture_rows: Sequence[Mapping[str, Any]],
    target_gws: Sequence[int],
    as_of_gw: int,
) -> list[dict[str, Any]]:
    """Compute one `xpoints` row per player per target gameweek.

    Pure: no database, no clock, no network. The nightly job is this function
    plus two queries and an upsert, which is what makes it testable at all.
    """
    stats_by_player = group_stat_rows(stat_rows)
    fixture_index = fixtures_by_team_gw(fixture_rows)
    team_matches = matches_played_by_team(fixture_rows, as_of_gw)

    rows: list[dict[str, Any]] = []

    for player in player_rows:
        element_id = int(player["element_id"])
        team = int(player["team_fpl_id"])

        features = build_features(
            element_id=element_id,
            element_type=int(player["element_type"]),
            team_fpl_id=team,
            stat_rows=stats_by_player.get(element_id, []),
            as_of_gw=as_of_gw,
            matches_in_window=team_matches.get(team, 0),
            # The only two columns read from `players`. Both describe today,
            # which is correct here and a leak in a backtest — see xp/features.py.
            status=player.get("status") or "a",
            chance_of_playing=player.get("chance_of_playing_next_round"),
        )

        for gw in target_gws:
            gw_fixtures = fixture_index.get((team, gw), [])
            result = expected_points(features, gw_fixtures, gw)
            rows.append(
                {
                    "season": season,
                    "element_id": element_id,
                    "gw": gw,
                    "model_version": MODEL_VERSION,
                    "xp": result.xp,
                    "xmins": result.xmins,
                    "p_start": result.p_start,
                    "p_play": result.p_play,
                    "components": result.components,
                    # Recorded because neither is recoverable later: `as_of_gw`
                    # is what a leak audit checks against, and `fixture_count`
                    # lets the UI say "double gameweek" without a join.
                    "as_of_gw": as_of_gw,
                    "fixture_count": len(gw_fixtures),
                }
            )

    return rows


# --- Database edges --------------------------------------------------------


def load_inputs(
    conn: psycopg.Connection, season: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the gameweek calendar and the player list.

    Only five columns of `players` are selected, and three of them are identity.
    That is on purpose: the season-to-date columns next to them are today's
    values and must not reach the model.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select gw, finished from gameweeks where season = %s order by gw",
            (season,),
        )
        gameweeks = cur.fetchall()

        cur.execute(
            "select element_id, element_type, team_fpl_id, status, "
            "chance_of_playing_next_round from players where season = %s",
            (season,),
        )
        players = cur.fetchall()

    return gameweeks, players


def load_history(
    conn: psycopg.Connection, season: str, as_of_gw: int
) -> list[dict[str, Any]]:
    """Every `player_gw_stats` row at or before `as_of_gw`.

    The `gw <= as_of_gw` filter is redundant with the one in `build_features` and
    kept anyway: it keeps the query honest about what the model is entitled to
    see, and it is a lot of rows not to fetch.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select element_id, gw, fixture_id, minutes, starts, bps, "
            "defensive_contribution, expected_goals, expected_assists, "
            "expected_goals_conceded "
            "from player_gw_stats where season = %s and gw <= %s",
            (season, as_of_gw),
        )
        return cur.fetchall()


def load_fixtures(
    conn: psycopg.Connection, season: str, target_gws: Sequence[int], as_of_gw: int
) -> list[dict[str, Any]]:
    """Fixtures for the target gameweeks plus the recent window.

    Two jobs in one query: the target gameweeks are what gets projected, and the
    recent ones supply the minutes model's denominator. Many rows per team is
    normal in both halves.
    """
    gws = sorted(
        {*target_gws, *range(max(as_of_gw - MINUTES_WINDOW + 1, 1), as_of_gw + 1)}
    )
    if not gws:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select fixture_id, gw, team_h_fpl_id, team_a_fpl_id, "
            "team_h_difficulty, team_a_difficulty, finished "
            "from fixtures where season = %s and gw = any(%s)",
            (season, gws),
        )
        return cur.fetchall()


def _for_db(rows: Sequence[dict[str, Any]], computed_at: datetime) -> list[dict[str, Any]]:
    """Wrap components as jsonb and stamp the run.

    `computed_at` is written explicitly rather than left to the column default,
    because the default only fires on insert and a re-run of the same version
    needs to say when it last ran.
    """
    return [
        {**row, "components": Jsonb(row["components"]), "computed_at": computed_at}
        for row in rows
    ]


def main(season: str = SEASON) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with db.connect() as conn:
        with db.run_logged(conn, JOB, season) as result:
            gameweeks, players = load_inputs(conn, season)
            as_of_gw, target_gws = plan_gameweeks(gameweeks)

            if not target_gws:
                # The off-season, or a season whose gameweeks have all finished.
                log.info("%s: no upcoming gameweeks for %s — nothing to do", JOB, season)
                return 0

            log.info(
                "%s: %s, features as of gw%d, projecting gw%s with %s",
                JOB,
                season,
                as_of_gw,
                target_gws,
                MODEL_VERSION,
            )

            rows = build_xp_rows(
                season=season,
                player_rows=players,
                stat_rows=load_history(conn, season, as_of_gw),
                fixture_rows=load_fixtures(conn, season, target_gws, as_of_gw),
                target_gws=target_gws,
                as_of_gw=as_of_gw,
            )

            result["rows"] = db.upsert(
                conn,
                "xpoints",
                _for_db(rows, datetime.now(UTC)),
                conflict_keys=list(XPOINTS_KEYS),
            )
            conn.commit()

    return result["rows"]


if __name__ == "__main__":
    main()
