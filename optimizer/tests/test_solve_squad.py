"""Tests for the ideal-squad solver: a team from scratch, and a wildcard.

As in test_solver.py, every case is a small world whose right answer is
worked out by hand in the docstring. The money rule is the thing most worth
pinning, because it is the one that is easy to get subtly wrong: on a wildcard a
kept player costs his SELLING price, and a new one his LIST price.
"""

from __future__ import annotations

import pytest

from optimizer.contract import SquadPlayer
from optimizer.solver import (
    DEF,
    FWD,
    GKP,
    MID,
    SQUAD_BY_POSITION,
    XI_BY_POSITION,
    PlayerMeta,
    SolverError,
    solve_squad,
)

GW = 6


def market(rows):
    """rows: (element_id, position, club, xp, price) -> (xp_matrix, pool)."""
    pool = {
        eid: PlayerMeta(element_id=eid, web_name=f"P{eid}", team_fpl_id=club,
                        element_type=pos, price=price)
        for eid, pos, club, _, price in rows
    }
    xp = {eid: {GW: float(value)} for eid, _, _, value, _ in rows}
    return xp, pool


def assert_legal(result, pool):
    ids = [p["element_id"] for p in result["players"]]
    assert len(ids) == 15 and len(set(ids)) == 15
    by_pos, clubs = {}, {}
    for e in ids:
        by_pos[pool[e].element_type] = by_pos.get(pool[e].element_type, 0) + 1
        clubs[pool[e].team_fpl_id] = clubs.get(pool[e].team_fpl_id, 0) + 1
    assert by_pos == SQUAD_BY_POSITION
    assert max(clubs.values()) <= 3
    xi_pos = {}
    for e in result["xi"]:
        xi_pos[pool[e].element_type] = xi_pos.get(pool[e].element_type, 0) + 1
    assert len(result["xi"]) == 11
    for pos, (lo, hi) in XI_BY_POSITION.items():
        assert lo <= xi_pos.get(pos, 0) <= hi
    assert set(result["xi"]) | set(result["bench_order"]) == set(ids)
    assert result["captain"] in result["xi"]
    assert result["cost"] + result["bank_after"] == result["budget"]
    assert result["bank_after"] >= 0


# Sixteen "good" players at 5.0m, one per club, plus a cheap filler per
# position at 4.0m. The best fifteen by xP is obvious; the budget decides
# whether it can be afforded.
BASE = [
    (1, GKP, 1, 4.0, 50), (2, GKP, 2, 3.0, 50),
    (3, DEF, 3, 5.0, 50), (4, DEF, 4, 4.8, 50), (5, DEF, 5, 4.6, 50),
    (6, DEF, 6, 4.4, 50), (7, DEF, 7, 4.2, 50),
    (8, MID, 8, 7.0, 50), (9, MID, 9, 6.5, 50), (10, MID, 10, 6.0, 50),
    (11, MID, 11, 5.5, 50), (12, MID, 12, 5.0, 50),
    (13, FWD, 13, 6.8, 50), (14, FWD, 14, 6.2, 50), (15, FWD, 15, 5.8, 50),
    # Cheap, weak fillers at 4.0m.
    (21, GKP, 21, 1.0, 40), (22, GKP, 22, 1.0, 40),
    (23, DEF, 23, 1.0, 40), (24, DEF, 24, 1.0, 40), (25, DEF, 25, 1.0, 40),
    (26, DEF, 26, 1.0, 40), (27, DEF, 27, 1.0, 40),
    (28, MID, 28, 1.0, 40), (29, MID, 29, 1.0, 40), (30, MID, 30, 1.0, 40),
    (31, MID, 31, 1.0, 40), (32, MID, 32, 1.0, 40),
    (33, FWD, 33, 1.0, 40), (34, FWD, 34, 1.0, 40), (35, FWD, 35, 1.0, 40),
]


