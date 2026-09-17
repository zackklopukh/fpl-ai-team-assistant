"""Tests for squad reconstruction from public payloads.

Offline. The fixtures are real data for team 895045 as of GW4, which is the point:
the reconstruction either works on a real transfer log or it does not, and a
hand-built payload would not tell us.

One thing deliberately *not* asserted: an exact match against the 1008 FPL
reported at that deadline. The reconstruction values the squad at today's prices
and the fixture was captured five days later, so the few tenths of difference are
price movement. A test pinning 1008 would fail every time prices move and would
teach the next person that the code is broken when it is not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

INGEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INGEST))

from money import selling_price  # noqa: E402
from squad_reconstruct import (  # noqa: E402
    MAX_FREE_TRANSFERS,
    format_squad,
    free_transfers,
    latest_available_gw,
    price_tables,
    reconstruct_squad,
    transfers_by_gameweek,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TEAM_ID = 895045
SEASON = "2026-27"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def bootstrap():
    return load("bootstrap_static")


@pytest.fixture
def transfers():
    return load("entry_transfers")


@pytest.fixture
def picks():
    return load("entry_picks")


@pytest.fixture
def squad(bootstrap, transfers, picks):
    return reconstruct_squad(TEAM_ID, bootstrap, transfers, picks, entry=None, season=SEASON)


class TestPriceTables:
    def test_prices_are_integer_tenths(self, bootstrap):
        prices_now, changes_start, meta = price_tables(bootstrap)
        assert prices_now
        assert all(isinstance(v, int) for v in prices_now.values())
        assert all(isinstance(v, int) for v in changes_start.values())
        assert set(prices_now) == set(meta)

    def test_a_missing_cost_change_is_zero_not_none(self):
        _, changes, _ = price_tables({"elements": [{"id": 1, "now_cost": 50}]})
        assert changes[1] == 0


class TestRealSquad:
    def test_fifteen_players(self, squad):
        assert squad["picks_available"] is True
        assert len(squad["players"]) == 15

    def test_the_squad_is_the_picks_in_order(self, squad, picks):
        assert [p["element_id"] for p in squad["players"]] == [
            p["element"] for p in picks["picks"]
        ]
        assert [p["squad_position"] for p in squad["players"]] == list(range(1, 16))

    def test_eleven_starters_and_four_on_the_bench(self, squad):
        assert sum(1 for p in squad["players"] if p["on_bench"]) == 4

    def test_every_price_is_a_plausible_integer_tenths(self, squad):
        for player in squad["players"]:
            for key in (
                "purchase_price_tenths",
                "current_price_tenths",
                "selling_price_tenths",
            ):
                value = player[key]
                assert isinstance(value, int), f"{key} is {type(value)}"
                # 3.8 is the cheapest a player has ever been, 16.0 the dearest.
                assert 35 <= value <= 160, f"{key}={value} for {player['element_id']}"

    def test_selling_price_follows_the_selling_rule_for_every_player(self, squad):
        for player in squad["players"]:
            assert player["selling_price_tenths"] == selling_price(
                player["purchase_price_tenths"], player["current_price_tenths"]
            )

    def test_selling_price_never_exceeds_the_current_price(self, squad):
        # The manager keeps at most half a rise, so this is an invariant, not a
        # coincidence of this particular squad.
        for player in squad["players"]:
            assert player["selling_price_tenths"] <= player["current_price_tenths"]
            assert player["selling_price_tenths"] >= min(
                player["purchase_price_tenths"], player["current_price_tenths"]
            )

    def test_squad_value_is_the_selling_total_plus_bank(self, squad):
        assert squad["squad_value_tenths"] == squad["selling_total_tenths"] + squad["bank_tenths"]

    def test_squad_value_is_near_what_fpl_reported_at_the_deadline(self, squad, picks):
        # FPL said 1008 at the GW4 deadline; this values the same squad at today's
        # prices, days later. Assert the neighbourhood, never the exact figure —
        # the gap is price movement and it grows every day the fixture ages.
        reported = picks["entry_history"]["value"]
        assert abs(squad["squad_value_tenths"] - reported) <= 20

    def test_the_bank_comes_from_the_picks_entry_history(self, squad, picks):
        assert squad["bank_tenths"] == picks["entry_history"]["bank"]

    def test_the_gameweek_and_chip_are_carried_through(self, squad, picks):
        assert squad["gw"] == picks["entry_history"]["event"]
        assert squad["active_chip"] == picks["active_chip"]

    def test_captain_and_vice_captain_are_flagged(self, squad):
        assert sum(1 for p in squad["players"] if p["is_captain"]) == 1
        assert sum(1 for p in squad["players"] if p["is_vice_captain"]) == 1

    def test_a_transferred_in_player_is_held_at_a_price_actually_paid(self, squad, transfers):
        # Whatever a player costs today, the purchase price has to be one of the
        # figures in the log — that is what the log is for.
        paid: dict[int, set[int]] = {}
        for row in transfers:
            paid.setdefault(row["element_in"], set()).add(row["element_in_cost"])

        for player in squad["players"]:
            if player["element_id"] in paid:
                assert player["purchase_price_tenths"] in paid[player["element_id"]]

    def test_a_player_bought_back_is_held_at_the_most_recent_price(self, squad):
        # Element 40 was bought in GW4 at 75, sold, then bought back later the
        # same gameweek at 76. Only the times distinguish them, and the manager
        # holds him at 76. Resolving by event alone would silently pick 75.
        held = {p["element_id"]: p for p in squad["players"]}
        assert held[40]["purchase_price_tenths"] == 76

    def test_every_pick_is_either_in_the_log_or_priced_by_bootstrap(
        self, squad, bootstrap, transfers
    ):
        # The bootstrap fixture is trimmed to keep the repo small, so some picks
        # are not in it. That is only survivable because every one of them was
        # bought in the transfer log, which carries the price paid. If a pick were
        # in neither, the reconstruction would be guessing and should say so.
        priced = {int(e["id"]) for e in bootstrap["elements"]}
        bought = {r["element_in"] for r in transfers}
        for player in squad["players"]:
            assert player["element_id"] in priced or player["element_id"] in bought

        assert set(squad["unpriced_elements"]) <= bought

    def test_the_result_is_json_serialisable(self, squad):
        # It is destined for the web app across an HTTP boundary.
        assert json.loads(json.dumps(squad, default=str))["team_id"] == TEAM_ID


class TestNoPicksYet:
    def test_pre_deadline_is_not_a_crash(self, bootstrap, transfers):
        result = reconstruct_squad(TEAM_ID, bootstrap, transfers, picks=None)
        assert result["picks_available"] is False
        assert result["players"] == []

    def test_the_entry_summary_still_fills_what_it_can(self, bootstrap, transfers):
        entry = {
            "name": "Zack's XI",
            "player_first_name": "Zack",
            "player_last_name": "K",
            "current_event": 5,
            "last_deadline_bank": 3,
            "last_deadline_value": 1010,
        }
        result = reconstruct_squad(TEAM_ID, bootstrap, transfers, picks=None, entry=entry)
        assert result["team_name"] == "Zack's XI"
        assert result["manager_name"] == "Zack K"
        assert result["gw"] == 5
        assert result["bank_tenths"] == 3
        assert result["squad_value_tenths"] == 1010

    def test_it_formats_without_raising(self, bootstrap, transfers):
        text = format_squad(reconstruct_squad(TEAM_ID, bootstrap, transfers, picks=None))
        assert "not public yet" in text


class TestFreeTransfers:
    def test_a_manager_who_has_never_transferred_banks_to_the_cap(self):
        assert free_transfers([], up_to_gw=20) == MAX_FREE_TRANSFERS

    def test_one_transfer_a_week_banks_nothing_new(self):
        # Spent as fast as they arrive from GW2 on. Going into GW6 the manager
        # has GW6's own transfer plus the GW1 one nobody ever uses, since a GW1
        # squad is built, not transferred into.
        log = [{"element_in": 1, "event": gw} for gw in range(2, 6)]
        assert free_transfers(log, up_to_gw=5) == 2

    def test_a_hit_cannot_push_the_count_negative(self):
        log = [{"element_in": i, "event": 2} for i in range(6)]
        assert free_transfers(log, up_to_gw=2) == 1

    def test_saving_accumulates_but_only_to_the_cap(self):
        # Three gameweeks of saving, plus the fourth's own transfer.
        assert free_transfers([], up_to_gw=3) == 4
        assert free_transfers([], up_to_gw=30) == MAX_FREE_TRANSFERS

    def test_transfers_made_under_a_wildcard_are_free(self):
        log = [{"element_in": i, "event": 4} for i in range(8)]
        without_chip = free_transfers(log, up_to_gw=4)
        with_chip = free_transfers(log, up_to_gw=4, chip_gws={4: "wildcard"})
        assert without_chip < with_chip
        assert with_chip == 5

    def test_the_real_log_gives_a_sane_number(self, squad):
        assert 0 <= squad["free_transfers"] <= MAX_FREE_TRANSFERS
        assert squad["free_transfers_is_estimate"] is True

    def test_transfers_are_counted_per_gameweek(self, transfers):
        counts = transfers_by_gameweek(transfers)
        assert sum(counts.values()) == len(transfers)
        assert all(isinstance(gw, int) for gw in counts)


class TestLatestAvailableGw:
    def test_it_picks_the_highest_finished_or_current_gameweek(self):
        bootstrap = {
            "events": [
                {"id": 1, "finished": True, "is_current": False},
                {"id": 2, "finished": True, "is_current": False},
                {"id": 3, "finished": False, "is_current": True},
                {"id": 4, "finished": False, "is_current": False},
            ]
        }
        assert latest_available_gw(bootstrap) == 3

    def test_the_preseason_has_no_available_gameweek(self):
        assert latest_available_gw({"events": [{"id": 1, "finished": False}]}) is None

    def test_the_real_bootstrap_fixture_has_one(self, bootstrap):
        assert latest_available_gw(bootstrap) is not None


class TestUnpricedElements:
    def test_a_squad_member_missing_from_bootstrap_is_reported_not_raised(self):
        # Stale reference data. The player was bought in the log so the purchase
        # price is known; only today's price is missing, and denying the user the
        # other fourteen players over it would be the wrong trade.
        bootstrap = {"elements": [{"id": 1, "now_cost": 50, "cost_change_start": 0}]}
        transfers = [
            {
                "element_in": 99,
                "element_in_cost": 70,
                "element_out": 1,
                "element_out_cost": 50,
                "event": 3,
                "time": "2026-09-01T10:00:00Z",
            }
        ]
        picks = {
            "entry_history": {"event": 3, "bank": 5},
            "active_chip": None,
            "picks": [{"element": 99, "position": 1, "multiplier": 1}],
        }
        result = reconstruct_squad(TEAM_ID, bootstrap, transfers, picks)
        assert result["unpriced_elements"] == [99]
        held = result["players"][0]
        assert held["purchase_price_tenths"] == 70
        assert held["selling_price_tenths"] == 70


class TestFormatting:
    def test_the_table_names_every_player(self, squad):
        text = format_squad(squad)
        assert str(squad["team_id"]) in text
        assert text.count("\n") > 15

    def test_money_renders_as_tenths_not_floats(self, squad):
        text = format_squad(squad)
        # A float leaking into display is a sign one leaked into the arithmetic.
        assert ".00" not in text
        assert "0000" not in text
