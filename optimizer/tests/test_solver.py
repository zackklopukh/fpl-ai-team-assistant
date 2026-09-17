"""Tests for the MIP squad solver.

No network, no database, no xP model. Every case is a small synthetic matrix
whose right answer is worked out by hand in the test's own docstring -- which is
the only way to test an optimizer meaningfully. A test that asserts the solver
agrees with itself catches nothing; a test that asserts it agrees with arithmetic
you did on paper catches a wrong constraint.

The base world is fifteen players, one per club so the three-per-club rule never
binds by accident, with xP chosen so the best eleven is unambiguous:

    GKP   1: 4.0    2: 1.0
    DEF   3: 4.5    4: 4.0   5: 3.5   6: 3.0   7: 1.0
    MID   8: 6.0    9: 5.5  10: 5.0  11: 4.5  12: 1.0
    FWD  13: 5.0   14: 4.5  15: 1.0

Best XI is a 4-4-2: GK 1, DEF 3/4/5/6, MID 8/9/10/11, FWD 13/14, captain 8.
That is 49.5 points plus the captain's 6.0 again = 55.5. Bench is 2, 7, 12, 15.
The eleventh place goes to DEF 6 on 3.0, because the alternatives (7, 12, 15) are
all on 1.0 -- so 3.0 is the cutoff every "does this transfer get into the team"
argument below is measured against.
"""

from __future__ import annotations

import time

import pytest

from optimizer.contract import SquadPlayer
from optimizer.solver import (
    DEF,
    FWD,
    GKP,
    MID,
    SQUAD_BY_POSITION,
    XI_BY_POSITION,
    XI_SIZE,
    PlayerMeta,
    SolveResult,
    SolverError,
    solve,
)

# element_id, position, club, xP
BASE = [
    (1, GKP, 1, 4.0),
    (2, GKP, 2, 1.0),
    (3, DEF, 3, 4.5),
    (4, DEF, 4, 4.0),
    (5, DEF, 5, 3.5),
    (6, DEF, 6, 3.0),
    (7, DEF, 7, 1.0),
    (8, MID, 8, 6.0),
    (9, MID, 9, 5.5),
    (10, MID, 10, 5.0),
    (11, MID, 11, 4.5),
    (12, MID, 12, 1.0),
    (13, FWD, 13, 5.0),
    (14, FWD, 14, 4.5),
    (15, FWD, 15, 1.0),
]
BASE_IDS = [row[0] for row in BASE]
GW = 6


def build(gws, *, market=(), selling=None, prices=None, xp_overrides=None):
    """Turn the tables above into (squad, xp_matrix, pool).

    `market` rows are (element_id, position, club, xp, price) and are not held.
    `selling` and `prices` override the default flat 50 (5.0m) selling and list
    prices per element_id. `xp_overrides` is element_id -> gw -> xp, for blanks.
    """
    selling = selling or {}
    prices = prices or {}
    xp_overrides = xp_overrides or {}

    pool: dict[int, PlayerMeta] = {}
    xp: dict[int, dict[int, float]] = {}
    for eid, pos, club, value in list(BASE) + [row[:4] for row in market]:
        pool[eid] = PlayerMeta(
            element_id=eid,
            web_name=f"P{eid}",
            team_fpl_id=club,
            element_type=pos,
            price=prices.get(eid, 50),
        )
        xp[eid] = {gw: float(value) for gw in gws}
    for eid, price in ((row[0], row[4]) for row in market):
        pool[eid] = PlayerMeta(**{**pool[eid].__dict__, "price": price})
    for eid, overrides in xp_overrides.items():
        xp[eid].update(overrides)

    squad = [SquadPlayer(element_id=eid, selling_price=selling.get(eid, 50)) for eid in BASE_IDS]
    return squad, xp, pool


def squad_of(plan):
    return list(plan.xi) + list(plan.bench_order)


