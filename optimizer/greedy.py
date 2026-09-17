"""The day-one solver: lineup selection plus a one-transfer greedy search.

ARCHITECTURE.md calls for this before the MIP exists, so the pipeline is real end
to end -- screenshot to snapshot to recommendation to UI -- and everything after
is an improvement to one function behind a fixed contract.

It is deliberately not clever. It picks the best legal XI for a given 15, and it
evaluates every single swap of one held player for one pool player in the same
position that the budget, the max-3-per-club rule and squad composition allow.
That is a few thousand evaluations, which is milliseconds, and it reduces to the
rule in ARCHITECTURE.md -- sell the worst starter, buy the best affordable
replacement -- without hard-coding it.

What it does not do, and what the MIP in solver.py is for: multi-transfer moves,
planning a transfer for a future gameweek, chips, or price-change timing.

All money is integer tenths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from optimizer.contract import GameweekBreakdown, Plan, SquadPlayer, Transfer
from optimizer.pool import DEF, FWD, GKP, MID, POSITION_NAMES, PlayerXP

SQUAD_QUOTA = {GKP: 2, DEF: 5, MID: 5, FWD: 3}
SQUAD_SIZE = 15
XI_SIZE = 11
MAX_PER_CLUB = 3

# A legal XI is exactly one keeper, and at least 3/2/1 outfield.
XI_MIN = {GKP: 1, DEF: 3, MID: 2, FWD: 1}
XI_MAX = {GKP: 1, DEF: 5, MID: 5, FWD: 3}

# ARCHITECTURE.md's cheap bench layer: bench players enter the objective at a
# fraction of their xP, first slot weighted higher than the fourth. It buys most
# of what full autosub modelling would, for one line of tuning. Slot 0 is the
# reserve keeper, who almost never plays -- hence the low weight there.
BENCH_WEIGHTS = (0.04, 0.16, 0.10, 0.05)

HIT_COST = 4  # points per transfer beyond the free ones


class SquadError(ValueError):
    """A squad that cannot be reasoned about. Message names the actual problem."""


@dataclass(frozen=True)
class Lineup:
    xi: list[int]
    bench_order: list[int]
    captain: int
    vice_captain: int
    objective: float  # XI + captaincy + discounted bench, over the horizon
    xi_xp: float  # XI + captaincy only -- what the manager actually expects to score


def validate_squad(players: Sequence[PlayerXP]) -> None:
    """Reject anything that is not a legal FPL squad, naming what is wrong.

    Done here rather than in the request model because pydantic can check the
    count but not composition -- composition needs to know each player's position
    and club, which only the xP snapshot knows.
    """
    if len(players) != SQUAD_SIZE:
        raise SquadError(f"squad has {len(players)} players, expected {SQUAD_SIZE}")

    ids = [p.element_id for p in players]
    if len(set(ids)) != len(ids):
        dupes = sorted({e for e in ids if ids.count(e) > 1})
        raise SquadError(f"squad contains duplicate players: element_ids {dupes}")

    for etype, want in SQUAD_QUOTA.items():
        have = sum(1 for p in players if p.element_type == etype)
        if have != want:
            raise SquadError(
                f"squad has {have} {POSITION_NAMES[etype]}s, expected {want}"
            )

    counts: dict[int, int] = {}
    for p in players:
        counts[p.team_fpl_id] = counts.get(p.team_fpl_id, 0) + 1
    over = sorted(t for t, n in counts.items() if n > MAX_PER_CLUB)
    if over:
        detail = ", ".join(f"team {t} has {counts[t]}" for t in over)
        raise SquadError(f"more than {MAX_PER_CLUB} players from one club: {detail}")


def pick_lineup(squad: Sequence[PlayerXP], gws: Sequence[int]) -> Lineup:
    """Best legal XI, captain, vice and bench order for a fixed 15.

    Greedy is exact here: fill the mandatory slots with the best player available
    for each, then take the best remaining players for the free slots subject to
    the per-position caps. With a fixed 15 and a linear objective there is nothing
    a search could find that this misses.
    """
    scored = sorted(squad, key=lambda p: (-p.horizon_xp(gws), p.element_id))
    by_type: dict[int, list[PlayerXP]] = {}
    for p in scored:
        by_type.setdefault(p.element_type, []).append(p)

    xi: list[PlayerXP] = []
    used: set[int] = set()
    counts = {etype: 0 for etype in SQUAD_QUOTA}

    for etype, need in XI_MIN.items():
        for p in by_type.get(etype, [])[:need]:
            xi.append(p)
            used.add(p.element_id)
            counts[etype] += 1

    for p in scored:
        if len(xi) == XI_SIZE:
            break
        if p.element_id in used or counts[p.element_type] >= XI_MAX[p.element_type]:
            continue
        xi.append(p)
        used.add(p.element_id)
        counts[p.element_type] += 1

    xi.sort(key=lambda p: (-p.horizon_xp(gws), p.element_id))
    captain = xi[0]
    vice = xi[1]

    # Bench order is FPL's: the reserve keeper sits in a fixed slot, then the
    # three outfield subs in the order autosubs should try them.
    bench = [p for p in scored if p.element_id not in used]
    bench_gk = [p for p in bench if p.element_type == GKP]
    bench_out = [p for p in bench if p.element_type != GKP]
    bench_ordered = bench_gk + bench_out

    xi_xp = sum(p.horizon_xp(gws) for p in xi) + captain.horizon_xp(gws)
    objective = xi_xp + sum(
        w * p.horizon_xp(gws) for w, p in zip(BENCH_WEIGHTS, bench_ordered)
    )

    return Lineup(
        xi=[p.element_id for p in xi],
        bench_order=[p.element_id for p in bench_ordered],
        captain=captain.element_id,
        vice_captain=vice.element_id,
        objective=objective,
        xi_xp=xi_xp,
    )


def _breakdown(
    squad_by_id: dict[int, PlayerXP], lineup: Lineup, gws: Sequence[int]
) -> list[GameweekBreakdown]:
    """Per-gameweek detail. The captain can differ from the horizon captain in any
    single gameweek -- a double gameweek is exactly when that happens -- so it is
    recomputed per gameweek here rather than copied from the Plan."""
    out = []
    for gw in gws:
        xi = [squad_by_id[e] for e in lineup.xi]
        cap = max(xi, key=lambda p: (p.xp(gw), -p.element_id))
        out.append(
            GameweekBreakdown(
                gw=gw,
                xp=round(sum(p.xp(gw) for p in xi) + cap.xp(gw), 3),
                captain_element_id=cap.element_id,
                n_fixtures={p.element_id: p.fixtures(gw) for p in xi},
            )
        )
    return out


def _club_counts(players: Iterable[PlayerXP]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for p in players:
        counts[p.team_fpl_id] = counts.get(p.team_fpl_id, 0) + 1
    return counts


def _hold_reasoning(squad_by_id: dict[int, PlayerXP], lineup: Lineup, free: int) -> str:
    cap = squad_by_id[lineup.captain]
    vice = squad_by_id[lineup.vice_captain]
    bank_note = (
        "Rolling the transfer keeps two next week, which is worth more than a "
        "marginal upgrade now."
        if free < 2
        else "You are already at the transfer cap, so a move only makes sense if it clearly gains."
    )
    return (
        f"No transfer. Captain {cap.web_name}, vice {vice.web_name}. "
        f"{bank_note} This plan is the baseline every other plan is measured against."
    )


def _transfer_reasoning(
    out_p: PlayerXP,
    in_p: PlayerXP,
    delta: float,
    hit: int,
    bank_after: int,
    was_starting: bool,
) -> str:
    role = "your weakest starter" if was_starting else "a bench player"
    money = (
        f"{in_p.web_name} costs {in_p.price / 10:.1f}m against {out_p.web_name}'s "
        f"{out_p.price / 10:.1f}m selling price, leaving {bank_after / 10:.1f}m in the bank."
    )
    hit_note = (
        f" That is a -{hit} hit, and the move still gains {delta:.2f} points after paying it."
        if hit
        else " No hit: this is within your free transfers."
    )
    return (
        f"Sell {out_p.web_name} ({role}) and buy {in_p.web_name}. {money}{hit_note} "
        f"Net gain over holding is {delta:.2f} expected points across the horizon."
    )


def recommend(
    squad: Sequence[SquadPlayer],
    pool: Sequence[PlayerXP],
    snapshot_players: dict[int, PlayerXP],
    *,
    bank: int,
    free_transfers: int,
    gws: Sequence[int],
    max_plans: int = 3,
) -> tuple[float, list[Plan]]:
    """Return (baseline_xp, ranked plans). A Hold plan is always among them.

    Holding is frequently optimal and a tool that never says so does not get
    believed, so Hold is a first-class result rather than a fallback -- it is
    returned even when a transfer beats it, and it is ranked honestly.
    """
    held = [snapshot_players[s.element_id] for s in squad]
    validate_squad(held)

    selling = {s.element_id: s.selling_price for s in squad}
    held_by_id = {p.element_id: p for p in held}

    baseline = pick_lineup(held, gws)
    plans: list[tuple[float, Plan]] = []

    hold = Plan(
        label="Hold",
        xi=baseline.xi,
        bench_order=baseline.bench_order,
        captain=baseline.captain,
        vice_captain=baseline.vice_captain,
        delta_xp=0.0,
        per_gw_breakdown=_breakdown(held_by_id, baseline, gws),
        reasoning=_hold_reasoning(held_by_id, baseline, free_transfers),
    )

    hit = 0 if free_transfers >= 1 else HIT_COST
    base_clubs = _club_counts(held)
    candidates = [p for p in pool if p.element_id not in held_by_id]

    # Candidates sorted by xP per position, so the first affordable legal buy for
    # a given sale is also the best one -- the greedy rule, stated as a loop.
    by_type: dict[int, list[PlayerXP]] = {}
    for p in candidates:
        by_type.setdefault(p.element_type, []).append(p)
    for players in by_type.values():
        players.sort(key=lambda p: (-p.horizon_xp(gws), p.element_id))

    scored: list[tuple[float, Plan]] = []
    for out_p in held:
        budget = bank + selling[out_p.element_id]
        for in_p in by_type.get(out_p.element_type, []):
            if in_p.price > budget:
                continue
            # Composition is preserved by construction (same position), so only
            # the club rule can be violated by a like-for-like swap.
            if in_p.team_fpl_id != out_p.team_fpl_id:
                if base_clubs.get(in_p.team_fpl_id, 0) + 1 > MAX_PER_CLUB:
                    continue

            new_squad = [p for p in held if p.element_id != out_p.element_id] + [in_p]
            lineup = pick_lineup(new_squad, gws)
            delta = lineup.objective - baseline.objective - hit
            if delta <= 0:
                continue

            new_by_id = {p.element_id: p for p in new_squad}
            bank_after = budget - in_p.price
            scored.append(
                (
                    delta,
                    Plan(
                        label="One transfer" if not hit else f"Take a -{hit}",
                        transfers_out=[
                            Transfer(
                                element_id=out_p.element_id,
                                web_name=out_p.web_name,
                                price=selling[out_p.element_id],
                            )
                        ],
                        transfers_in=[
                            Transfer(
                                element_id=in_p.element_id,
                                web_name=in_p.web_name,
                                price=in_p.price,
                            )
                        ],
                        hit_cost=hit,
                        xi=lineup.xi,
                        bench_order=lineup.bench_order,
                        captain=lineup.captain,
                        vice_captain=lineup.vice_captain,
                        delta_xp=round(delta, 3),
                        per_gw_breakdown=_breakdown(new_by_id, lineup, gws),
                        reasoning=_transfer_reasoning(
                            out_p,
                            in_p,
                            delta,
                            hit,
                            bank_after,
                            out_p.element_id in baseline.xi,
                        ),
                    ),
                )
            )

    scored.sort(key=lambda item: -item[0])

    # Distinct ideas only. Three routes to the same signing, or three ways to
    # move the same player on, is one idea presented three times -- and a list of
    # near-identical plans is how a tool teaches people to ignore its plans.
    seen_out: set[int] = set()
    seen_in: set[int] = set()
    for delta, plan in scored:
        out_id = plan.transfers_out[0].element_id
        in_id = plan.transfers_in[0].element_id
        if out_id in seen_out or in_id in seen_in:
            continue
        seen_out.add(out_id)
        seen_in.add(in_id)
        plans.append((delta, plan))
        if len(plans) >= max_plans - 1:
            break

    plans.append((0.0, hold))
    plans.sort(key=lambda item: -item[0])
    ranked = [plan for _, plan in plans]

    if len(ranked) == 1:
        # Nothing legal and affordable beats holding. Say so, rather than
        # silently returning a bare Hold that looks like a missing answer.
        hold.reasoning += (
            " No affordable transfer in your squad's positions improves on this,"
            " so holding is the recommendation, not a default."
        )

    return round(baseline.xi_xp, 3), ranked
