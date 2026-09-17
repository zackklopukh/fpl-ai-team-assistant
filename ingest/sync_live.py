"""Sync `event/{gw}/live/` into `player_gw_stats`.

Runs every 15 minutes while matches are on. This is the table the xP model trains
from, so every row has to describe the gameweek it belongs to and nothing else —
the price and ownership written here are snapshotted now, not looked up later.

Three things shape the transform:

* **One row per fixture, not per gameweek.** The key is
  (season, element_id, gw, fixture_id), so a player in a double gameweek produces
  two rows. The live payload's `stats` block is the gameweek *total*; the
  per-fixture split comes from `explain`, which carries a fixture id.
* **Bonus is provisional.** Until the gameweek's `data_checked` flips true, the
  bonus in this payload is the live BPS projection and will change. `bonus_settled`
  records which it is, and because every write is an upsert on the natural key, the
  run after settlement simply rewrites the row.
* **was_home / opponent are not in this payload.** They are derived by joining the
  fixture id back to that gameweek's fixtures, which is why this job reads
  fixtures as well.

The script calls three endpoints, all through FPLClient: bootstrap-static (for
the current gameweek, its data_checked flag, and the price/ownership snapshot),
fixtures for that gameweek, and the live payload itself.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal

import config
import db
from fpl_client import FPLClient
from sync_bootstrap import _now, as_numeric

log = logging.getLogger(__name__)

# Counted stats that `explain` reports per fixture, so a double gameweek splits
# them correctly. Names match the schema's columns exactly.
COUNTED_STATS = (
    "minutes",
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
)

# Reported for the gameweek as a whole and absent from `explain`. In a double they
# go on the first fixture's row only, so summing a player's rows still gives the
# gameweek total rather than double-counting it.
GAMEWEEK_ONLY_NUMERIC = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "influence",
    "creativity",
    "threat",
    "ict_index",
)


def current_gameweek(bootstrap: dict) -> dict | None:
    """The gameweek live scoring currently applies to.

    None between seasons and in the gap after the last gameweek is checked, which
    is a reason to do nothing rather than an error.
    """
    for event in bootstrap["events"]:
        if event.get("is_current"):
            return event
    return None


def snapshot_context(bootstrap: dict) -> dict[int, dict]:
    """element_id -> the team, price and ownership to stamp on this gameweek's rows.

    Price is already in tenths. Ownership is published as a percentage, not a
    count, so the count is reconstructed from `total_players` — approximate at the
    last digit, and the only form of it the API offers.
    """
    total_players = bootstrap.get("total_players") or 0
    context = {}

    for element in bootstrap["elements"]:
        percent = as_numeric(element.get("selected_by_percent")) or Decimal(0)
        context[element["id"]] = {
            "team_fpl_id": element["team"],
            "value_tenths": int(element["now_cost"]),
            "selected_by": int(percent * total_players / 100),
        }

    return context


def fixture_sides(fixtures: list[dict]) -> dict[int, tuple[int, int]]:
    """fixture_id -> (home team, away team), for deriving was_home and opponent."""
    return {fixture["id"]: (fixture["team_h"], fixture["team_a"]) for fixture in fixtures}


def build_stat_rows(
    live: dict,
    gw: int,
    sides: dict[int, tuple[int, int]],
    context: dict[int, dict],
    data_checked: bool,
    season: str = config.SEASON,
) -> list[dict]:
    """live elements[] -> `player_gw_stats` rows, one per player per fixture.

    A player whose team did not play has no `explain` entry and produces no row.
    The schema comment offers a null fixture_id for a blank, but fixture_id is part
    of the primary key and Postgres will not accept a null there — and a blank has
    no stats to record anyway.
    """
    updated_at = _now()
    rows: list[dict] = []

    for element in live.get("elements", []):
        element_id = element["id"]
        stats = element.get("stats") or {}
        explains = [e for e in (element.get("explain") or []) if e.get("fixture")]

        if not explains:
            continue

        info = context.get(element_id) or {}
        team = info.get("team_fpl_id")
        # With one fixture, `stats` already is that fixture's line and is the more
        # complete of the two. Only a double needs splitting out of `explain`.
        single = len(explains) == 1

        for index, explain in enumerate(explains):
            fixture_id = explain["fixture"]
            home, away = sides.get(fixture_id, (None, None))

            was_home = None
            opponent = None
            if team is not None and home is not None:
                was_home = team == home
                opponent = away if was_home else home

            if single:
                counted = {key: int(stats.get(key) or 0) for key in COUNTED_STATS}
                total_points = int(stats.get("total_points") or 0)
            else:
                by_identifier = {
                    item["identifier"]: item for item in explain.get("stats") or []
                }
                counted = {
                    key: int((by_identifier.get(key) or {}).get("value") or 0)
                    for key in COUNTED_STATS
                }
                total_points = sum(
                    int(item.get("points") or 0) for item in explain.get("stats") or []
                )

            first = index == 0
            aggregates = {
                key: as_numeric(stats.get(key)) if first else None
                for key in GAMEWEEK_ONLY_NUMERIC
            }

            rows.append(
                {
                    "season": season,
                    "element_id": element_id,
                    "gw": gw,
                    "fixture_id": fixture_id,
                    "minutes": counted["minutes"],
                    "total_points": total_points,
                    # Not in `explain`, so it lands on the first row of a double.
                    "starts": int(stats.get("starts") or 0) if first else 0,
                    "goals_scored": counted["goals_scored"],
                    "assists": counted["assists"],
                    "clean_sheets": counted["clean_sheets"],
                    "goals_conceded": counted["goals_conceded"],
                    "own_goals": counted["own_goals"],
                    "penalties_saved": counted["penalties_saved"],
                    "penalties_missed": counted["penalties_missed"],
                    "yellow_cards": counted["yellow_cards"],
                    "red_cards": counted["red_cards"],
                    "saves": counted["saves"],
                    "bonus": counted["bonus"],
                    "bps": counted["bps"],
                    "defensive_contribution": counted["defensive_contribution"],
                    "expected_goals": aggregates["expected_goals"],
                    "expected_assists": aggregates["expected_assists"],
                    "expected_goal_involvements": aggregates[
                        "expected_goal_involvements"
                    ],
                    "expected_goals_conceded": aggregates["expected_goals_conceded"],
                    "influence": aggregates["influence"],
                    "creativity": aggregates["creativity"],
                    "threat": aggregates["threat"],
                    "ict_index": aggregates["ict_index"],
                    "was_home": was_home,
                    "opponent_team_fpl_id": opponent,
                    "value_tenths": info.get("value_tenths"),
                    "selected_by": info.get("selected_by"),
                    "bonus_settled": data_checked,
                    "updated_at": updated_at,
                }
            )

    return rows


def main(gw: int | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    with FPLClient() as client:
        bootstrap = client.bootstrap_static()

        if gw is None:
            event = current_gameweek(bootstrap)
            if event is None:
                log.info("no current gameweek — nothing to sync")
                return 0
        else:
            event = next(e for e in bootstrap["events"] if e["id"] == gw)

        gw = event["id"]
        data_checked = bool(event.get("data_checked"))

        fixtures = client.fixtures(event=gw)
        live = client.event_live(gw)

    rows = build_stat_rows(
        live,
        gw=gw,
        sides=fixture_sides(fixtures),
        context=snapshot_context(bootstrap),
        data_checked=data_checked,
    )

    with db.connect() as conn:
        with db.run_logged(conn, "live", config.SEASON) as result:
            written = db.upsert(
                conn,
                "player_gw_stats",
                rows,
                ["season", "element_id", "gw", "fixture_id"],
            )
            log.info(
                "player_gw_stats: %d rows for gw %d (bonus %s)",
                written,
                gw,
                "settled" if data_checked else "provisional",
            )
            result["rows"] = written

    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
