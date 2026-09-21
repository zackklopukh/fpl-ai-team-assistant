"""The MIP squad solver: mixed-integer linear programming with PuLP driving CBC.

ARCHITECTURE.md splits the optimizer in two, and this is the half with a right
answer. The xPoints model is statistics and always improvable; the solver is
optimization and is either correct or it is not. Keeping them apart means a bug
in one never requires touching the other, so nothing in this module knows where
an xP number came from or believes anything about it.

This module is pure. No FastAPI, no HTTP, no database, no clock beyond measuring
its own runtime, no file access. It takes numbers in and returns `Plan` objects
from `contract.py`. The service wraps it; it does not wrap the service.

THE FORMULATION
---------------
Per player p and gameweek g in the horizon:

    squad[p][g]     binary   p is one of the 15
    starting[p][g]  binary   p is one of the 11        starting <= squad
    captain[p][g]   binary   p wears the armband       captain  <= starting
    buy[p][g]       binary   p entered the squad at g
    sell[p][g]      binary   p left the squad at g

    squad[p][g] - squad[p][g-1] == buy[p][g] - sell[p][g]

with squad[p][current_gw - 1] fixed to the squad the caller holds today.

maximise   sum_g decay^(g-g0) * [ sum_p xp[p][g] * (starting[p][g] + captain[p][g])
                                + bench term (see below)
                                - 4 * hits[g] ]

subject to, for every gameweek:

    15 in the squad; 2 GKP / 5 DEF / 5 MID / 3 FWD; at most 3 from any one club
    11 starting, in a legal formation: 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD
    exactly one captain
    bank[g] = bank[g-1] + sum(selling price of players sold) - sum(price of players bought)
    bank[g] >= 0

The budget constraint is the one people get wrong: cash freed is the *selling*
price of a player sold, not what they cost to buy. FPL lets the seller keep half
of any rise, floored to the nearest 0.1, so the two differ for anyone who has
risen. The caller reconstructs selling price from the transfer log and passes it
in on the squad; this module never recomputes it.

THE HIT GOES INSIDE THE OBJECTIVE
---------------------------------
`- 4 * hits[g]` is a term in the objective, not a subtraction applied to the
answer afterwards. Applied afterwards, the solver maximises raw points, happily
takes four transfers to gain three, and then the report says -9. Inside, a
transfer has to pay for itself before the solver will touch it.

Free transfers carry over, capped at 5. That is genuinely stateful across
gameweeks, so it is modelled:

    ft[g0] = free_transfers                        (given)
    hits[g] >= transfers_in[g] - ft[g],  hits >= 0
    rollover[g] >= 0
    rollover[g] <= ft[g] - transfers_in[g] + hits[g]
    ft[g+1] <= rollover[g] + 1,  ft[g+1] <= 5

The third constraint is the linearisation of rollover = max(ft - t_in, 0). It
leans on hits being pushed down by the objective: inflating hits by one to
manufacture a free transfer costs 4 points and can save at most 4 points later,
so it is never strictly profitable and the relaxation is tight at any optimum.

THE BENCH, CHEAPLY
------------------
Modelling autosubs properly means the joint probability that a starter blanks and
a specific bench player is eligible to replace them in a legal formation. That is
a lot of machinery for a small points gain, so instead bench players enter the
objective at a fraction of their xP, with the first bench slot worth more than
the last -- `BENCH_WEIGHTS`, one tunable constant. It captures the thing that
actually matters, which is preferring a playing bench defender over a nailed-on
non-starter.

The three outfield bench slots are ordered by assignment variables that are
*continuous* in [0, 1], not binary. For a fixed integral choice of who is on the
bench, the slot assignment is a transportation problem with a totally unimodular
constraint matrix, so the LP relaxation is integral on its own. The ordering
therefore costs columns and no branching. The bench keeper needs no variable at
all: with exactly 2 keepers in the squad and exactly 1 starting, the identity of
the benched one is forced.

WHY THE POOL ARRIVES PRE-FILTERED
---------------------------------
With ~700 players over 5 gameweeks CBC runs for minutes. `pool.candidate_pool`
shrinks that to roughly the top 150 by xP per position plus everyone currently
held, which is the single biggest performance lever and costs almost nothing in
quality, because the optimal transfer target is never the 300th-best midfielder.
This module does no filtering and does not import that one -- the caller does the
shrinking and passes the result in as `prices`. Anything not in `prices` does not
exist as far as the solver is concerned, so every held player must be there.

WHY THERE IS A TIME LIMIT AND WHAT TRUNCATION MEANS
---------------------------------------------------
MIP solvers find good solutions early and then spend the rest of their time
proving them optimal. No user has ever wanted that proof. Every solve gets a
deadline and returns its best incumbent. `SolveResult.truncated` says whether any
solve stopped early, so the caller can set `truncated` on the response and the UI
can say "best found in 20s" rather than implying a proof it does not have.

WHY MORE THAN ONE PLAN
----------------------
"Hold your transfer" is frequently optimal and a tool that never says it does not
get believed, so the zero-transfer baseline is always solved and always returned.
Alternatives come from re-solving with the previous plan's transfer set excluded
by a no-good cut. Asking a MIP for its second-best solution any other way gets
you a near-duplicate that differs by one bench slot.

All money is integer tenths. 55 means 5.5m.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import pulp

from optimizer.contract import GameweekBreakdown, Plan, Transfer

# FPL's element_type. Matches pool.py and db/schema.sql; duplicated rather than
# imported so this module depends on nothing but the contract.
GKP, DEF, MID, FWD = 1, 2, 3, 4

SQUAD_SIZE = 15
XI_SIZE = 11
SQUAD_BY_POSITION = {GKP: 2, DEF: 5, MID: 5, FWD: 3}
# Legal XI formations, as (min, max) starters per position.
XI_BY_POSITION = {GKP: (1, 1), DEF: (3, 5), MID: (2, 5), FWD: (1, 3)}
MAX_PER_CLUB = 3

POINTS_PER_HIT = 4
MAX_FREE_TRANSFERS = 5  # Carry-over cap, 2026-27 rules.

# The bench layer. Slots 0-2 are the ordered outfield bench; slot 3 is the
# reserve keeper, who comes on least often of all and so is worth least.
# Tune this and nothing else to change how much the solver cares about the bench.
BENCH_WEIGHTS: tuple[float, float, float, float] = (0.20, 0.13, 0.09, 0.04)

DEFAULT_TIME_LIMIT = 20.0  # Seconds, for the whole call, across every solve.
DEFAULT_N_PLANS = 3


class SolverError(Exception):
    """The inputs cannot describe a squad, or no legal squad exists."""


@dataclass(frozen=True)
class PlayerMeta:
    """What the solver needs to know about a player besides their xP.

    Field names match `pool.PlayerXP` deliberately, so the output of
    `pool.candidate_pool` can be handed straight to `solve` without translation.
    Anything exposing these five attributes works; this class exists so tests and
    callers without a pool snapshot have something to construct.
    """

    element_id: int
    web_name: str
    team_fpl_id: int
    element_type: int
    price: int  # Integer tenths. Buying price -- selling price arrives on the squad.


@dataclass(frozen=True)
class SolveResult:
    """Ranked plans plus the facts the caller needs to build a truthful response.

    `solve` returns this rather than a bare `list[Plan]` because `truncated` has
    nowhere else to live: it is a property of the search, not of any one plan, and
    `OptimizeResponse.truncated` has to come from somewhere. Iterating or indexing
    a SolveResult walks `plans`, so caller code written against a list still works.
    """

    plans: list[Plan]
    baseline_xp: float
    truncated: bool
    solve_ms: int

    def __iter__(self):
        return iter(self.plans)

    def __len__(self) -> int:
        return len(self.plans)

    def __getitem__(self, index):
        return self.plans[index]


@dataclass
class _Held:
    """A player the caller holds today."""

    element_id: int
    selling_price: int


@dataclass
class _Solution:
    """One raw MIP answer, before it is dressed up as a Plan."""

    objective: float  # Includes bench weights; used for ranking only.
    honest_xp: float  # Starters + captain - hits. What the user would actually score.
    squad_by_gw: dict[int, set[int]]
    xi_by_gw: dict[int, list[int]]
    captain_by_gw: dict[int, int]
    hits_by_gw: dict[int, int]
    bought_by_gw: dict[int, list[int]]
    sold_by_gw: dict[int, list[int]]
    truncated: bool
    # Who this plan buys at the current gameweek. That is what a no-good cut
    # excludes -- see _apply_cuts for why it is the buys and not the sales.
    cut_key: tuple[int, ...] = field(default=())
    # Every gameweek's moves. Two plans that agree on this week but diverge later
    # are different advice ("roll it" vs "never move again"), so dedup uses this.
    signature: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = field(default=())


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _normalise_squad(squad: Iterable[Any]) -> list[_Held]:
    held: list[_Held] = []
    for row in squad:
        if isinstance(row, Mapping):
            eid = int(row["element_id"])
            sell = int(row["selling_price"])
        else:
            eid = int(row.element_id)
            sell = int(row.selling_price)
        held.append(_Held(eid, sell))
    if len(held) != SQUAD_SIZE:
        raise SolverError(f"a squad is {SQUAD_SIZE} players, got {len(held)}")
    if len({h.element_id for h in held}) != SQUAD_SIZE:
        raise SolverError("duplicate element_id in squad")
    return held


def _normalise_pool(prices: Any) -> dict[int, PlayerMeta]:
    """Accept a mapping of element_id -> meta, or any iterable of player objects.

    `pool.candidate_pool` returns a list of PlayerXP; a caller assembling input by
    hand finds a dict more natural. Both arrive here as the same dict.
    """
    rows = prices.values() if isinstance(prices, Mapping) else prices
    out: dict[int, PlayerMeta] = {}
    for row in rows:
        if isinstance(row, Mapping):
            meta = PlayerMeta(
                element_id=int(row["element_id"]),
                web_name=str(row.get("web_name", "")),
                team_fpl_id=int(row["team_fpl_id"]),
                element_type=int(row["element_type"]),
                price=int(row["price"]),
            )
        else:
            meta = PlayerMeta(
                element_id=int(row.element_id),
                web_name=str(getattr(row, "web_name", "")),
                team_fpl_id=int(row.team_fpl_id),
                element_type=int(row.element_type),
                price=int(row.price),
            )
        if meta.element_type not in SQUAD_BY_POSITION:
            raise SolverError(f"player {meta.element_id} has element_type {meta.element_type}")
        out[meta.element_id] = meta
    if not out:
        raise SolverError("empty player pool")
    return out


def _normalise_xp(
    xp_matrix: Any, pool: Mapping[int, PlayerMeta], gws: Sequence[int], source: Any
) -> dict[int, dict[int, float]]:
    """xP as element_id -> gw -> points.

    A gameweek with no entry is worth nothing rather than being an error: a blank
    gameweek and a player outside the model's horizon are the same thing to a
    solver, and both are legitimately zero.
    """
    xp: dict[int, dict[int, float]] = {}
    if xp_matrix is None:
        # Fall back to xP carried on the pool objects themselves (PlayerXP does).
        rows = source.values() if isinstance(source, Mapping) else source
        by_id = {}
        for row in rows:
            eid = int(row["element_id"]) if isinstance(row, Mapping) else int(row.element_id)
            raw = row.get("xp_by_gw") if isinstance(row, Mapping) else getattr(row, "xp_by_gw", None)
            if raw is None:
                raise SolverError("no xp_matrix given and pool players carry no xp_by_gw")
            by_id[eid] = raw
        xp_matrix = by_id
    for eid in pool:
        row = xp_matrix.get(eid) or {}
        xp[eid] = {gw: float(row.get(gw, 0.0)) for gw in gws}
    return xp


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _build(
    *,
    pool: Mapping[int, PlayerMeta],
    xp: Mapping[int, Mapping[int, float]],
    held: Sequence[_Held],
    gws: Sequence[int],
    bank: int,
    free_transfers: int,
    bench_weights: Sequence[float],
    decay: float,
    max_transfers_per_gw: int | None,
    forbid_transfers: bool,
) -> tuple[pulp.LpProblem, dict[str, Any]]:
    """Assemble the MIP. Returns the problem and the variable handles to read back."""
    ids = sorted(pool)
    held_ids = {h.element_id for h in held}
    # What each player fetches if sold. A held player has a selling price the
    # caller reconstructed; anyone else was bought at list price during the
    # horizon and so sells for what they cost, since we model no price movement.
    sell_price = {eid: pool[eid].price for eid in ids}
    for h in held:
        sell_price[h.element_id] = h.selling_price

    by_position: dict[int, list[int]] = {pos: [] for pos in SQUAD_BY_POSITION}
    by_club: dict[int, list[int]] = {}
    for eid in ids:
        by_position[pool[eid].element_type].append(eid)
        by_club.setdefault(pool[eid].team_fpl_id, []).append(eid)

    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    squad = pulp.LpVariable.dicts("squad", (ids, gws), cat=pulp.LpBinary)
    start = pulp.LpVariable.dicts("start", (ids, gws), cat=pulp.LpBinary)
    capt = pulp.LpVariable.dicts("capt", (ids, gws), cat=pulp.LpBinary)
    buy = pulp.LpVariable.dicts("buy", (ids, gws), cat=pulp.LpBinary)
    sell = pulp.LpVariable.dicts("sell", (ids, gws), cat=pulp.LpBinary)

    # Ordered outfield bench slots. Continuous on purpose -- see the module
    # docstring: the assignment polytope is integral for any fixed bench.
    outfield = [eid for eid in ids if pool[eid].element_type != GKP]
    bslot = pulp.LpVariable.dicts("bslot", (outfield, [0, 1, 2], gws), 0, 1, cat=pulp.LpContinuous)

    hits = pulp.LpVariable.dicts("hits", gws, 0, SQUAD_SIZE, cat=pulp.LpInteger)
    rollover = pulp.LpVariable.dicts("roll", gws, 0, MAX_FREE_TRANSFERS, cat=pulp.LpContinuous)
    ft = {gws[0]: float(free_transfers)}
    for gw in gws[1:]:
        ft[gw] = pulp.LpVariable(f"ft_{gw}", 0, MAX_FREE_TRANSFERS, cat=pulp.LpInteger)
    money = pulp.LpVariable.dicts("bank", gws, 0, None, cat=pulp.LpContinuous)

    for i, gw in enumerate(gws):
        prev_squad = (lambda eid, g=gws[i - 1]: squad[eid][g]) if i else (
            lambda eid: 1 if eid in held_ids else 0
        )
        prev_bank = money[gws[i - 1]] if i else float(bank)

        # --- squad composition -------------------------------------------------
        prob += pulp.lpSum(squad[e][gw] for e in ids) == SQUAD_SIZE, f"size_{gw}"
        for pos, n in SQUAD_BY_POSITION.items():
            prob += pulp.lpSum(squad[e][gw] for e in by_position[pos]) == n, f"pos_{pos}_{gw}"
        for club, members in by_club.items():
            if len(members) > MAX_PER_CLUB:
                prob += pulp.lpSum(squad[e][gw] for e in members) <= MAX_PER_CLUB, f"club_{club}_{gw}"

        # --- the eleven --------------------------------------------------------
        prob += pulp.lpSum(start[e][gw] for e in ids) == XI_SIZE, f"xi_{gw}"
        for pos, (lo, hi) in XI_BY_POSITION.items():
            members = by_position[pos]
            prob += pulp.lpSum(start[e][gw] for e in members) >= lo, f"form_lo_{pos}_{gw}"
            prob += pulp.lpSum(start[e][gw] for e in members) <= hi, f"form_hi_{pos}_{gw}"
        prob += pulp.lpSum(capt[e][gw] for e in ids) == 1, f"capt_{gw}"
        for e in ids:
            prob += start[e][gw] <= squad[e][gw], f"start_le_squad_{e}_{gw}"
            prob += capt[e][gw] <= start[e][gw], f"capt_le_start_{e}_{gw}"

        # --- ordered bench -----------------------------------------------------
        for slot in (0, 1, 2):
            prob += pulp.lpSum(bslot[e][slot][gw] for e in outfield) == 1, f"bslot_{slot}_{gw}"
        for e in outfield:
            prob += (
                pulp.lpSum(bslot[e][slot][gw] for slot in (0, 1, 2)) <= squad[e][gw] - start[e][gw],
                f"bench_only_{e}_{gw}",
            )

        # --- transfers ---------------------------------------------------------
        for e in ids:
            prob += squad[e][gw] - prev_squad(e) == buy[e][gw] - sell[e][gw], f"flow_{e}_{gw}"
            prob += buy[e][gw] + sell[e][gw] <= 1, f"nochurn_{e}_{gw}"
        n_in = pulp.lpSum(buy[e][gw] for e in ids)
        if forbid_transfers:
            prob += n_in == 0, f"hold_{gw}"
        if max_transfers_per_gw is not None:
            prob += n_in <= max_transfers_per_gw, f"maxin_{gw}"

        # --- money -------------------------------------------------------------
        prob += (
            money[gw]
            == prev_bank
            + pulp.lpSum(sell_price[e] * sell[e][gw] for e in ids)
            - pulp.lpSum(pool[e].price * buy[e][gw] for e in ids),
            f"bank_{gw}",
        )

        # --- free transfers and hits -------------------------------------------
        prob += hits[gw] >= n_in - ft[gw], f"hit_{gw}"
        prob += rollover[gw] <= ft[gw] - n_in + hits[gw], f"roll_{gw}"
        if i + 1 < len(gws):
            prob += ft[gws[i + 1]] <= rollover[gw] + 1, f"ftnext_{gw}"

    # --- objective -------------------------------------------------------------
    terms = []
    honest_terms = []
    for i, gw in enumerate(gws):
        w = decay**i
        points = pulp.lpSum(
            xp[e][gw] * (start[e][gw] + capt[e][gw]) for e in ids if xp[e][gw]
        ) - POINTS_PER_HIT * hits[gw]
        honest_terms.append(w * points)
        bench = pulp.lpSum(
            bench_weights[slot] * xp[e][gw] * bslot[e][slot][gw]
            for e in outfield
            for slot in (0, 1, 2)
            if xp[e][gw]
        ) + pulp.lpSum(
            bench_weights[3] * xp[e][gw] * (squad[e][gw] - start[e][gw])
            for e in by_position[GKP]
            if xp[e][gw]
        )
        terms.append(w * (points + bench))
    prob += pulp.lpSum(terms)

    handles = {
        "ids": ids,
        "squad": squad,
        "start": start,
        "capt": capt,
        "buy": buy,
        "sell": sell,
        "hits": hits,
        "honest": pulp.lpSum(honest_terms),
        "gws": gws,
    }
    return prob, handles


def _run(prob: pulp.LpProblem, handles: dict[str, Any], limit: float, msg: bool) -> _Solution | None:
    """Solve once and read the answer back. None when nothing feasible was found."""
    solver = pulp.PULP_CBC_CMD(msg=1 if msg else 0, timeLimit=max(1.0, limit))
    prob.solve(solver)
    if prob.sol_status not in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
        return None
    truncated = prob.sol_status != pulp.LpSolutionOptimal

    ids, gws = handles["ids"], handles["gws"]

    def on(var) -> bool:
        # CBC returns 0.9999999 for a binary that is on.
        return (var.value() or 0.0) > 0.5

    squad_by_gw, xi_by_gw, capt_by_gw = {}, {}, {}
    hits_by_gw, bought, sold = {}, {}, {}
    for gw in gws:
        squad_by_gw[gw] = {e for e in ids if on(handles["squad"][e][gw])}
        xi_by_gw[gw] = [e for e in ids if on(handles["start"][e][gw])]
        chosen = [e for e in ids if on(handles["capt"][e][gw])]
        capt_by_gw[gw] = chosen[0] if chosen else (xi_by_gw[gw][0] if xi_by_gw[gw] else 0)
        hits_by_gw[gw] = int(round(handles["hits"][gw].value() or 0.0))
        bought[gw] = sorted(e for e in ids if on(handles["buy"][e][gw]))
        sold[gw] = sorted(e for e in ids if on(handles["sell"][e][gw]))

    g0 = gws[0]
    return _Solution(
        objective=float(pulp.value(prob.objective) or 0.0),
        honest_xp=float(pulp.value(handles["honest"]) or 0.0),
        squad_by_gw=squad_by_gw,
        xi_by_gw=xi_by_gw,
        captain_by_gw=capt_by_gw,
        hits_by_gw=hits_by_gw,
        bought_by_gw=bought,
        sold_by_gw=sold,
        truncated=truncated,
        cut_key=tuple(bought[g0]),
        signature=tuple((tuple(bought[g]), tuple(sold[g])) for g in gws),
    )


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _bench_order(
    solution: _Solution, gw: int, pool: Mapping[int, PlayerMeta], xp: Mapping[int, Mapping[int, float]]
) -> list[int]:
    """The four bench slots: reserve keeper first, then outfield by descending xP.

    FPL numbers the bench 12-15 with the reserve keeper at 12, and the outfield
    autosub order is 13, 14, 15. Reading the order back off the slot variables
    would give the same answer, since the weights are strictly decreasing and the
    objective is a maximisation -- sorting is simply less to go wrong.
    """
    bench = solution.squad_by_gw[gw] - set(solution.xi_by_gw[gw])
    keepers = [e for e in bench if pool[e].element_type == GKP]
    outfield = sorted(
        (e for e in bench if pool[e].element_type != GKP),
        key=lambda e: (-xp[e][gw], e),
    )
    return keepers + outfield


def _label(n_transfers: int, hit: int, moves_later: bool) -> str:
    if n_transfers == 0:
        # Banking this week's transfer to spend two next week is real advice and
        # is not the same as never moving again, so it does not say "Hold".
        return "Roll your transfer" if moves_later else "Hold"
    if hit:
        return f"Take a -{hit}"
    return {1: "One transfer", 2: "Two transfers", 3: "Three transfers"}.get(
        n_transfers, f"{n_transfers} transfers"
    )


def _reasoning(solution: _Solution, gws: Sequence[int], delta: float) -> str:
    g0 = gws[0]
    n = len(solution.bought_by_gw[g0])
    later = sum(len(solution.bought_by_gw[g]) for g in gws[1:])
    # This function sees one plan, never the others, so it must not claim how a
    # plan ranks. "No transfer beats holding" used to appear here and read as
    # false whenever a transfer plan outscored the Hold printed just above it.
    if n == 0 and later == 0:
        bits = [
            f"Keep this squad unchanged over GW{g0}-{gws[-1]}. Every other plan's "
            "gain is measured against this one."
        ]
    elif n == 0:
        bits = [
            f"No transfer this week: bank it and use it later, worth {delta:+.2f} "
            "points over the horizon."
        ]
    else:
        bits = [f"{n} transfer{'s' if n != 1 else ''} now, worth {delta:+.2f} points over the horizon"]
        if solution.hits_by_gw[g0]:
            bits[0] += f" after a -{POINTS_PER_HIT * solution.hits_by_gw[g0]} hit"
        bits[0] += "."
    if later:
        bits.append(
            f"The plan assumes {later} further transfer{'s' if later != 1 else ''} "
            "later in the horizon; only this week's is committed."
        )
    return " ".join(bits)


def _to_plan(
    solution: _Solution,
    *,
    baseline_honest: float,
    pool: Mapping[int, PlayerMeta],
    xp: Mapping[int, Mapping[int, float]],
    fixtures: Mapping[int, Mapping[int, int]],
    gws: Sequence[int],
) -> Plan:
    g0 = gws[0]
    xi = sorted(solution.xi_by_gw[g0], key=lambda e: (pool[e].element_type, -xp[e][g0], e))
    captain = solution.captain_by_gw[g0]
    vice_candidates = sorted((e for e in xi if e != captain), key=lambda e: (-xp[e][g0], e))
    vice = vice_candidates[0] if vice_candidates else captain

    def transfer(eid: int, price: int) -> Transfer:
        return Transfer(element_id=eid, web_name=pool[eid].web_name, price=price)

    ins = [transfer(e, pool[e].price) for e in solution.bought_by_gw[g0]]
    outs = [transfer(e, pool[e].price) for e in solution.sold_by_gw[g0]]
    hit = POINTS_PER_HIT * solution.hits_by_gw[g0]
    delta = solution.honest_xp - baseline_honest
    moves_later = any(solution.bought_by_gw[g] for g in gws[1:])

    breakdown = []
    for gw in gws:
        squad_gw = solution.squad_by_gw[gw]
        cap = solution.captain_by_gw[gw]
        gw_xp = sum(xp[e][gw] for e in solution.xi_by_gw[gw]) + xp[cap][gw]
        breakdown.append(
            GameweekBreakdown(
                gw=gw,
                xp=round(gw_xp, 3),
                captain_element_id=cap,
                # Report only what the caller actually supplied. Defaulting a
                # missing player to 1 fixture would quietly turn a blank into a
                # normal gameweek in the UI, which is the one thing this field
                # exists to make visible.
                n_fixtures={
                    e: int(fixtures[e][gw])
                    for e in sorted(squad_gw)
                    if e in fixtures and gw in fixtures[e]
                },
            )
        )

    return Plan(
        label=_label(len(ins), hit, moves_later),
        transfers_in=ins,
        transfers_out=outs,
        hit_cost=hit,
        xi=xi,
        bench_order=_bench_order(solution, g0, pool, xp),
        captain=captain,
        vice_captain=vice,
        delta_xp=round(delta, 3),
        per_gw_breakdown=breakdown,
        reasoning=_reasoning(solution, gws, delta),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def solve(
    squad: Iterable[Any],
    xp_matrix: Mapping[int, Mapping[int, float]] | None,
    prices: Any,
    bank: int,
    free_transfers: int,
    current_gw: int,
    horizon: int = 3,
    *,
    fixtures: Mapping[int, Mapping[int, int]] | None = None,
    time_limit: float = DEFAULT_TIME_LIMIT,
    n_plans: int = DEFAULT_N_PLANS,
    bench_weights: Sequence[float] = BENCH_WEIGHTS,
    decay: float = 1.0,
    max_transfers_per_gw: int | None = None,
    msg: bool = False,
) -> SolveResult:
    """Rank courses of action for one squad over one horizon.

    Args:
        squad: the 15 players held, each with `element_id` and `selling_price`
            (integer tenths). `contract.SquadPlayer` works, as does a dict.
        xp_matrix: element_id -> gameweek -> expected points. A missing entry is
            zero, which is also how a blank gameweek arrives: the xP already
            accounts for how many fixtures a player has, so a double is simply a
            bigger number and a blank is 0. Pass None to read `xp_by_gw` off the
            pool objects instead, which is what `pool.PlayerXP` carries.
        prices: **the already-shrunk candidate pool**, as a mapping of
            element_id -> meta or any iterable of objects exposing `element_id`,
            `web_name`, `team_fpl_id`, `element_type` and `price` (integer tenths,
            buying price). `pool.candidate_pool(...)` output drops straight in.
            The solver does no filtering of its own -- hand it ~150 per position
            plus the held squad, or CBC will run for minutes. Every held player
            must appear here or the squad cannot be represented.
        bank: money in the bank, integer tenths.
        free_transfers: free transfers available this gameweek, 0-5.
        current_gw: the gameweek being planned for.
        horizon: how many gameweeks to plan over, starting at `current_gw`.
        fixtures: element_id -> gw -> fixture count, for the response breakdown
            only. It changes no decision; the xP matrix already encodes it.
        time_limit: seconds for the whole call, shared across every solve. The
            best incumbent is returned rather than proven optimal.
        n_plans: how many distinct plans to try for, including the hold.
        bench_weights: four multipliers -- outfield bench slots 1, 2, 3 then the
            reserve keeper. See `BENCH_WEIGHTS`.
        decay: per-gameweek discount on future gameweeks. 1.0 trusts the horizon
            equally throughout, which is what ARCHITECTURE.md specifies; below 1.0
            leans on the gameweek actually being committed.
        max_transfers_per_gw: optional cap, mostly to keep pathological inputs fast.
        msg: pass CBC's log through, for debugging a formulation change.

    Returns:
        A `SolveResult`: ranked `Plan`s best first and always including the hold,
        the baseline xP from holding, whether any solve hit its deadline, and the
        wall time.

    Raises:
        SolverError: the squad is not 15 players, a held player is missing from
            the pool, or no legal squad exists (usually an infeasible budget).

    Known simplification: a held player sold and later bought back inside the
    horizon is credited their original selling price on a second sale. No price
    movement is modelled, so this only bites on a sell-buy-sell cycle within five
    gameweeks, which is not a plan anyone should be following anyway.
    """
    started = time.perf_counter()
    if horizon < 1:
        raise SolverError("horizon must be at least 1")

    held = _normalise_squad(squad)
    pool = _normalise_pool(prices)
    missing = [h.element_id for h in held if h.element_id not in pool]
    if missing:
        raise SolverError(f"held players missing from the pool: {missing}")
    if len(bench_weights) < 4:
        raise SolverError("bench_weights needs four entries")

    gws = list(range(current_gw, current_gw + horizon))
    xp = _normalise_xp(xp_matrix, pool, gws, prices)
    fixtures = fixtures or {}

    common = dict(
        pool=pool,
        xp=xp,
        held=held,
        gws=gws,
        bank=int(bank),
        free_transfers=int(free_transfers),
        bench_weights=bench_weights,
        decay=float(decay),
        max_transfers_per_gw=max_transfers_per_gw,
    )

    def remaining() -> float:
        return time_limit - (time.perf_counter() - started)

    # The hold is solved first and separately. It is cheap -- no transfer
    # branching at all -- it is the yardstick every delta_xp is measured against,
    # and it guarantees there is something to return even if the open search
    # times out with nothing.
    prob, handles = _build(forbid_transfers=True, **common)
    baseline = _run(prob, handles, min(remaining(), max(2.0, time_limit * 0.25)), msg)
    if baseline is None:
        raise SolverError("no legal starting eleven exists for the squad given")

    truncated = baseline.truncated
    solutions: list[_Solution] = []
    # Keyed on the whole horizon, not just this week: the open search will often
    # return "no transfer now, two next week", which has the same cut key as the
    # baseline but is different advice and must not be swallowed as a duplicate.
    seen = {baseline.signature}

    # Open search, then re-solve with each answer's transfer set cut out. A MIP's
    # runner-up is otherwise the same plan with a different bench slot.
    cuts: list[tuple[int, ...]] = []
    for _ in range(max(0, n_plans - 1)):
        left = remaining()
        if left <= 1.0:
            truncated = True
            break
        prob, handles = _build(forbid_transfers=False, **common)
        _apply_cuts(prob, handles, cuts)
        found = _run(prob, handles, min(left, max(2.0, left)), msg)
        if found is None:
            break
        truncated = truncated or found.truncated
        cuts.append(found.cut_key)
        if found.signature not in seen:
            seen.add(found.signature)
            solutions.append(found)

    ranked = sorted(solutions, key=lambda s: -s.objective)
    to_plan = dict(
        baseline_honest=baseline.honest_xp, pool=pool, xp=xp, fixtures=fixtures, gws=gws
    )
    plans = [_to_plan(s, **to_plan) for s in ranked]
    hold = _to_plan(baseline, **to_plan)
    # The hold sits where its own objective puts it, so a user who should hold is
    # told so first, and one who should not still sees it offered.
    insert_at = sum(1 for s in ranked if s.objective > baseline.objective)
    plans.insert(insert_at, hold)

    return SolveResult(
        plans=plans,
        baseline_xp=round(baseline.honest_xp, 3),
        truncated=truncated,
        solve_ms=int((time.perf_counter() - started) * 1000),
    )


def _apply_cuts(
    prob: pulp.LpProblem, handles: dict[str, Any], cuts: Sequence[tuple[int, ...]]
) -> None:
    """Forbid each previously found set of incoming players at the current gameweek.

    A no-good cut: at least one of those signings must not be made.

    The cut is on the buys alone, deliberately. Cutting on the full move set
    (buys and sales together) is weak enough to be useless -- the solver satisfies
    it by swapping which of two equally worthless bench players it sells, and
    hands back the same signings under a different sale. That is a near-duplicate
    dressed as an alternative, which is exactly what asking a MIP for a second
    plan is supposed to avoid. Who you sell to fund a move is a detail; who you
    buy is the recommendation.

    The empty set -- holding -- cannot be excluded this way, so it becomes a
    requirement of at least one transfer instead.
    """
    g0 = handles["gws"][0]
    ids = set(handles["ids"])
    for n, bought in enumerate(cuts):
        moves = [handles["buy"][e][g0] for e in bought if e in ids]
        if moves:
            prob += pulp.lpSum(moves) <= len(moves) - 1, f"cut_{n}"
        else:
            prob += pulp.lpSum(handles["buy"][e][g0] for e in ids) >= 1, f"cut_{n}"