def assert_legal(plan, pool):
    """Every plan, regardless of what it recommends, has to describe a legal team."""
    fifteen = squad_of(plan)
    assert len(fifteen) == 15
    assert len(set(fifteen)) == 15
    assert len(plan.xi) == XI_SIZE

    by_pos: dict[int, int] = {}
    clubs: dict[int, int] = {}
    for eid in fifteen:
        by_pos[pool[eid].element_type] = by_pos.get(pool[eid].element_type, 0) + 1
        clubs[pool[eid].team_fpl_id] = clubs.get(pool[eid].team_fpl_id, 0) + 1
    assert by_pos == SQUAD_BY_POSITION
    assert max(clubs.values()) <= 3

    xi_pos: dict[int, int] = {}
    for eid in plan.xi:
        xi_pos[pool[eid].element_type] = xi_pos.get(pool[eid].element_type, 0) + 1
    for pos, (lo, hi) in XI_BY_POSITION.items():
        assert lo <= xi_pos.get(pos, 0) <= hi, f"illegal formation {xi_pos}"

    assert plan.captain in plan.xi
    assert plan.vice_captain in plan.xi
    assert plan.vice_captain != plan.captain


# ---------------------------------------------------------------------------


def test_baseline_xi_and_captain_match_the_hand_calculation():
    """No market at all, so the only decision is the lineup: the 4-4-2 above."""
    squad, xp, pool = build([GW])
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)

    assert isinstance(result, SolveResult)
    hold = next(p for p in result.plans if p.label == "Hold")
    assert sorted(hold.xi) == [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14]
    assert hold.captain == 8
    assert hold.vice_captain == 9
    assert hold.transfers_in == [] and hold.transfers_out == []
    assert hold.hit_cost == 0
    assert result.baseline_xp == pytest.approx(55.5)
    assert hold.delta_xp == pytest.approx(0.0)
    # The reserve keeper leads the bench (FPL slot 12); outfield follow by xP.
    assert hold.bench_order[0] == 2
    assert set(hold.bench_order) == {2, 7, 12, 15}
    assert_legal(hold, pool)


def test_hold_is_always_offered():
    """A tool that never says 'hold' does not get believed, so it is never absent."""
    squad, xp, pool = build([GW], market=[(100, MID, 20, 12.0, 50)])
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)
    labels = [p.label for p in result.plans]
    assert "Hold" in labels
    assert labels[0] != "Hold"  # a 12.0 midfielder for a 1.0 one is not a hold


def test_obvious_affordable_upgrade_is_found():
    """Player 100 is a 12.0 midfielder at the same 5.0m everyone else costs.

    Selling MID 12 (1.0, benched) funds him exactly with no bank. Any other sale
    loses more than 1.0, so out must be 12 and in must be 100, and with one free
    transfer there is no hit. He is also the best player in the team, so he takes
    the armband off player 8.
    """
    squad, xp, pool = build([GW], market=[(100, MID, 20, 12.0, 50)])
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)

    best = result.plans[0]
    assert [t.element_id for t in best.transfers_in] == [100]
    assert [t.element_id for t in best.transfers_out] == [12]
    assert best.hit_cost == 0
    assert best.captain == 100
    assert best.delta_xp > 0
    # 100 replaces 12 in the squad and displaces nobody from the XI except by
    # being better than the 3.0 cutoff: XI gains 12.0 - 3.0, captaincy gains
    # 12.0 - 6.0. 55.5 + 9.0 + 6.0 = 70.5.
    assert best.per_gw_breakdown[0].xp == pytest.approx(70.5)
    assert_legal(best, pool)


def test_a_transfer_worth_less_than_four_points_is_not_taken_on_a_hit():
    """Player 101 is a 4.0 midfielder. He beats the XI cutoff of 3.0 by 1.0.

    With no free transfer that costs -4 for a gain of 1.0 plus a sliver of bench
    weight, so holding wins. This is the case that fails if the hit is subtracted
    from the answer afterwards instead of sitting inside the objective: raw points
    go up, so an after-the-fact model takes it and then reports a loss.
    """
    squad, xp, pool = build([GW], market=[(101, MID, 20, 4.0, 50)])
    result = solve(squad, xp, pool, bank=0, free_transfers=0, current_gw=GW, horizon=1)

    assert result.plans[0].label == "Hold"
    assert result.plans[0].transfers_in == []
    for plan in result.plans:
        if plan.transfers_in:
            assert plan.delta_xp < 0, "a hit that does not pay for itself ranked above holding"