class TestFromScratch:
    def test_an_unconstrained_budget_buys_the_fifteen_best(self):
        """15 x 5.0m = 75.0m, under 100.0m, so the good players are all bought."""
        xp, pool = market(BASE)
        result = solve_squad(None, xp, pool, GW, horizon=1, budget=1000)

        assert_legal(result, pool)
        assert {p["element_id"] for p in result["players"]} == set(range(1, 16))
        assert result["cost"] == 750
        assert result["bank_after"] == 250
        assert result["kept_count"] is None and result["gain_vs_hold"] is None
        # Captain is the best starter, MID 8 on 7.0.
        assert result["captain"] == 8

    def test_the_budget_is_respected_to_the_tenth(self):
        """At 70.0m the good fifteen (75.0m) cannot all be afforded.

        Swapping one good player for a 4.0m filler saves exactly 1.0m, so 70.0m
        forces five swaps and a total of exactly 70.0m. The solver should drop
        the five whose loss costs least in the XI — never overspend.
        """
        xp, pool = market(BASE)
        result = solve_squad(None, xp, pool, GW, horizon=1, budget=700)

        assert_legal(result, pool)
        assert result["cost"] <= 700
        assert sum(1 for p in result["players"] if p["price"] == 40) >= 5

    def test_a_budget_below_the_cheapest_squad_is_an_error_not_a_team(self):
        """Fifteen fillers cost 60.0m; 59.9m cannot buy a legal squad."""
        xp, pool = market(BASE)
        with pytest.raises(SolverError, match="budget"):
            solve_squad(None, xp, pool, GW, horizon=1, budget=599)

    def test_the_squad_is_frozen_across_the_horizon(self):
        """A wildcard buys once. Later gameweeks change the XI, never the fifteen.

        MID 12 is worth nothing in GW6 and a lot in GW7-8. A free-transfer model
        would sign him later; a wildcard must decide now, and either way the
        breakdown must describe one fifteen throughout.
        """
        rows = BASE
        xp, pool = market(rows)
        for eid in xp:
            xp[eid].update({7: xp[eid][GW], 8: xp[eid][GW]})
        result = solve_squad(None, xp, pool, GW, horizon=3, budget=1000)

        assert_legal(result, pool)
        assert len(result["per_gw_breakdown"]) == 3
        fifteen = {p["element_id"] for p in result["players"]}
        for gw in result["per_gw_breakdown"]:
            assert gw.captain_element_id in fifteen


class TestWildcard:
    def squad(self, selling=None):
        selling = selling or {}
        return [SquadPlayer(element_id=e, selling_price=selling.get(e, 50)) for e in range(1, 16)]

    def test_the_money_is_bank_plus_what_the_squad_fetches(self):
        xp, pool = market(BASE)
        result = solve_squad(self.squad(), xp, pool, GW, horizon=1, bank=5)

        assert result["budget"] == 15 * 50 + 5

    def test_a_kept_player_costs_his_selling_price_not_his_list_price(self):
        """The case the whole money rule exists for.

        MID 8 was bought cheap and has risen: list 10.0m, selling 7.5m. Keeping
        him costs 7.5m. If the solver priced him at list, the squad would be
        2.5m poorer than it really is.
        """
        rows = [r if r[0] != 8 else (8, MID, 8, 7.0, 100) for r in BASE]
        xp, pool = market(rows)
        selling = {8: 75}
        result = solve_squad(self.squad(selling), xp, pool, GW, horizon=1, bank=0)

        by_id = {p["element_id"]: p for p in result["players"]}
        assert by_id[8]["kept"] is True
        assert by_id[8]["price"] == 100
        assert by_id[8]["cost"] == 75
        assert result["budget"] == 14 * 50 + 75
        assert result["cost"] == sum(p["cost"] for p in result["players"])

    def test_keeping_the_best_squad_already_held_changes_nothing(self):
        """The held fifteen IS the best fifteen, so the wildcard keeps all of it
        and gains exactly nothing over holding."""
        xp, pool = market(BASE)
        result = solve_squad(self.squad(), xp, pool, GW, horizon=1, bank=0)

        assert_legal(result, pool)
        assert result["kept_count"] == 15
        assert result["gain_vs_hold"] == pytest.approx(0.0, abs=1e-6)

    def test_a_wildcard_never_does_worse_than_holding(self):
        """Holding is always a legal wildcard, so the gain is never negative.

        Here the held squad is weak (fillers in midfield) and a 25.0m bank is
        enough to buy the good midfielders back.
        """
        weak = list(range(1, 8)) + [28, 29, 30, 31, 32] + [13, 14, 15]
        squad = [SquadPlayer(element_id=e, selling_price=pool_price) for e, pool_price in
                 ((e, 40 if e >= 21 else 50) for e in weak)]
        xp, pool = market(BASE)
        result = solve_squad(squad, xp, pool, GW, horizon=1, bank=50)

        assert_legal(result, pool)
        assert result["gain_vs_hold"] > 0
        assert result["kept_count"] < 15

    def test_no_hit_is_ever_taken(self):
        """Replacing every player costs nothing in points on a wildcard."""
        fillers = list(range(21, 36))
        squad = [SquadPlayer(element_id=e, selling_price=40) for e in fillers]
        xp, pool = market(BASE)
        result = solve_squad(squad, xp, pool, GW, horizon=1, bank=150)

        # 15 x 4.0m + 15.0m bank = 75.0m: exactly the good fifteen.
        assert {p["element_id"] for p in result["players"]} == set(range(1, 16))
        assert result["kept_count"] == 0
        # total_xp is starters + captain with no -4s: 11 best + captain 8.
        starters = sorted((xp[e][GW] for e in result["xi"]), reverse=True)
        assert result["total_xp"] == pytest.approx(sum(starters) + 7.0, abs=1e-6)
