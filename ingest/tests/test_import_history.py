"""Tests for the vaastav history import.

Offline: no network, no database. The samples under fixtures/history_sample/ are
real rows from vaastav/Fantasy-Premier-League, trimmed to a handful of players
(fixture `stats` blobs blanked). They were picked to include the cases that
silently corrupt a training set:

* 2024-25: Ødegaard's GW33 double (xP 8.1 repeated on both rows), Chalobah's
  loan out of Chelsea in GW4 and recall in GW22, an assistant manager
  (element_type 5), and GW22 / GW34 where the source's xP is 0 on every row.
* 2025-26: Guéhi's January move Palace -> Man City (GW23), Kroupi's exact
  duplicate rows, and GW7 where xP was never captured.

Salah is in both seasons under different element ids and team ids, which is the
point of `code`.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from import_history import (  # noqa: E402
    HISTORY_SEASONS,
    build_season,
    build_stat_rows,
    dedupe_gw_rows,
    fetch,
    guard_season,
    missing_xp_gameweeks,
    validate,
)

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "history_sample"

SALAH = 118748


@pytest.fixture(scope="module")
def seasons():
    return {
        season: build_season(fetch(season, SAMPLE, download=False))
        for season in ("2024-25", "2025-26")
    }


def all_rows(built):
    return [
        row
        for table in (built.teams, built.players, built.fixtures, built.stats)
        for row in table
    ]


def stat(built, element, gw):
    return [s for s in built.stats if s["element_id"] == element and s["gw"] == gw]


def team_at(built, row):
    """The player's own club for a stat row, derived exactly as a backtest would."""
    fixture = next(f for f in built.fixtures if f["fixture_id"] == row["fixture_id"])
    return fixture["team_h_fpl_id"] if row["was_home"] else fixture["team_a_fpl_id"]


# --- Season identity ---------------------------------------------------------


def test_every_row_carries_its_season(seasons):
    for season, built in seasons.items():
        rows = all_rows(built)
        assert rows
        assert {r["season"] for r in rows} == {season}


def test_live_season_is_never_produced(seasons):
    assert config.SEASON not in HISTORY_SEASONS
    for built in seasons.values():
        assert all(r["season"] != config.SEASON for r in all_rows(built))


def test_live_season_is_refused():
    with pytest.raises(ValueError, match="live season"):
        guard_season(config.SEASON)
    with pytest.raises(ValueError):
        guard_season("2022-23")
    with pytest.raises(ValueError):
        build_stat_rows(config.SEASON, [])


def test_code_links_a_player_across_seasons_despite_new_ids(seasons):
    salah = {
        season: next(p for p in built.players if p["code"] == SALAH)
        for season, built in seasons.items()
    }
    # Different element id and different club id each season — only code is stable.
    assert salah["2024-25"]["element_id"] == 328
    assert salah["2025-26"]["element_id"] == 381
    assert salah["2024-25"]["web_name"] == salah["2025-26"]["web_name"] == "M.Salah"


# --- Money --------------------------------------------------------------------


def test_prices_are_int_tenths(seasons):
    for built in seasons.values():
        for p in built.players:
            assert type(p["now_cost_tenths"]) is int
            assert type(p["cost_change_start_tenths"]) is int
        for s in built.stats:
            assert type(s["value_tenths"]) is int
            assert 30 <= s["value_tenths"] <= 200


def test_value_tenths_comes_from_the_row_not_players_raw(seasons):
    built = seasons["2024-25"]
    # players_raw's end-of-season now_cost for Salah was 136; his GW1 value was 125.
    gw1 = stat(built, 328, 1)
    assert [r["value_tenths"] for r in gw1] == [125]
    assert {r["value_tenths"] for r in built.stats if r["element_id"] == 328} != {136}


def test_selected_by_comes_from_the_row(seasons):
    built = seasons["2025-26"]
    (row,) = stat(built, 381, 1)
    assert row["selected_by"] == 5_221_323
    values = {r["selected_by"] for r in built.stats if r["element_id"] == 381}
    assert len(values) > 1  # ownership moves gameweek to gameweek


def test_players_money_is_first_listed_price_not_final(seasons):
    salah = next(p for p in seasons["2024-25"].players if p["element_id"] == 328)
    assert salah["now_cost_tenths"] == 125  # GW1 value, not the final 136
    assert salah["cost_change_start_tenths"] == 0


def test_players_rows_carry_no_end_of_season_performance(seasons):
    for built in seasons.values():
        for p in built.players:
            assert p["total_points"] == 0
            assert p["minutes"] == 0
            assert p["form"] is None
            assert p["points_per_game"] is None
            assert p["selected_by_percent"] is None
            assert p["expected_goals"] is None
            assert p["status"] == "a"


# --- fpl_xp, source, bonus ------------------------------------------------------


def test_fpl_xp_is_mapped_from_xp(seasons):
    (row,) = stat(seasons["2024-25"], 328, 1)
    assert row["fpl_xp"] == Decimal("5.0")
    (row,) = stat(seasons["2025-26"], 381, 2)
    assert row["fpl_xp"] == Decimal("7.0")