def test_the_same_transfer_is_taken_when_it_is_free():
    """Identical to the previous case but with the free transfer, so 1.0 > 0."""
    squad, xp, pool = build([GW], market=[(101, MID, 20, 4.0, 50)])
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)

    best = result.plans[0]
    assert [t.element_id for t in best.transfers_in] == [101]
    assert best.hit_cost == 0
    assert best.delta_xp > 0


def test_only_the_first_of_two_marginal_transfers_is_taken():
    """Two 4.0 midfielders available, one free transfer, cutoff 3.0.

    The first is worth +1.0 free. The second displaces the new cutoff (the other
    4.0 man is in, so the eleventh place is now worth 3.0 again -- DEF 6 drops
    out) for another +1.0, at a cost of 4. So exactly one transfer.
    """
    squad, xp, pool = build(
        [GW], market=[(101, MID, 20, 4.0, 50), (102, MID, 21, 4.0, 50)]
    )
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)

    best = result.plans[0]
    assert len(best.transfers_in) == 1
    assert best.hit_cost == 0


def test_delta_xp_is_net_of_the_hit_not_gross():
    """Player 100 is a 20.0 midfielder; MID 12 on 1.0 is sold to fund him.

    He clears the 3.0 XI cutoff, so the eleven gains 20.0 - 3.0 = 17.0, and he
    takes the armband off player 8, so the captaincy gains 20.0 - 6.0 = 14.0.
    Gross +31.0. With no free transfer that is a -4, so the number the user is
    shown must be 27.0. Reporting 31.0 and a hit separately is how a tool ends up
    recommending moves that lose points.
    """
    squad, xp, pool = build([GW], market=[(100, MID, 20, 20.0, 50)])
    result = solve(squad, xp, pool, bank=0, free_transfers=0, current_gw=GW, horizon=1)

    best = result.plans[0]
    assert [t.element_id for t in best.transfers_in] == [100]
    assert best.hit_cost == 4
    assert best.delta_xp == pytest.approx(27.0)
    assert best.per_gw_breakdown[0].xp == pytest.approx(55.5 + 31.0)


def test_free_transfers_roll_over_so_waiting_a_week_avoids_a_hit():
    """Two 20.0 midfielders, both blanking this gameweek and both huge the next.

    Buying them now with one free transfer costs -4 and gains nothing this week,
    because their xP this week is 0. Buying nothing now rolls the transfer to two,
    and both arrive next week for free. So the right answer is no transfer this
    week and two the next -- which is only reachable if free transfers are carried
    over inside the model rather than treated as a per-gameweek allowance.
    """
    gws = [GW, GW + 1]
    squad, xp, pool = build(
        gws,
        market=[(105, MID, 20, 20.0, 50), (106, MID, 21, 20.0, 50)],
        xp_overrides={105: {GW: 0.0}, 106: {GW: 0.0}},
    )
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=2)

    best = result.plans[0]
    assert best.transfers_in == [], "it should bank the transfer, not spend it on a blank"
    assert best.hit_cost == 0
    assert best.label == "Roll your transfer"
    assert best.delta_xp > 0, "rolling has to beat never moving at all"
    # And the strict never-transfer baseline is still offered separately.
    assert any(p.label == "Hold" for p in result.plans)


