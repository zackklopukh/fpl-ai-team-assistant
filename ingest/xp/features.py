"""Features for the baseline xPoints model, always as of an explicit gameweek.

The lookahead rule (CLAUDE.md, "Things not to do") is why this is a separate
layer rather than a few lines inside the model. The `players` table describes
*today*: price, form, status, season-to-date xG. Using those columns to predict
gameweek 12 means predicting it with gameweek 30's information, and every
evaluation number downstream becomes fiction.

So every rate here is built from `player_gw_stats` rows filtered to
`gw <= as_of_gw`, and `as_of_gw` is a required argument rather than an implicit
"now". Predicting a future gameweek is this same call with `as_of_gw` set to the
last finished gameweek — which is correct, because season-to-date genuinely is
all that is known at that point. A backtest is the same call with an earlier
`as_of_gw`, and it cannot accidentally read today's numbers because no code path
in here can reach them.

Availability (`status`, `chance_of_playing`) is the one input the schema keeps no
historical snapshot of. It is therefore passed in explicitly: the nightly job
hands over today's values, and a backtest must hand over neutral ones rather than
reaching into `players` behind this module's back.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# How many gameweeks of history the minutes model looks at. Minutes are the most
# recency-sensitive thing about a player — a new signing who has started the last
# three is nailed on, whatever the season total says.
MINUTES_WINDOW = 5

# Rate stats (xG, xA, xGC, CBIT, BPS) are shrunk toward a positional prior with
# this much pseudo-history, so a striker who has played 40 minutes does not get a
# 2.0 xG per 90 projection off one shot.
PRIOR_MINUTES = 270.0


@dataclass(frozen=True)
class PositionPrior:
    """Rough league-average per-90 rates, used as the shrinkage target."""

    xg90: float
    xa90: float
    xgc90: float
    dc90: float
    bps90: float


# Eyeballed from typical Premier League per-90 rates. These are deliberately
# crude: they only matter for players with little history, and their job is to
# stop small samples exploding, not to be accurate in their own right.
POSITION_PRIORS: dict[int, PositionPrior] = {
    1: PositionPrior(xg90=0.00, xa90=0.02, xgc90=1.35, dc90=0.0, bps90=17.0),
    2: PositionPrior(xg90=0.05, xa90=0.07, xgc90=1.35, dc90=6.0, bps90=16.0),
    3: PositionPrior(xg90=0.14, xa90=0.13, xgc90=1.40, dc90=5.0, bps90=15.0),
    4: PositionPrior(xg90=0.35, xa90=0.13, xgc90=1.45, dc90=2.0, bps90=15.0),
}


@dataclass(frozen=True)
class PlayerFeatures:
    """Everything the model is allowed to know about one player at one moment."""

    element_id: int
    element_type: int  # 1 GKP, 2 DEF, 3 MID, 4 FWD
    team_fpl_id: int
    as_of_gw: int

    # Minutes model inputs, over the recent window.
    matches_in_window: int
    starts_in_window: int
    minutes_in_window: int

    # Per-90 rates, shrunk toward the positional prior.
    xg90: float
    xa90: float
    xgc90: float
    dc90: float
    bps90: float

    minutes_total: int  # season to date, as of as_of_gw — for shrinkage weight

    # Availability. Not derivable from player_gw_stats; see the module docstring.
    status: str = "a"
    chance_of_playing: int | None = None

    @property
    def prior(self) -> PositionPrior:
        return POSITION_PRIORS.get(self.element_type, POSITION_PRIORS[3])


def _f(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    """Read a numeric column that may be null or a Decimal."""
    value = row.get(key)
    if value is None:
        return default
    return float(value)


def _shrunk_rate(total: float, minutes: float, prior_rate: float) -> float:
    """Blend an observed per-90 rate toward the positional prior.

    With zero minutes this is exactly the prior; with a full season of minutes the
    prior is a rounding error. Written as a weighted total rather than a weighted
    average of rates so that the zero-minutes case needs no special branch.
    """
    prior_total = prior_rate * (PRIOR_MINUTES / 90.0)
    return (total + prior_total) / ((minutes + PRIOR_MINUTES) / 90.0)


def group_stat_rows(
    rows: Iterable[Mapping[str, object]],
) -> dict[int, list[Mapping[str, object]]]:
    """Bucket `player_gw_stats` rows by element id.

    One row per player per fixture: a double gameweek contributes two and a blank
    contributes none. Because a blank leaves no trace here, the count of rows is
    not a count of matches the player could have played in — that has to come
    from `fixtures`, which is why `matches_in_window` is passed into
    `build_features` rather than inferred.
    """
    grouped: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["element_id"])].append(row)
    return dict(grouped)


def build_features(
    *,
    element_id: int,
    element_type: int,
    team_fpl_id: int,
    stat_rows: Sequence[Mapping[str, object]],
    as_of_gw: int,
    matches_in_window: int,
    status: str = "a",
    chance_of_playing: int | None = None,
) -> PlayerFeatures:
    """Summarise one player's history up to and including `as_of_gw`.

    `stat_rows` are `player_gw_stats` rows for this player — any gameweek; rows
    after `as_of_gw` are dropped here rather than trusted to the caller, because
    that filter is the entire leakage guarantee and it should live in one place.

    `matches_in_window` is how many fixtures the player's team actually played in
    the last `MINUTES_WINDOW` gameweeks, counted from `fixtures`. It cannot be
    inferred from `stat_rows`: a blank leaves no row, and so does a player who was
    not in the matchday squad, and those two mean opposite things about whether a
    start was available to miss.
    """
    played_rows = [r for r in stat_rows if r.get("gw") is not None and int(r["gw"]) <= as_of_gw]

    window_lo = as_of_gw - MINUTES_WINDOW
    window_rows = [r for r in played_rows if int(r["gw"]) > window_lo]

    minutes_total = int(sum(_f(r, "minutes") for r in played_rows))

    prior = POSITION_PRIORS.get(element_type, POSITION_PRIORS[3])

    return PlayerFeatures(
        element_id=element_id,
        element_type=element_type,
        team_fpl_id=team_fpl_id,
        as_of_gw=as_of_gw,
        # Never fewer matches than the player has rows for, or a stale fixture
        # table would produce a start rate above 1.
        matches_in_window=max(matches_in_window, len(window_rows)),
        starts_in_window=int(sum(_f(r, "starts") for r in window_rows)),
        minutes_in_window=int(sum(_f(r, "minutes") for r in window_rows)),
        xg90=_shrunk_rate(
            sum(_f(r, "expected_goals") for r in played_rows), minutes_total, prior.xg90
        ),
        xa90=_shrunk_rate(
            sum(_f(r, "expected_assists") for r in played_rows), minutes_total, prior.xa90
        ),
        xgc90=_shrunk_rate(
            sum(_f(r, "expected_goals_conceded") for r in played_rows),
            minutes_total,
            prior.xgc90,
        ),
        dc90=_shrunk_rate(
            sum(_f(r, "defensive_contribution") for r in played_rows),
            minutes_total,
            prior.dc90,
        ),
        bps90=_shrunk_rate(sum(_f(r, "bps") for r in played_rows), minutes_total, prior.bps90),
        minutes_total=minutes_total,
        status=status,
        chance_of_playing=chance_of_playing,
    )