def test_uncaptured_xp_gameweek_is_null_not_zero(seasons):
    assert missing_xp_gameweeks(
        [{"GW": "7", "xP": "0.0"}, {"GW": "7", "xP": "0"}, {"GW": "8", "xP": "0.0"},
         {"GW": "8", "xP": "2.1"}]
    ) == {7}
    built = seasons["2025-26"]
    assert 7 in built.notes["gameweeks_missing_xp"]
    assert all(s["fpl_xp"] is None for s in built.stats if s["gw"] == 7)
    assert {22, 34} <= set(seasons["2024-25"].notes["gameweeks_missing_xp"])


def test_a_real_zero_xp_is_kept(seasons):
    # Kroupi GW1 2025-26: xP 0.0 in a gameweek that was captured.
    (row,) = stat(seasons["2025-26"], 100, 1)
    assert row["fpl_xp"] == Decimal("0.0")


def test_source_is_history_and_bonus_settled(seasons):
    for built in seasons.values():
        assert {s["source"] for s in built.stats} == {"history"}
        assert all(s["bonus_settled"] is True for s in built.stats)


# --- Doubles and blanks -------------------------------------------------------


def test_double_gameweek_produces_two_rows(seasons):
    rows = stat(seasons["2024-25"], 13, 33)
    assert sorted(r["fixture_id"] for r in rows) == [326, 331]
    # Per-fixture stats stay per fixture.
    assert sorted(r["minutes"] for r in rows) == [85, 90]


def test_double_gameweek_xp_is_not_double_counted(seasons):
    rows = stat(seasons["2024-25"], 13, 33)
    # xP 8.1 is a gameweek figure repeated on both source rows; it lands once.
    assert [r["fpl_xp"] for r in rows] == [Decimal("8.1"), None]
    assert sum(r["fpl_xp"] or 0 for r in rows) == Decimal("8.1")


def test_blank_produces_no_row():
    rows = build_stat_rows("2024-25", [])
    assert rows == []


def test_primary_keys_are_unique(seasons):
    for built in seasons.values():
        keys = [(s["element_id"], s["gw"], s["fixture_id"]) for s in built.stats]
        assert len(keys) == len(set(keys))


# --- Team at gameweek ---------------------------------------------------------


def test_team_at_gameweek_is_derivable_through_a_transfer(seasons):
    built = seasons["2025-26"]
    guehi = next(p for p in built.players if p["element_id"] == 260)
    # players.team_fpl_id is the END-OF-SEASON club (Man City, 13) ...
    assert guehi["team_fpl_id"] == 13
    # ... but before the January move the rows say Crystal Palace (8).
    assert team_at(built, stat(built, 260, 22)[0]) == 8
    assert team_at(built, stat(built, 260, 23)[0]) == 13


def test_team_at_gameweek_follows_a_loan_and_recall(seasons):
    built = seasons["2024-25"]
    assert team_at(built, stat(built, 159, 3)[0]) == 6  # Chelsea
    assert team_at(built, stat(built, 159, 4)[0]) == 7  # Crystal Palace, on loan
    assert team_at(built, stat(built, 159, 22)[0]) == 6  # recalled


def test_opponent_is_the_other_side_of_the_fixture(seasons):
    for built in seasons.values():
        fixtures = {f["fixture_id"]: f for f in built.fixtures}
        for s in built.stats:
            f = fixtures[s["fixture_id"]]
            sides = {f["team_h_fpl_id"], f["team_a_fpl_id"]}
            assert s["opponent_team_fpl_id"] in sides
            assert team_at(built, s) in sides
            assert team_at(built, s) != s["opponent_team_fpl_id"]


# --- Source quirks ------------------------------------------------------------


def test_exact_duplicate_rows_are_dropped(seasons):
    built = seasons["2025-26"]
    assert built.notes["duplicate_rows_dropped"] == 3
    assert len(stat(built, 100, 1)) == 1


def test_conflicting_duplicates_abort():
    a = {"element": "1", "GW": "1", "fixture": "1", "total_points": "2"}
    b = dict(a, total_points="9")
    with pytest.raises(ValueError, match="conflicting"):
        dedupe_gw_rows([a, b])


def test_assistant_managers_are_skipped(seasons):
    built = seasons["2024-25"]
    assert built.notes["non_player_elements_skipped"] == 1
    assert all(p["element_type"] in {1, 2, 3, 4} for p in built.players)
    assert not any(s["element_id"] == 735 for s in built.stats)


def test_defensive_contribution_defaults_before_it_existed(seasons):
    assert all(s["defensive_contribution"] == 0 for s in seasons["2024-25"].stats)
    assert any(s["defensive_contribution"] > 0 for s in seasons["2025-26"].stats)


def test_validate_catches_a_stat_row_for_an_unknown_player(seasons):
    built = seasons["2025-26"]
    stray = dict(built.stats[0], element_id=99999)
    problems = validate(
        built.season, built.teams, built.players, built.fixtures, [*built.stats, stray]
    )
    assert any("not in players" in p for p in problems)


def test_validate_catches_a_wrong_season(seasons):
    built = seasons["2025-26"]
    stray = dict(built.stats[0], season=config.SEASON)
    problems = validate(
        built.season, built.teams, built.players, built.fixtures, [*built.stats, stray]
    )
    assert any("season other than" in p for p in problems)
