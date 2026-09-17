"""Tests for the API-payload-to-row transforms.

Offline: no network, no database. The saved payloads in tests/fixtures/ are real
responses (bootstrap trimmed to 60 players, every field shape intact), and the
transforms are pure functions over them, which is the whole reason they are split
out of `main`.

What is worth guarding here is the quiet stuff: a price that becomes a float, a
team id written into the element_type column, a double gameweek collapsing into
one row. None of those raises — they just produce a table that is wrong.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync_bootstrap import (  # noqa: E402
    build_gameweek_rows,
    build_player_rows,
    build_team_rows,
)
from sync_fixtures import build_fixture_rows  # noqa: E402
from sync_live import (  # noqa: E402
    build_stat_rows,
    current_gameweek,
    fixture_sides,
    snapshot_context,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def bootstrap():
    return load("bootstrap_static")


@pytest.fixture(scope="module")
def fixtures_payload():
    return load("fixtures")


def by_key(rows, key, value):
    return next(row for row in rows if row[key] == value)


class TestTeamRows:
    def test_api_id_becomes_fpl_id_and_code_is_kept_separately(self, bootstrap):
        rows = build_team_rows(bootstrap, season="2026-27")
        arsenal = by_key(rows, "fpl_id", 1)

        # fpl_id is reassigned each season; code is the stable one. Conflating
        # them is how last season's Arsenal becomes this season's Aston Villa.
        assert arsenal["fpl_id"] == 1
        assert arsenal["code"] == 3
        assert arsenal["name"] == "Arsenal"
        assert arsenal["short_name"] == "ARS"

    def test_every_row_carries_the_season(self, bootstrap):
        rows = build_team_rows(bootstrap, season="2026-27")
        assert len(rows) == 20
        assert {row["season"] for row in rows} == {"2026-27"}

    def test_a_null_strength_is_carried_not_defaulted(self, bootstrap):
        # Preseason payloads leave these null and fill them in later.
        rows = build_team_rows(bootstrap, season="2026-27")
        assert by_key(rows, "fpl_id", 1)["strength"] is None
        assert by_key(rows, "fpl_id", 1)["strength_overall_home"] == 4

    def test_no_column_outside_the_schema(self, bootstrap):
        allowed = {
            "season", "fpl_id", "code", "name", "short_name", "strength",
            "strength_overall_home", "strength_overall_away",
            "strength_attack_home", "strength_attack_away",
            "strength_defence_home", "strength_defence_away", "updated_at",
        }
        assert set(build_team_rows(bootstrap)[0]) == allowed


class TestGameweekRows:
    def test_event_id_becomes_gw_and_the_deadline_is_a_utc_datetime(self, bootstrap):
        rows = build_gameweek_rows(bootstrap, season="2026-27")
        gw1 = by_key(rows, "gw", 1)

        assert gw1["name"] == "Gameweek 1"
        assert isinstance(gw1["deadline_time"], datetime)
        assert gw1["deadline_time"] == datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)

    def test_flags_are_booleans_not_the_raw_payload_values(self, bootstrap):
        rows = build_gameweek_rows(bootstrap)
        for row in rows:
            for flag in ("is_current", "is_next", "is_previous", "finished", "data_checked"):
                assert isinstance(row[flag], bool)

    def test_thirty_eight_gameweeks(self, bootstrap):
        assert len(build_gameweek_rows(bootstrap)) == 38


class TestPlayerRows:
    def test_money_is_integer_tenths(self, bootstrap):
        rows = build_player_rows(bootstrap)
        raya = by_key(rows, "element_id", 1)

        # 60 means £6.0m. A float here is the invariant this project is most
        # likely to break silently.
        assert raya["now_cost_tenths"] == 60
        assert isinstance(raya["now_cost_tenths"], int)
        assert isinstance(raya["cost_change_start_tenths"], int)

        for row in rows:
            assert isinstance(row["now_cost_tenths"], int)
            assert not isinstance(row["now_cost_tenths"], bool)

    def test_team_and_position_land_in_the_right_columns(self, bootstrap):
        rows = build_player_rows(bootstrap)
        raya = by_key(rows, "element_id", 1)

        # The API's `team` is the schema's team_fpl_id, and `element_type` is the
        # position. Both are small ints, so a swap would never raise.
        assert raya["team_fpl_id"] == 1
        assert raya["element_type"] == 1  # GKP
        assert raya["code"] == 154561
        assert raya["web_name"] == "Raya"

    def test_element_types_stay_inside_the_four_positions(self, bootstrap):
        rows = build_player_rows(bootstrap)
        assert {row["element_type"] for row in rows} <= {1, 2, 3, 4}
        assert {row["team_fpl_id"] for row in rows} <= set(range(1, 21))

    def test_string_numerics_become_decimals(self, bootstrap):
        raya = by_key(build_player_rows(bootstrap), "element_id", 1)

        assert raya["form"] == Decimal("7.2")
        assert raya["expected_goals_conceded"] == Decimal("2.72")
        assert isinstance(raya["selected_by_percent"], Decimal)

    def test_empty_news_becomes_null(self, bootstrap):
        raya = by_key(build_player_rows(bootstrap), "element_id", 1)
        assert raya["news"] is None
        assert raya["news_added"] is None

    def test_the_generated_full_name_column_is_never_written(self, bootstrap):
        assert "full_name" not in build_player_rows(bootstrap)[0]

    def test_every_row_has_the_same_columns(self, bootstrap):
        # db.upsert builds one statement from the first row and rejects the rest
        # if their keys differ, so this is a real failure mode.
        rows = build_player_rows(bootstrap)
        columns = list(rows[0].keys())
        assert all(list(row.keys()) == columns for row in rows)

    def test_season_is_on_every_row(self, bootstrap):
        rows = build_player_rows(bootstrap, season="2026-27")
        assert {row["season"] for row in rows} == {"2026-27"}
        # One row per element in the fixture, whatever the fixture's size — it is
        # a sample and gets regenerated.
        assert len(rows) == len(bootstrap["elements"])


class TestFixtureRows:
    def test_event_becomes_gw_and_teams_become_fpl_ids(self, fixtures_payload):
        rows = build_fixture_rows(fixtures_payload, season="2026-27")
        first = by_key(rows, "fixture_id", 1)

        assert first["gw"] == 1
        assert first["team_h_fpl_id"] == 1
        assert first["team_a_fpl_id"] == 7
        assert first["team_h_difficulty"] == 2
        assert first["team_a_difficulty"] == 5
        assert first["team_h_score"] == 3
        assert first["kickoff_time"] == datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)

    def test_an_unscheduled_fixture_keeps_a_null_gw(self):
        # A postponed match waiting on a cup replay: no event, no kickoff, and
        # started/finished come back null rather than false.
        payload = [
            {
                "id": 999,
                "event": None,
                "team_h": 3,
                "team_a": 11,
                "team_h_difficulty": 3,
                "team_a_difficulty": 2,
                "kickoff_time": None,
                "started": None,
                "finished": False,
                "finished_provisional": False,
                "minutes": 0,
                "team_h_score": None,
                "team_a_score": None,
            }
        ]
        row = build_fixture_rows(payload)[0]

        assert row["gw"] is None
        assert row["kickoff_time"] is None
        # The schema declares these not-null, so a null from the API must coerce.
        assert row["started"] is False
        assert row["minutes"] == 0
        assert row["team_h_score"] is None

    def test_a_double_gameweek_is_two_rows_for_the_same_team(self):
        payload = [
            {"id": 1, "event": 24, "team_h": 5, "team_a": 9, "minutes": 0},
            {"id": 2, "event": 24, "team_h": 12, "team_a": 5, "minutes": 0},
        ]
        rows = build_fixture_rows(payload)

        appearances = [
            row for row in rows
            if 5 in (row["team_h_fpl_id"], row["team_a_fpl_id"]) and row["gw"] == 24
        ]
        assert len(appearances) == 2

    def test_no_column_outside_the_schema(self, fixtures_payload):
        allowed = {
            "season", "fixture_id", "gw", "team_h_fpl_id", "team_a_fpl_id",
            "team_h_difficulty", "team_a_difficulty", "kickoff_time", "started",
            "finished", "finished_provisional", "minutes", "team_h_score",
            "team_a_score", "updated_at",
        }
        assert set(build_fixture_rows(fixtures_payload)[0]) == allowed


def explain_entry(fixture_id, **values):
    """One `explain` block, in the shape the live endpoint returns it."""
    def scored(identifier, value):
        if identifier == "minutes":
            return 2 if value >= 60 else (1 if value else 0)
        per_unit = {"goals_scored": 4, "assists": 3, "bonus": 1}
        return per_unit.get(identifier, 0) * (value or 0)

    return {
        "fixture": fixture_id,
        "stats": [
            {"identifier": identifier, "value": value, "points": scored(identifier, value)}
            for identifier, value in values.items()
        ],
    }


def live_element(element_id, stats, explains):
    return {"id": element_id, "stats": stats, "explain": explains}


SINGLE_STATS = {
    "minutes": 90,
    "goals_scored": 1,
    "assists": 0,
    "clean_sheets": 1,
    "goals_conceded": 0,
    "own_goals": 0,
    "penalties_saved": 0,
    "penalties_missed": 0,
    "yellow_cards": 1,
    "red_cards": 0,
    "saves": 0,
    "bonus": 3,
    "bps": 41,
    "starts": 1,
    "defensive_contribution": 8,
    "expected_goals": "0.42",
    "expected_assists": "0.11",
    "expected_goal_involvements": "0.53",
    "expected_goals_conceded": "0.77",
    "influence": "54.2",
    "creativity": "20.1",
    "threat": "31.0",
    "ict_index": "10.5",
    "total_points": 11,
    "in_dreamteam": False,
}

CONTEXT = {
    10: {"team_fpl_id": 5, "value_tenths": 78, "selected_by": 1_200_000},
}

SIDES = {101: (5, 9), 102: (12, 5)}


class TestLiveStatRows:
    def test_a_single_fixture_produces_one_row_from_the_stats_block(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(101, minutes=90)])
            ]
        }
        rows = build_stat_rows(
            live, gw=7, sides=SIDES, context=CONTEXT, data_checked=False, season="2026-27"
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["season"] == "2026-27"
        assert (row["element_id"], row["gw"], row["fixture_id"]) == (10, 7, 101)
        assert row["minutes"] == 90
        assert row["total_points"] == 11
        assert row["bonus"] == 3
        assert row["bps"] == 41
        assert row["defensive_contribution"] == 8
        assert row["expected_goals"] == Decimal("0.42")
        assert row["ict_index"] == Decimal("10.5")

    def test_home_away_and_opponent_come_from_the_fixture(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(101, minutes=90)])
            ]
        }
        row = build_stat_rows(live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True)[0]

        # Team 5 is the home side in fixture 101.
        assert row["was_home"] is True
        assert row["opponent_team_fpl_id"] == 9

    def test_the_away_fixture_flips_both(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(102, minutes=90)])
            ]
        }
        row = build_stat_rows(live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True)[0]

        assert row["was_home"] is False
        assert row["opponent_team_fpl_id"] == 12

    def test_a_double_gameweek_produces_one_row_per_fixture(self):
        # Two matches: a goal at home, a blank away. The `stats` block is the
        # gameweek total; only `explain` knows which match was which.
        stats = dict(SINGLE_STATS, minutes=160, goals_scored=1, bonus=3, bps=55, total_points=11)
        live = {
            "elements": [
                live_element(
                    10,
                    stats,
                    [
                        explain_entry(101, minutes=90, goals_scored=1, bonus=3),
                        explain_entry(102, minutes=70, goals_scored=0, bonus=0),
                    ],
                )
            ]
        }
        rows = build_stat_rows(live, gw=24, sides=SIDES, context=CONTEXT, data_checked=True)

        assert len(rows) == 2
        assert [row["fixture_id"] for row in rows] == [101, 102]

        home, away = rows
        assert (home["minutes"], home["goals_scored"], home["bonus"]) == (90, 1, 3)
        assert (away["minutes"], away["goals_scored"], away["bonus"]) == (70, 0, 0)
        assert home["was_home"] is True and home["opponent_team_fpl_id"] == 9
        assert away["was_home"] is False and away["opponent_team_fpl_id"] == 12

        # Per-fixture points: 2 for the 90 minutes, 4 for the goal, 3 bonus.
        assert home["total_points"] == 2 + 4 + 3
        assert away["total_points"] == 2
        assert home["total_points"] + away["total_points"] == stats["total_points"]

        # Minutes split rather than being repeated on both rows.
        assert home["minutes"] + away["minutes"] == stats["minutes"]

    def test_gameweek_only_stats_sit_on_the_first_row_of_a_double(self):
        # xG and the ICT family have no per-fixture split in the payload. Putting
        # them on both rows would double the totals in every downstream sum.
        live = {
            "elements": [
                live_element(
                    10,
                    SINGLE_STATS,
                    [explain_entry(101, minutes=90), explain_entry(102, minutes=70)],
                )
            ]
        }
        home, away = build_stat_rows(
            live, gw=24, sides=SIDES, context=CONTEXT, data_checked=True
        )

        assert home["expected_goals"] == Decimal("0.42")
        assert away["expected_goals"] is None
        assert home["starts"] == 1
        assert away["starts"] == 0

    def test_bonus_settled_follows_data_checked(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(101, minutes=90)])
            ]
        }

        provisional = build_stat_rows(
            live, gw=7, sides=SIDES, context=CONTEXT, data_checked=False
        )[0]
        settled = build_stat_rows(
            live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True
        )[0]

        assert provisional["bonus_settled"] is False
        assert settled["bonus_settled"] is True
        # Same key both times, so the settled run rewrites the provisional row.
        key = ("season", "element_id", "gw", "fixture_id")
        assert [provisional[k] for k in key] == [settled[k] for k in key]

    def test_price_and_ownership_are_snapshotted_from_the_context(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(101, minutes=90)])
            ]
        }
        row = build_stat_rows(live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True)[0]

        # Written now so a backtest reads the price during that gameweek rather
        # than reaching into today's players row.
        assert row["value_tenths"] == 78
        assert isinstance(row["value_tenths"], int)
        assert row["selected_by"] == 1_200_000

    def test_a_blank_gameweek_produces_no_row(self):
        # fixture_id is part of the primary key, so there is no null to write.
        live = {"elements": [live_element(10, dict(SINGLE_STATS, minutes=0), [])]}
        assert build_stat_rows(live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True) == []

    def test_every_row_has_the_same_columns(self):
        live = {
            "elements": [
                live_element(10, SINGLE_STATS, [explain_entry(101, minutes=90)]),
                live_element(
                    10,
                    SINGLE_STATS,
                    [explain_entry(101, minutes=90), explain_entry(102, minutes=70)],
                ),
            ]
        }
        rows = build_stat_rows(live, gw=7, sides=SIDES, context=CONTEXT, data_checked=True)
        columns = list(rows[0].keys())
        assert all(list(row.keys()) == columns for row in rows)


class TestLiveContext:
    def test_current_gameweek_is_the_one_flagged_current(self, bootstrap):
        events = [
            {"id": 1, "is_current": False},
            {"id": 2, "is_current": True},
            {"id": 3, "is_current": False},
        ]
        assert current_gameweek({"events": events})["id"] == 2

    def test_no_current_gameweek_is_none_not_an_error(self):
        # True between seasons, when this job must do nothing rather than fail.
        assert current_gameweek({"events": [{"id": 1, "is_current": False}]}) is None

    def test_snapshot_context_carries_team_price_and_ownership(self, bootstrap):
        context = snapshot_context(bootstrap)
        raya = context[1]

        assert raya["team_fpl_id"] == 1
        assert raya["value_tenths"] == 60
        assert isinstance(raya["value_tenths"], int)
        # 40.9% of total_players, reconstructed because FPL publishes no count.
        expected = int(Decimal("40.9") * bootstrap["total_players"] / 100)
        assert raya["selected_by"] == expected

    def test_fixture_sides_maps_id_to_both_teams(self, fixtures_payload):
        sides = fixture_sides(fixtures_payload)
        assert sides[1] == (1, 7)
        assert len(sides) == len(fixtures_payload)
