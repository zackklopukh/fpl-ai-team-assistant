"""Tests for the history backfill and the daily price snapshot.

Offline: no network, no database. Both scripts are a pure transform plus a thin
I/O wrapper, and the transform is what is worth guarding.

The test that matters most is the one asserting a history row's own `value` lands
in `value_tenths`. Sourcing that column from today's price instead is a silent
bug — the job still runs, the table still fills, and every backtest number
produced from it is wrong in a direction that flatters the model.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

INGEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INGEST))

from backfill_history import (  # noqa: E402
    elements_to_walk,
    filter_new_rows,
    history_rows,
    settled_gameweeks,
)
from snapshot_prices import price_rows  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SEASON = "2026-27"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def history_entry(**overrides):
    """One `element-summary` history row, with the fields the loader reads."""
    row = {
        "element": 1,
        "fixture": 101,
        "opponent_team": 7,
        "total_points": 8,
        "was_home": True,
        "round": 3,
        "minutes": 90,
        "starts": 1,
        "goals_scored": 1,
        "assists": 0,
        "clean_sheets": 1,
        "goals_conceded": 0,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "saves": 0,
        "bonus": 2,
        "bps": 34,
        "defensive_contribution": 5,
        "expected_goals": "0.42",
        "expected_assists": "0.11",
        "expected_goal_involvements": "0.53",
        "expected_goals_conceded": "0.88",
        "influence": "40.2",
        "creativity": "12.6",
        "threat": "31.0",
        "ict_index": "8.4",
        "value": 74,
        "selected": 1_204_551,
        "transfers_in": 10,
        "transfers_out": 3,
    }
    row.update(overrides)
    return row


class TestHistoricalPriceAndOwnership:
    def test_value_goes_to_value_tenths_as_an_integer(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(value=74)]})
        assert rows[0]["value_tenths"] == 74
        assert isinstance(rows[0]["value_tenths"], int)

    def test_selected_goes_to_selected_by(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(selected=1_204_551)]})
        assert rows[0]["selected_by"] == 1_204_551

    def test_the_gameweek_price_is_not_the_current_price(self):
        # The leak this whole job exists to avoid. The player cost 74 in GW3 and
        # costs 81 today; the GW3 row must say 74. Nothing in the transform is
        # allowed to reach into a players row or a bootstrap payload for this.
        summary = {"history": [history_entry(round=3, value=74)]}
        rows = history_rows(SEASON, 1, summary)

        current_price_today = 81
        assert rows[0]["value_tenths"] == 74
        assert rows[0]["value_tenths"] != current_price_today

    def test_each_gameweek_keeps_its_own_price(self):
        summary = {
            "history": [
                history_entry(round=1, fixture=1, value=70),
                history_entry(round=2, fixture=12, value=71),
                history_entry(round=3, fixture=25, value=74),
            ]
        }
        rows = history_rows(SEASON, 1, summary)
        assert [(r["gw"], r["value_tenths"]) for r in rows] == [(1, 70), (2, 71), (3, 74)]

    def test_a_null_value_stays_null_rather_than_becoming_zero(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(value=None)]})
        assert rows[0]["value_tenths"] is None


class TestHistoryRows:
    def test_keys_are_season_element_gw_fixture(self):
        rows = history_rows(SEASON, 42, {"history": [history_entry(round=3, fixture=101)]})
        assert (rows[0]["season"], rows[0]["element_id"], rows[0]["gw"], rows[0]["fixture_id"]) == (
            SEASON,
            42,
            3,
            101,
        )

    def test_a_double_gameweek_produces_two_rows(self):
        # Same gw, two fixtures. Anything that collapsed this to one row would
        # lose half a player's returns in the weeks that matter most.
        summary = {
            "history": [
                history_entry(round=25, fixture=300, total_points=6),
                history_entry(round=25, fixture=301, total_points=9),
            ]
        }
        rows = history_rows(SEASON, 1, summary)
        assert len(rows) == 2
        assert {r["fixture_id"] for r in rows} == {300, 301}
        assert {r["gw"] for r in rows} == {25}

    def test_a_blank_gameweek_is_an_absent_row_not_a_zero_row(self):
        summary = {"history": [history_entry(round=1, fixture=1), history_entry(round=3, fixture=25)]}
        rows = history_rows(SEASON, 1, summary)
        assert [r["gw"] for r in rows] == [1, 3]

    def test_string_numerics_become_decimals_not_floats(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(expected_goals="0.42")]})
        assert rows[0]["expected_goals"] == Decimal("0.42")
        assert not isinstance(rows[0]["expected_goals"], float)

    def test_counting_stats_default_to_zero_when_absent(self):
        entry = history_entry()
        del entry["defensive_contribution"]
        rows = history_rows(SEASON, 1, {"history": [entry]})
        assert rows[0]["defensive_contribution"] == 0

    def test_opponent_and_home_flag_are_carried(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(opponent_team=7, was_home=False)]})
        assert rows[0]["opponent_team_fpl_id"] == 7
        assert rows[0]["was_home"] is False

    def test_a_row_without_a_round_or_fixture_is_skipped(self):
        # Both are primary key columns, so a row missing either cannot be stored.
        summary = {"history": [history_entry(round=None), history_entry(fixture=None)]}
        assert history_rows(SEASON, 1, summary) == []

    def test_an_empty_history_is_not_an_error(self):
        assert history_rows(SEASON, 1, {"history": []}) == []
        assert history_rows(SEASON, 1, {}) == []

    def test_every_row_has_identical_columns_in_identical_order(self):
        # db.upsert requires this and raises otherwise, so catch it here instead
        # of eight minutes into a backfill.
        summary = {
            "history": [
                history_entry(round=1, fixture=1),
                history_entry(round=2, fixture=2, expected_goals=None, value=None),
            ]
        }
        rows = history_rows(SEASON, 1, summary)
        assert list(rows[0].keys()) == list(rows[1].keys())


class TestBonusSettled:
    def test_a_checked_gameweek_is_marked_settled(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(round=3)]}, {1, 2, 3})
        assert rows[0]["bonus_settled"] is True

    def test_an_unchecked_gameweek_stays_rewritable(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(round=4)]}, {1, 2, 3})
        assert rows[0]["bonus_settled"] is False

    def test_settled_gameweeks_reads_data_checked_from_bootstrap(self):
        bootstrap = {
            "events": [
                {"id": 1, "data_checked": True},
                {"id": 2, "data_checked": True},
                {"id": 3, "data_checked": False},
            ]
        }
        assert settled_gameweeks(bootstrap) == frozenset({1, 2})

    def test_the_real_bootstrap_fixture_reports_settled_gameweeks(self):
        settled = settled_gameweeks(load("bootstrap_static"))
        assert settled
        assert all(isinstance(gw, int) for gw in settled)


class TestResumability:
    def test_rows_already_written_are_skipped(self):
        summary = {
            "history": [
                history_entry(round=1, fixture=1),
                history_entry(round=2, fixture=12),
                history_entry(round=3, fixture=25),
            ]
        }
        rows = history_rows(SEASON, 1, summary)
        existing = {(1, 1, 1), (1, 2, 12)}

        remaining = filter_new_rows(rows, existing)
        assert [r["gw"] for r in remaining] == [3]

    def test_a_fully_written_player_costs_no_writes(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(round=1, fixture=1)]})
        assert filter_new_rows(rows, {(1, 1, 1)}) == []

    def test_one_leg_of_a_double_can_be_missing(self):
        # Interrupted mid-player: fixture 300 landed, 301 did not.
        summary = {
            "history": [
                history_entry(round=25, fixture=300),
                history_entry(round=25, fixture=301),
            ]
        }
        rows = filter_new_rows(history_rows(SEASON, 1, summary), {(1, 25, 300)})
        assert [r["fixture_id"] for r in rows] == [301]

    def test_another_players_rows_do_not_mask_this_one(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry(round=1, fixture=1)]})
        assert len(filter_new_rows(rows, {(2, 1, 1)})) == 1

    def test_nothing_written_yet_means_everything_is_new(self):
        rows = history_rows(SEASON, 1, {"history": [history_entry()]})
        assert filter_new_rows(rows, set()) == rows

    def test_from_element_resumes_in_ascending_order(self):
        bootstrap = {"elements": [{"id": 5}, {"id": 1}, {"id": 3}, {"id": 9}]}
        assert elements_to_walk(bootstrap, 0) == [1, 3, 5, 9]
        assert elements_to_walk(bootstrap, 3) == [3, 5, 9]

    def test_from_element_is_inclusive_so_a_half_written_player_is_redone(self):
        # The row filter makes redoing the element that failed cheap, and picking
        # up *after* it would lose whatever it had not finished writing.
        assert elements_to_walk({"elements": [{"id": 3}, {"id": 4}]}, 3) == [3, 4]

    def test_the_real_bootstrap_fixture_walks_in_order(self):
        elements = elements_to_walk(load("bootstrap_static"))
        assert elements == sorted(elements)
        assert len(set(elements)) == len(elements)


class TestPriceSnapshot:
    def test_one_row_per_element(self):
        bootstrap = load("bootstrap_static")
        rows = price_rows(SEASON, bootstrap, dt.date(2026, 9, 17))
        assert len(rows) == len(bootstrap["elements"])

    def test_the_key_is_season_element_and_date(self):
        rows = price_rows(SEASON, load("bootstrap_static"), dt.date(2026, 9, 17))
        keys = [(r["season"], r["element_id"], r["as_of_date"]) for r in rows]
        assert len(set(keys)) == len(keys)
        assert keys[0][0] == SEASON
        assert keys[0][2] == dt.date(2026, 9, 17)

    def test_cost_is_now_cost_in_tenths_untouched(self):
        bootstrap = {
            "elements": [
                {
                    "id": 1,
                    "now_cost": 145,
                    "cost_change_event": -1,
                    "selected_by_percent": "40.9",
                    "transfers_in_event": 12,
                    "transfers_out_event": 7,
                }
            ]
        }
        row = price_rows(SEASON, bootstrap, dt.date(2026, 9, 17))[0]
        assert row["cost_tenths"] == 145
        assert isinstance(row["cost_tenths"], int)
        assert row["cost_change_event_tenths"] == -1

    def test_no_money_column_is_ever_a_float(self):
        rows = price_rows(SEASON, load("bootstrap_static"), dt.date(2026, 9, 17))
        for row in rows:
            assert isinstance(row["cost_tenths"], int)
            assert isinstance(row["cost_change_event_tenths"], int)

    def test_ownership_is_a_decimal(self):
        rows = price_rows(SEASON, load("bootstrap_static"), dt.date(2026, 9, 17))
        assert isinstance(rows[0]["selected_by_percent"], Decimal)

    def test_a_missing_cost_change_defaults_to_zero(self):
        rows = price_rows(SEASON, {"elements": [{"id": 1, "now_cost": 50}]}, dt.date(2026, 9, 17))
        assert rows[0]["cost_change_event_tenths"] == 0
        assert rows[0]["transfers_in_event"] is None

    def test_rows_share_a_column_order(self):
        rows = price_rows(SEASON, load("bootstrap_static"), dt.date(2026, 9, 17))
        assert all(list(r.keys()) == list(rows[0].keys()) for r in rows)

    def test_a_same_day_rerun_produces_identical_keys(self):
        # Which is what makes the (season, element_id, as_of_date) upsert an
        # overwrite rather than a second row.
        bootstrap = load("bootstrap_static")
        day = dt.date(2026, 9, 17)
        first = price_rows(SEASON, bootstrap, day)
        second = price_rows(SEASON, bootstrap, day)
        assert [(r["season"], r["element_id"], r["as_of_date"]) for r in first] == [
            (r["season"], r["element_id"], r["as_of_date"]) for r in second
        ]


@pytest.mark.parametrize("value", [0, 39, 145])
def test_prices_survive_the_round_trip_as_integers(value):
    rows = price_rows(SEASON, {"elements": [{"id": 1, "now_cost": value}]}, dt.date(2026, 9, 17))
    assert rows[0]["cost_tenths"] == value
