"""Tests for the FPL money rules.

These are the cheapest tests in the repo and they guard the invariant most likely
to produce a wrong answer that looks right: an off-by-one squad value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from money import (  # noqa: E402
    Holding,
    reconstruct_purchase_prices,
    selling_price,
    squad_value,
    start_price,
)


class TestSellingPrice:
    def test_unchanged_price_sells_for_what_it_cost(self):
        assert selling_price(50, 50) == 50

    @pytest.mark.parametrize(
        "purchase,current,expected",
        [
            (50, 51, 50),  # rose 0.1 — half of one tenth floors to nothing
            (50, 52, 51),  # rose 0.2 — keeps 0.1
            (50, 53, 51),  # rose 0.3 — keeps 0.1, the floored half
            (50, 54, 52),  # rose 0.4 — keeps 0.2
            (50, 55, 52),  # rose 0.5 — keeps 0.2
            (130, 137, 133),  # rose 0.7 — keeps 0.3
        ],
    )
    def test_seller_keeps_half_a_rise_rounded_down(self, purchase, current, expected):
        assert selling_price(purchase, current) == expected

    @pytest.mark.parametrize(
        "purchase,current",
        [(50, 49), (50, 45), (130, 121)],
    )
    def test_falls_are_passed_on_in_full(self, purchase, current):
        assert selling_price(purchase, current) == current

    def test_a_rise_then_a_fall_below_purchase_gives_back_the_current_price(self):
        # Bought at 50, peaked at 56, now back to 48. No profit is retained.
        assert selling_price(50, 48) == 48

    def test_result_is_always_an_integer(self):
        # The rule floors a halved difference. A float here means a corrupted
        # squad value later, so assert the type, not just the value.
        result = selling_price(55, 58)
        assert isinstance(result, int)
        assert result == 56


class TestStartPrice:
    def test_start_price_backs_out_the_season_change(self):
        assert start_price(now_cost=55, cost_change_start=5) == 50
        assert start_price(now_cost=45, cost_change_start=-5) == 50
        assert start_price(now_cost=50, cost_change_start=0) == 50


class TestReconstructPurchasePrices:
    def test_an_untouched_squad_falls_back_to_day_one_prices(self):
        prices = reconstruct_purchase_prices(
            current_squad=[1, 2],
            transfers=[],
            prices_now={1: 55, 2: 100},
            price_changes_start={1: 5, 2: -2},
        )
        assert prices == {1: 50, 2: 102}

    def test_a_transferred_in_player_uses_the_price_actually_paid(self):
        transfers = [
            {
                "element_in": 9,
                "element_in_cost": 76,
                "element_out": 1,
                "element_out_cost": 95,
                "event": 4,
                "time": "2026-09-12T12:17:51Z",
            }
        ]
        prices = reconstruct_purchase_prices(
            current_squad=[9],
            transfers=transfers,
            prices_now={9: 80},
            price_changes_start={9: 4},
        )
        # 76 paid, not the 76 implied by today's price — they happen to differ.
        assert prices == {9: 76}

    def test_buying_a_player_back_uses_the_most_recent_purchase(self):
        # Bought in GW2 at 70, sold in GW5, bought back in GW9 at 82. The manager
        # holds him at 82. Rows are deliberately out of order, as the API returns
        # them newest-first.
        transfers = [
            {
                "element_in": 7,
                "element_in_cost": 82,
                "element_out": 3,
                "element_out_cost": 50,
                "event": 9,
                "time": "2026-10-25T10:00:00Z",
            },
            {
                "element_in": 3,
                "element_in_cost": 50,
                "element_out": 7,
                "element_out_cost": 74,
                "event": 5,
                "time": "2026-09-20T10:00:00Z",
            },
            {
                "element_in": 7,
                "element_in_cost": 70,
                "element_out": 2,
                "element_out_cost": 45,
                "event": 2,
                "time": "2026-08-28T10:00:00Z",
            },
        ]
        prices = reconstruct_purchase_prices(
            current_squad=[7],
            transfers=transfers,
            prices_now={7: 85},
            price_changes_start={7: 15},
        )
        assert prices == {7: 82}

    def test_two_transfers_in_the_same_gameweek_resolve_by_time(self):
        transfers = [
            {
                "element_in": 5,
                "element_in_cost": 60,
                "element_out": 4,
                "element_out_cost": 55,
                "event": 7,
                "time": "2026-10-10T09:00:00Z",
            },
            {
                "element_in": 5,
                "element_in_cost": 58,
                "element_out": 6,
                "element_out_cost": 50,
                "event": 7,
                "time": "2026-10-09T09:00:00Z",
            },
        ]
        prices = reconstruct_purchase_prices(
            current_squad=[5],
            transfers=transfers,
            prices_now={5: 62},
            price_changes_start={5: 2},
        )
        assert prices == {5: 60}

    def test_an_unknown_element_is_an_error_not_a_silent_zero(self):
        # Usually means stale bootstrap data or an element id from another season,
        # which is exactly the corruption the (season, element_id) key exists to
        # prevent. Fail loudly.
        with pytest.raises(KeyError):
            reconstruct_purchase_prices(
                current_squad=[999],
                transfers=[],
                prices_now={1: 50},
                price_changes_start={},
            )


class TestSquadValue:
    def test_squad_value_is_sellable_value_plus_bank(self):
        holdings = [
            Holding(element_id=1, purchase_price=50, current_price=53),  # sells 51
            Holding(element_id=2, purchase_price=100, current_price=98),  # sells 98
        ]
        assert squad_value(holdings, bank=7) == 51 + 98 + 7

    def test_holding_exposes_the_selling_rule(self):
        assert Holding(element_id=1, purchase_price=47, current_price=49).selling_price == 48
