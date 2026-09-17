"""Sync `bootstrap-static` into teams, gameweeks and players.

The most frequent job in the repo — every 30 minutes in season — because it is
the only thing carrying injury news and price changes, and both of those land
without warning on a Friday afternoon.

The payload-to-row transforms below are pure functions on purpose: they are the
part that can be wrong in a way no exception reveals, so they are tested offline
against a saved payload. Everything touching the network or the database lives in
`main`.

Two mappings are easy to get subtly wrong and are therefore written out in full
rather than inferred:

* The API's `id` is the schema's `fpl_id` / `element_id`, and the API's `team` is
  `team_fpl_id`. Both are reassigned every season, which is why `season` is on
  every row.
* Money arrives already in tenths (`now_cost: 60` is £6.0m). It is carried across
  as an int and suffixed `_tenths`; it is never divided.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import config
import db
from fpl_client import FPLClient

log = logging.getLogger(__name__)


def as_numeric(value: Any) -> Decimal | None:
    """FPL sends its numerics as strings ("7.2", "0.00").

    Decimal rather than float: these land in numeric columns, and a float round
    trip is how "0.1" becomes "0.09999999999999999" in a training set.
    """
    if value is None or value == "":
        return None
    return Decimal(str(value))


def as_timestamp(value: Any) -> datetime | None:
    """ISO-8601 with a trailing Z, as every FPL timestamp is. Always UTC."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_team_rows(payload: dict, season: str = config.SEASON) -> list[dict]:
    """teams[] -> `teams` rows.

    `strength` and the strength_* breakdown are null in the preseason payload and
    fill in once the season starts, so none of them is required.
    """
    updated_at = _now()

    return [
        {
            "season": season,
            "fpl_id": team["id"],
            "code": team["code"],
            "name": team["name"],
            "short_name": team["short_name"],
            "strength": team.get("strength"),
            "strength_overall_home": team.get("strength_overall_home"),
            "strength_overall_away": team.get("strength_overall_away"),
            "strength_attack_home": team.get("strength_attack_home"),
            "strength_attack_away": team.get("strength_attack_away"),
            "strength_defence_home": team.get("strength_defence_home"),
            "strength_defence_away": team.get("strength_defence_away"),
            "updated_at": updated_at,
        }
        for team in payload["teams"]
    ]


def build_gameweek_rows(payload: dict, season: str = config.SEASON) -> list[dict]:
    """events[] -> `gameweeks` rows.

    `deadline_time` is the clock the rest of the system derives from, so it is the
    one field here with no tolerance for a null.
    """
    updated_at = _now()

    return [
        {
            "season": season,
            "gw": event["id"],
            "name": event["name"],
            "deadline_time": as_timestamp(event["deadline_time"]),
            "is_current": bool(event.get("is_current")),
            "is_next": bool(event.get("is_next")),
            "is_previous": bool(event.get("is_previous")),
            "finished": bool(event.get("finished")),
            # True once bonus has settled. sync_live keys bonus_settled off this.
            "data_checked": bool(event.get("data_checked")),
            "average_entry_score": event.get("average_entry_score"),
            "highest_score": event.get("highest_score"),
            "updated_at": updated_at,
        }
        for event in payload["events"]
    ]


def build_player_rows(payload: dict, season: str = config.SEASON) -> list[dict]:
    """elements[] -> `players` rows.

    Everything from `minutes` down is a season-to-date aggregate describing today,
    not any particular gameweek. It is stored for the app to display and must
    never be used as a feature when predicting a past gameweek — train from
    player_gw_stats instead.

    `full_name` is a generated column and is deliberately not written.
    """
    updated_at = _now()
    rows = []

    for element in payload["elements"]:
        rows.append(
            {
                "season": season,
                "element_id": element["id"],
                "code": element["code"],
                "team_fpl_id": element["team"],
                "element_type": element["element_type"],
                "web_name": element["web_name"],
                "first_name": element.get("first_name"),
                "second_name": element.get("second_name"),
                "now_cost_tenths": int(element["now_cost"]),
                "cost_change_start_tenths": int(element.get("cost_change_start") or 0),
                "status": element.get("status") or "a",
                # The API uses "" for no news; null reads better downstream, where
                # "is there news" is the actual question.
                "news": element.get("news") or None,
                "news_added": as_timestamp(element.get("news_added")),
                "chance_of_playing_this_round": element.get("chance_of_playing_this_round"),
                "chance_of_playing_next_round": element.get("chance_of_playing_next_round"),
                "minutes": int(element.get("minutes") or 0),
                "total_points": int(element.get("total_points") or 0),
                "form": as_numeric(element.get("form")),
                "points_per_game": as_numeric(element.get("points_per_game")),
                "selected_by_percent": as_numeric(element.get("selected_by_percent")),
                "expected_goals": as_numeric(element.get("expected_goals")),
                "expected_assists": as_numeric(element.get("expected_assists")),
                "expected_goal_involvements": as_numeric(
                    element.get("expected_goal_involvements")
                ),
                "expected_goals_conceded": as_numeric(
                    element.get("expected_goals_conceded")
                ),
                "updated_at": updated_at,
            }
        )

    return rows


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    with FPLClient() as client:
        payload = client.bootstrap_static()

    teams = build_team_rows(payload)
    gameweeks = build_gameweek_rows(payload)
    players = build_player_rows(payload)

    with db.connect() as conn:
        with db.run_logged(conn, "bootstrap", config.SEASON) as result:
            # Teams first: players carries a foreign key onto (season, fpl_id).
            written = db.upsert(conn, "teams", teams, ["season", "fpl_id"])
            log.info("teams: %d rows", written)
            result["rows"] += written

            written = db.upsert(conn, "gameweeks", gameweeks, ["season", "gw"])
            log.info("gameweeks: %d rows", written)
            result["rows"] += written

            written = db.upsert(conn, "players", players, ["season", "element_id"])
            log.info("players: %d rows", written)
            result["rows"] += written

    return 0


if __name__ == "__main__":
    sys.exit(main())
