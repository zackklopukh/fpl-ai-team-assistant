"""Tests for the greedy solver and the candidate pool.

No network, no database: everything runs off optimizer/tests/seed_xp.json.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from optimizer.contract import SquadPlayer
from optimizer.greedy import (
    MAX_PER_CLUB,
    SQUAD_QUOTA,
    XI_MAX,
    XI_MIN,
    SquadError,
    pick_lineup,
    recommend,
    validate_squad,
)
from optimizer.pool import DEFAULT_POOL_PER_POSITION, FixtureXPProvider, candidate_pool

SEED = Path(__file__).parent / "seed_xp.json"
GWS = [6, 7, 8]
SNAPSHOT = FixtureXPProvider(SEED).snapshot("2026-27")

# The deliberately template-owned premium in the seed data: highest xP in the
# set, and owned by 64.8%. Differential mode has to refuse to recommend him.
PREMIUM_ID = 112


def build_squad(preferred_club: int | None = None):
    """A legal, cheap 15 from the seed data.

    Cheapest-first so there is always an upgrade available, which is what makes
    the transfer assertions meaningful. `preferred_club` fills the first three
    slots from one club so the max-3 rule actually binds.
    """
    picked = []
    clubs: dict[int, int] = {}
    ordered = sorted(SNAPSHOT.players.values(), key=lambda p: (p.price, p.element_id))
    for etype, need in SQUAD_QUOTA.items():
        have = 0
        pool = ordered
        if preferred_club is not None:
            # Try that club first, still respecting the cap.
            pool = sorted(ordered, key=lambda p: (p.team_fpl_id != preferred_club,))
        for p in pool:
            if p.element_type != etype or clubs.get(p.team_fpl_id, 0) >= MAX_PER_CLUB:
                continue
            picked.append(p)
            clubs[p.team_fpl_id] = clubs.get(p.team_fpl_id, 0) + 1
            have += 1
            if have == need:
                break
    return picked


def as_request_squad(players):
    # Selling price equals today's price for a squad bought today. The caller
    # reconstructs real selling prices from the transfer log; the optimizer just
    # takes what it is given.
    return [
        SquadPlayer(element_id=p.element_id, selling_price=p.price, purchase_price=p.price)
        for p in players
    ]


def run(squad, *, bank=0, free_transfers=1, max_ownership=None):
    ids = [p.element_id for p in squad]
    pool = candidate_pool(SNAPSHOT, ids, GWS, max_ownership=max_ownership)
    return recommend(
        as_request_squad(squad),
        pool,
        SNAPSHOT.players,
        bank=bank,
        free_transfers=free_transfers,
        gws=GWS,
    )


def resulting_squad(plan, squad):
    """The 15 a plan leaves you with."""
    out = {t.element_id for t in plan.transfers_out}
    ids = [p.element_id for p in squad if p.element_id not in out]
    ids += [t.element_id for t in plan.transfers_in]
    return [SNAPSHOT.players[e] for e in ids]


def test_lineup_is_a_legal_eleven():
    lineup = pick_lineup(build_squad(), GWS)

    assert len(lineup.xi) == 11
    assert len(lineup.bench_order) == 4
    assert not set(lineup.xi) & set(lineup.bench_order)

    counts: dict[int, int] = {}
    for eid in lineup.xi:
        etype = SNAPSHOT.players[eid].element_type
        counts[etype] = counts.get(etype, 0) + 1
    for etype, minimum in XI_MIN.items():
        assert minimum <= counts.get(etype, 0) <= XI_MAX[etype]

    assert lineup.captain in lineup.xi
    assert lineup.vice_captain in lineup.xi
    assert lineup.captain != lineup.vice_captain
    # The reserve keeper takes the fixed first bench slot.
    assert SNAPSHOT.players[lineup.bench_order[0]].element_type == 1


def test_hold_plan_is_always_returned():
    _, plans = run(build_squad(), bank=200)

    holds = [p for p in plans if not p.transfers_in and not p.transfers_out]
    assert len(holds) == 1
    hold = holds[0]
    assert hold.label == "Hold"
    assert hold.delta_xp == 0.0
    assert hold.hit_cost == 0
    assert len(hold.xi) == 11
    # The explanation is more the product than the verdict.
    assert len(hold.reasoning) > 40
    assert all(plan.reasoning for plan in plans)


def test_plans_are_ranked_best_first():
    _, plans = run(build_squad(), bank=200)
    assert 2 <= len(plans) <= 3
    assert plans == sorted(plans, key=lambda p: -p.delta_xp)


def test_transfer_never_exceeds_budget():
    for bank in (0, 3, 40):
        squad = build_squad()
        selling = {p.element_id: p.price for p in squad}
        _, plans = run(squad, bank=bank)
        for plan in plans:
            for t_in, t_out in zip(plan.transfers_in, plan.transfers_out):
                assert t_in.price <= selling[t_out.element_id] + bank, (
                    f"bought {t_in.price} with {selling[t_out.element_id] + bank} available"
                )


def test_club_rule_is_never_broken_by_a_suggestion():
    # Club 5 holds the premium, so with three of its players already held the
    # obvious buy is illegal and the solver has to find another.
    squad = build_squad(preferred_club=5)
    held_from_5 = sum(1 for p in squad if p.team_fpl_id == 5)
    assert held_from_5 == MAX_PER_CLUB  # the rule genuinely binds in this fixture

    premium = SNAPSHOT.players[PREMIUM_ID]
    assert premium.team_fpl_id == 5

    # Positive control: from a squad with no club-5 players, that same premium is
    # the recommendation. So the assertion below is the rule binding, not the
    # solver failing to find him.
    _, control = run(build_squad(), bank=300)
    assert PREMIUM_ID in {
        t.element_id for plan in control for t in plan.transfers_in
    }

    _, plans = run(squad, bank=300)
    for plan in plans:
        counts: dict[int, int] = {}
        for p in resulting_squad(plan, squad):
            counts[p.team_fpl_id] = counts.get(p.team_fpl_id, 0) + 1
        assert max(counts.values()) <= MAX_PER_CLUB
        # Specifically, the highest-xP affordable forward is not recommended,
        # because buying him would be a fourth from his club.
        assert PREMIUM_ID not in {t.element_id for t in plan.transfers_in}


def test_squad_composition_is_preserved():
    squad = build_squad()
    _, plans = run(squad, bank=200)
    for plan in plans:
        counts: dict[int, int] = {}
        for p in resulting_squad(plan, squad):
            counts[p.element_type] = counts.get(p.element_type, 0) + 1
        assert counts == SQUAD_QUOTA


def test_validate_squad_names_the_actual_problem():
    squad = build_squad()

    with pytest.raises(SquadError, match="14 players"):
        validate_squad(squad[:-1])

    # Swap a forward for an extra defender.
    extra_def = next(
        p
        for p in SNAPSHOT.players.values()
        if p.element_type == 2 and p not in squad
    )
    broken = [p for p in squad if p.element_type != 4] + [extra_def] + [
        p for p in squad if p.element_type == 4
    ][:2]
    with pytest.raises(SquadError, match="6 defenders, expected 5"):
        validate_squad(broken)

    dupes = squad[:-1] + [squad[0]]
    with pytest.raises(SquadError, match="duplicate"):
        validate_squad(dupes)


def test_validate_squad_catches_a_fourth_from_one_club():
    squad = build_squad(preferred_club=5)
    victim = next(p for p in squad if p.team_fpl_id != 5)
    fourth = next(
        p
        for p in SNAPSHOT.players.values()
        if p.team_fpl_id == 5
        and p.element_type == victim.element_type
        and p not in squad
    )
    broken = [p for p in squad if p is not victim] + [fourth]
    with pytest.raises(SquadError, match="more than 3 players from one club"):
        validate_squad(broken)


def test_a_hit_is_charged_and_must_still_pay_for_itself():
    squad = build_squad()
    _, plans = run(squad, bank=200, free_transfers=0)
    moves = [p for p in plans if p.transfers_in]
    assert moves, "the seed data has upgrades worth a hit"
    for plan in moves:
        assert plan.hit_cost == 4
        assert plan.label == "Take a -4"
        # delta_xp is reported after the hit is paid, so a plan that survives
        # ranking genuinely beats holding.
        assert plan.delta_xp > 0


def test_pool_prefilter_caps_each_position_and_keeps_the_squad():
    squad = build_squad()
    ids = {p.element_id for p in squad}
    pool = candidate_pool(SNAPSHOT, ids, GWS)

    by_type: dict[int, int] = {}
    for p in pool:
        by_type[p.element_type] = by_type.get(p.element_type, 0) + 1
    # Defenders and midfielders are over the cap in the seed data, so the filter
    # is actually doing something here.
    assert by_type[2] <= DEFAULT_POOL_PER_POSITION + SQUAD_QUOTA[2]
    assert by_type[3] <= DEFAULT_POOL_PER_POSITION + SQUAD_QUOTA[3]
    assert len(pool) < len(SNAPSHOT.players)
    assert ids <= {p.element_id for p in pool}


def test_pool_keeps_held_players_even_in_differential_mode():
    squad = build_squad()
    # Hold the premium, then ask for differentials. You cannot reason about a
    # squad while pretending one of its members does not exist.
    squad = [p for p in squad if p.element_type != 4][:12] + [
        SNAPSHOT.players[PREMIUM_ID]
    ] + [p for p in squad if p.element_type == 4][:2]
    ids = {p.element_id for p in squad}
    pool = candidate_pool(SNAPSHOT, ids, GWS, max_ownership=10.0)

    assert PREMIUM_ID in {p.element_id for p in pool}
    assert all(
        p.ownership <= 10.0 for p in pool if p.element_id not in ids
    )


def test_per_gameweek_breakdown_covers_the_horizon():
    _, plans = run(build_squad(), bank=200)
    for plan in plans:
        assert [b.gw for b in plan.per_gw_breakdown] == GWS
        for b in plan.per_gw_breakdown:
            assert b.captain_element_id in plan.xi
            # Doubles and blanks both exist; a fixture count is never negative.
            assert set(b.n_fixtures) == set(plan.xi)
            assert all(n >= 0 for n in b.n_fixtures.values())