def test_budget_uses_selling_prices_not_list_prices():
    """MID 12 sells for 4.5m though he is listed at 6.0m; everyone else sells for 4.0m.

    Target 102 is a 20.0 midfielder priced at 5.5m. Bank is 1.0m. Only MID 12's
    4.5m plus the 1.0m bank reaches 5.5m exactly, so the transfer is affordable to
    the tenth and must happen. Capped at one transfer so no combination of sales
    can fund it another way.
    """
    squad, xp, pool = build(
        [GW],
        market=[(102, MID, 20, 20.0, 55)],
        selling={eid: 40 for eid in BASE_IDS} | {12: 45},
        prices={12: 60},
    )
    result = solve(
        squad, xp, pool, bank=10, free_transfers=1, current_gw=GW, horizon=1,
        max_transfers_per_gw=1,
    )
    best = result.plans[0]
    assert [t.element_id for t in best.transfers_in] == [102]
    assert [t.element_id for t in best.transfers_out] == [12]
    assert best.hit_cost == 0


def test_budget_binds_one_tenth_too_high():
    """Exactly the case above with the target at 5.6m instead of 5.5m.

    4.5m + 1.0m = 5.5m, so it is one tenth short and the transfer is impossible,
    however many points it would have been worth. If the solver had wrongly used
    MID 12's 6.0m *list* price as the cash freed it would have had 7.0m and taken
    it -- which is precisely the off-by-one that integer tenths exist to prevent.
    """
    squad, xp, pool = build(
        [GW],
        market=[(102, MID, 20, 20.0, 56)],
        selling={eid: 40 for eid in BASE_IDS} | {12: 45},
        prices={12: 60},
    )
    result = solve(
        squad, xp, pool, bank=10, free_transfers=1, current_gw=GW, horizon=1,
        max_transfers_per_gw=1,
    )
    assert result.plans[0].label == "Hold"
    assert all(102 not in squad_of(p) for p in result.plans)


def test_three_per_club_holds():
    """Club 3 already supplies DEF 3, 4 and 5 (re-clubbed below), and the market
    offers two 15.0 defenders from that same club at the same price.

    Ignoring the rule, the solver would buy both and sell the two weakest players.
    It cannot: taking two from club 3 means selling two of the three it already
    has, and the squad must still end on three.
    """
    squad, xp, pool = build([GW], market=[(103, DEF, 3, 15.0, 50), (104, DEF, 3, 15.0, 50)])
    for eid in (3, 4, 5):
        pool[eid] = PlayerMeta(**{**pool[eid].__dict__, "team_fpl_id": 3})

    result = solve(squad, xp, pool, bank=0, free_transfers=5, current_gw=GW, horizon=1)
    for plan in result.plans:
        assert_legal(plan, pool)
        club_3 = sum(1 for eid in squad_of(plan) if pool[eid].team_fpl_id == 3)
        assert club_3 <= 3, f"{club_3} players from one club"


def test_a_blank_gameweek_player_is_benched():
    """DEF 3 is the best defender at 4.5 but has no fixture, so his xP is 0.

    A blank is not a special case in the model -- the xP matrix already accounts
    for fixture count, so a blank simply arrives as 0. What the formation
    constraint must not do is start him anyway to fill a defender slot: the XI
    needs three defenders and 4, 5 and 6 supply them, with the eleventh place
    going to one of the 1.0 players rather than to a 0.0 one.
    """
    squad, xp, pool = build([GW], xp_overrides={3: {GW: 0.0}})
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)

    hold = next(p for p in result.plans if p.label == "Hold")
    assert 3 not in hold.xi
    assert 3 in hold.bench_order
    assert {4, 5, 6} <= set(hold.xi)
    assert_legal(hold, pool)


def test_every_plan_is_a_legal_team_over_a_multi_gameweek_horizon():
    """Three gameweeks, a market with real options, and a double for player 9.

    Nothing here has a hand-computed answer -- this is the structural check that
    whatever the solver decides is a team someone could actually field, including
    when the horizon lets it plan more than one transfer.
    """
    gws = [GW, GW + 1, GW + 2]
    market = [
        (100, GKP, 20, 4.5, 45),
        (101, DEF, 21, 5.5, 55),
        (102, MID, 22, 7.0, 80),
        (103, MID, 23, 6.5, 70),
        (104, FWD, 24, 6.5, 75),
    ]
    squad, xp, pool = build(gws, market=market)
    xp[9][GW + 1] = 11.0  # a double gameweek, already baked into the number
    fixtures = {eid: {gw: 1 for gw in gws} for eid in pool}
    fixtures[9][GW + 1] = 2  # the double
    fixtures[3][GW + 2] = 0  # and a blank

    result = solve(
        squad, xp, pool, bank=20, free_transfers=2, current_gw=GW, horizon=3, fixtures=fixtures
    )
    assert result.plans
    for plan in result.plans:
        assert_legal(plan, pool)
        assert len(plan.per_gw_breakdown) == 3
        assert [b.gw for b in plan.per_gw_breakdown] == gws
        # The breakdown covers the squad as it is that gameweek, not as it is today.
        assert set(plan.per_gw_breakdown[0].n_fixtures) == set(squad_of(plan))


def test_plans_are_distinct_and_ranked():
    """Three plausible upgrades means three genuinely different answers, not one
    answer with the bench shuffled."""
    market = [
        (100, MID, 20, 9.0, 50),
        (101, MID, 21, 8.5, 50),
        (102, FWD, 22, 8.0, 50),
    ]
    squad, xp, pool = build([GW], market=market)
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1, n_plans=3)

    # Distinct on who comes *in*. Two plans that sign the same player and differ
    # only in which worthless bench player funds it are one plan, not two.
    incoming = [tuple(t.element_id for t in p.transfers_in) for p in result.plans]
    assert len(set(incoming)) == len(incoming) >= 2, incoming
    assert result.plans[0].delta_xp >= result.plans[-1].delta_xp


def test_time_limit_is_respected_and_truncation_is_reported():
    """A budget too small to finish the search must still return a usable answer.

    The hold is solved first precisely so there is always something to show, and
    `truncated` tells the caller the search stopped early so the UI can say 'best
    found' rather than implying a proof of optimality it does not have.
    """
    market = [(200 + i, (i % 4) + 1, 30 + (i % 25), 3.0 + (i % 60) / 10, 40 + (i % 60)) for i in range(160)]
    gws = [GW, GW + 1, GW + 2, GW + 3, GW + 4]
    squad, xp, pool = build(gws, market=market)

    started = time.perf_counter()
    result = solve(
        squad, xp, pool, bank=30, free_transfers=2, current_gw=GW, horizon=5, time_limit=1.0
    )
    elapsed = time.perf_counter() - started

    assert result.truncated is True
    assert result.plans, "a truncated solve still has to return the hold"
    assert any(p.label == "Hold" for p in result.plans)
    assert result.solve_ms > 0
    # CBC's own floor is one second per solve, so the wall clock can overrun a
    # sub-second budget. What must not happen is it running for minutes.
    assert elapsed < 20.0


def test_an_unfilterable_pool_is_the_callers_problem_not_a_crash():
    squad, xp, pool = build([GW])
    del pool[7]
    with pytest.raises(SolverError, match="missing from the pool"):
        solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)


def test_xp_can_ride_on_the_pool_objects():
    """`pool.candidate_pool` returns PlayerXP rows that already carry xp_by_gw, so
    passing None for the matrix has to work rather than forcing the caller to
    unpack and repack it."""

    class Row:
        def __init__(self, meta, xp_by_gw):
            self.element_id = meta.element_id
            self.web_name = meta.web_name
            self.team_fpl_id = meta.team_fpl_id
            self.element_type = meta.element_type
            self.price = meta.price
            self.xp_by_gw = xp_by_gw

    squad, xp, pool = build([GW])
    rows = [Row(meta, xp[eid]) for eid, meta in pool.items()]
    result = solve(squad, None, rows, bank=0, free_transfers=1, current_gw=GW, horizon=1)
    assert result.baseline_xp == pytest.approx(55.5)


def test_solve_result_still_behaves_like_a_list_of_plans():
    squad, xp, pool = build([GW])
    result = solve(squad, xp, pool, bank=0, free_transfers=1, current_gw=GW, horizon=1)
    assert len(result) == len(result.plans)
    assert result[0] is result.plans[0]
    assert [p.label for p in result] == [p.label for p in result.plans]
