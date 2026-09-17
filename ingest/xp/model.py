"""The baseline expected-points model.

Deliberately simple, deliberately explainable, deliberately not machine learning.
ARCHITECTURE.md is explicit that the solver gets built first against a dumb xP
model, so that a bad recommendation can be attributed to a bad decision rather
than a bad prediction. This is that dumb model, and it exists mostly to be
replaced — which is why `MODEL_VERSION` is one constant and every output row
carries it.

xP is decomposed rather than predicted directly, because the components are what
the UI shows as reasoning. "4.8 points, of which 1.7 is appearance, 1.9 attacking
returns and 0.8 a likely clean sheet" is a product; "4.8" is a number.

Expected minutes multiplies through everything else, which is why it gets the
most care here: a player who does not play scores nothing, and the single
largest error this model can make is projecting points for someone on the bench.

Gameweek, not fixture, is the unit of output — and a gameweek holds zero, one or
two fixtures. `expected_points` therefore takes a *list* of fixtures and sums
over it. A blank returns exactly zero and a double returns roughly twice a
single, by construction rather than by a special case.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .features import PlayerFeatures

# Bump this in one place. A new version writes new rows beside the old ones and
# never overwrites them (see the xpoints primary key), so past recommendations
# stay explainable after the model changes.
MODEL_VERSION = "baseline-0.1"

# --- Scoring rules ---------------------------------------------------------
# Verify these against the official rules each season; they move.

GOAL_POINTS = {1: 6, 2: 6, 3: 5, 4: 4}
ASSIST_POINTS = 3
CLEAN_SHEET_POINTS = {1: 4, 2: 4, 3: 1, 4: 0}

# Defensive contribution: 2 points at 10+ CBIT for defenders, 12+ for midfielders
# and forwards. Goalkeepers are not eligible, so they have no threshold.
DEFCON_POINTS = 2
DEFCON_THRESHOLD = {2: 10, 3: 12, 4: 12}

APPEARANCE_POINTS = 1  # 1 for playing at all, a second point at 60 minutes

# --- Minutes model ---------------------------------------------------------

# What a start is worth in minutes, and what a substitute appearance is worth.
# Both are averages over a season of real substitutions.
MINUTES_IF_START = 82.0
MINUTES_IF_SUB = 20.0

# A starter who is not withdrawn before the hour. Rotation-risk players are
# already penalised through p_start, so this stays high.
P_SIXTY_GIVEN_START = 0.88

# Availability by status flag when `chance_of_playing` is null. 'd' (doubtful)
# with no percentage is the press-conference-pending case and is genuinely a
# coin flip; the rest are unavailable until told otherwise.
STATUS_AVAILABILITY = {
    "a": 1.0,  # available
    "d": 0.5,  # doubtful
    "i": 0.0,  # injured
    "s": 0.0,  # suspended
    "u": 0.0,  # unavailable
    "n": 0.0,  # not in squad / on loan
}

# --- Fixture difficulty ----------------------------------------------------
# FDR runs 1 (easiest) to 5 (hardest), and 3 is neutral. These steps are small on
# purpose: FDR is a coarse, partly subjective number and leaning on it hard is a
# good way to make confident wrong predictions.

ATTACK_FDR_STEP = 0.09  # per point of FDR below neutral
DEFENCE_FDR_STEP = 0.12  # per point of FDR above neutral, on goals conceded
HOME_ATTACK_BONUS = 1.06
HOME_DEFENCE_BONUS = 0.92  # home sides concede less

# BPS per 90 mapped to expected bonus points per 90. Anchors interpolated
# linearly; below the first anchor a player effectively never places top three.
BPS_TO_BONUS: tuple[tuple[float, float], ...] = (
    (0.0, 0.00),
    (15.0, 0.02),
    (20.0, 0.10),
    (25.0, 0.25),
    (30.0, 0.55),
    (35.0, 0.95),
    (40.0, 1.40),
    (50.0, 2.10),
    (60.0, 2.60),
)

COMPONENT_KEYS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "defensive_contribution",
    "bonus",
)


@dataclass(frozen=True)
class FixtureContext:
    """One fixture from one player's point of view.

    `difficulty` is the FDR faced by *this player's* team, so the caller resolves
    home/away before constructing it and the model never has to know which side
    of the fixture row the player is on.
    """

    fixture_id: int | None
    gw: int
    is_home: bool
    difficulty: int | None = 3


@dataclass(frozen=True)
class GameweekXP:
    """The model's output for one player in one gameweek."""

    element_id: int
    gw: int
    xp: float
    xmins: float
    p_start: float
    p_play: float
    fixture_count: int
    components: dict[str, float] = field(default_factory=dict)


# --- Minutes ---------------------------------------------------------------


def availability(features: PlayerFeatures) -> float:
    """Probability the player is fit and selectable for a fixture.

    `chance_of_playing` wins when FPL supplies it, because it is a direct signal
    from the club rather than an inference from a status letter. A player flagged
    non-available with no percentage is treated as out, not as a coin flip — the
    asymmetry is deliberate, since projecting points for someone who does not
    play is a far more expensive error than missing a late return.
    """
    if features.chance_of_playing is not None:
        return max(0.0, min(1.0, features.chance_of_playing / 100.0))
    return STATUS_AVAILABILITY.get(features.status, 0.0)


def minutes_model(features: PlayerFeatures) -> tuple[float, float, float, float]:
    """Return (p_start, p_play, p_sixty, xmins) for a single fixture.

    Everything else in the model multiplies through these, so the shape matters
    more than the constants. Two distinct ways of appearing are modelled — a
    start, and a substitute cameo — because they are worth very different numbers
    of minutes and the difference is most of the gap between a bench filler and a
    rotation option.
    """
    avail = availability(features)

    matches = features.matches_in_window
    if matches == 0:
        # No recent evidence: a player with no matches in the window is either
        # new, injured or freshly promoted to the squad. Assume a fringe role
        # rather than zero, and let availability do the rest.
        start_rate, sub_rate = 0.2, 0.2
    else:
        start_rate = min(1.0, features.starts_in_window / matches)
        # Minutes not attributable to starts are cameos. Convert them to a rate
        # of appearing off the bench in the matches the player did not start.
        non_start_minutes = max(
            0.0, features.minutes_in_window - features.starts_in_window * MINUTES_IF_START
        )
        non_starts = matches - features.starts_in_window
        sub_rate = (
            min(1.0, (non_start_minutes / MINUTES_IF_SUB) / non_starts) if non_starts > 0 else 0.0
        )

    p_start = avail * start_rate
    p_sub = avail * (1.0 - start_rate) * sub_rate
    p_play = p_start + p_sub
    p_sixty = p_start * P_SIXTY_GIVEN_START

    xmins = p_start * MINUTES_IF_START + p_sub * MINUTES_IF_SUB
    return p_start, p_play, p_sixty, xmins


# --- Fixture difficulty ----------------------------------------------------


def attack_multiplier(ctx: FixtureContext) -> float:
    """Scale attacking returns by opponent difficulty and venue."""
    fdr = 3 if ctx.difficulty is None else max(1, min(5, ctx.difficulty))
    multiplier = 1.0 + (3 - fdr) * ATTACK_FDR_STEP
    multiplier *= HOME_ATTACK_BONUS if ctx.is_home else (2.0 - HOME_ATTACK_BONUS)
    return max(0.0, multiplier)


def defence_multiplier(ctx: FixtureContext) -> float:
    """Scale expected goals conceded by opponent difficulty and venue."""
    fdr = 3 if ctx.difficulty is None else max(1, min(5, ctx.difficulty))
    multiplier = 1.0 + (fdr - 3) * DEFENCE_FDR_STEP
    multiplier *= HOME_DEFENCE_BONUS if ctx.is_home else (2.0 - HOME_DEFENCE_BONUS)
    return max(0.0, multiplier)


# --- Small statistical helpers ---------------------------------------------


def poisson_tail(lam: float, k: int) -> float:
    """P(X >= k) for X ~ Poisson(lam). Used for the defensive-contribution gate.

    Poisson is the wrong distribution for counting tackles and interceptions —
    the real counts are less dispersed than this assumes — but it is the right
    shape: near zero for players well below the threshold, and rising smoothly
    rather than switching on at a hard cutoff.
    """
    if k <= 0:
        return 1.0
    if lam <= 0.0:
        return 0.0

    term = math.exp(-lam)
    cumulative = term
    for i in range(1, k):
        term *= lam / i
        cumulative += term
    return max(0.0, min(1.0, 1.0 - cumulative))


def bonus_per_90(bps90: float) -> float:
    """Interpolate expected bonus points per 90 from a BPS per-90 rate."""
    if bps90 <= BPS_TO_BONUS[0][0]:
        return BPS_TO_BONUS[0][1]
    for (x0, y0), (x1, y1) in zip(BPS_TO_BONUS, BPS_TO_BONUS[1:]):
        if bps90 <= x1:
            return y0 + (y1 - y0) * (bps90 - x0) / (x1 - x0)
    return BPS_TO_BONUS[-1][1]


# --- The model ------------------------------------------------------------


def _fixture_components(
    features: PlayerFeatures,
    ctx: FixtureContext,
    p_play: float,
    p_sixty: float,
    xmins: float,
) -> dict[str, float]:
    """Per-component expected points for one fixture."""
    position = features.element_type
    ninetieths = xmins / 90.0

    appearance = APPEARANCE_POINTS * p_play + APPEARANCE_POINTS * p_sixty

    attack = attack_multiplier(ctx)
    goals = features.xg90 * ninetieths * attack * GOAL_POINTS.get(position, 4)
    assists = features.xa90 * ninetieths * attack * ASSIST_POINTS

    # A clean sheet needs both the team to concede nothing and the player to be on
    # for 60 minutes. Treated as independent, which slightly overstates it for
    # players hooked while winning — a known and small bias.
    cs_points = CLEAN_SHEET_POINTS.get(position, 0)
    if cs_points:
        xgc_match = features.xgc90 * defence_multiplier(ctx)
        clean_sheet = math.exp(-xgc_match) * p_sixty * cs_points
    else:
        clean_sheet = 0.0

    threshold = DEFCON_THRESHOLD.get(position)
    if threshold is None:
        defcon = 0.0
    else:
        # Not scaled by fixture difficulty: a defender facing a strong side gets
        # more chances to clear and intercept, not fewer, so the FDR effect here
        # is ambiguous and left out rather than guessed at.
        defcon = poisson_tail(features.dc90 * ninetieths, threshold) * DEFCON_POINTS

    bonus = bonus_per_90(features.bps90) * ninetieths

    return {
        "appearance": appearance,
        "goals": goals,
        "assists": assists,
        "clean_sheet": clean_sheet,
        "defensive_contribution": defcon,
        "bonus": bonus,
    }


def expected_points(
    features: PlayerFeatures,
    fixtures: Sequence[FixtureContext],
    gw: int,
) -> GameweekXP:
    """Expected points for one player in one gameweek, summed over its fixtures.

    `fixtures` holds every fixture this player's team plays in `gw`: none for a
    blank, one normally, two for a double. The sum is the whole double/blank
    handling — there is no branch for it, because a branch is what rots.
    """
    components = {key: 0.0 for key in COMPONENT_KEYS}

    if not fixtures:
        # A blank is exactly zero, not a small number. The player cannot appear.
        return GameweekXP(
            element_id=features.element_id,
            gw=gw,
            xp=0.0,
            xmins=0.0,
            p_start=0.0,
            p_play=0.0,
            fixture_count=0,
            components=components,
        )

    p_start, p_play, p_sixty, xmins_per_fixture = minutes_model(features)

    for ctx in fixtures:
        for key, value in _fixture_components(
            features, ctx, p_play, p_sixty, xmins_per_fixture
        ).items():
            components[key] += value

    # Round once, at the edge, so that the stored components sum exactly to the
    # stored headline. The UI shows both and a penny of drift looks like a bug.
    components = {key: round(value, 3) for key, value in components.items()}
    xp = round(sum(components.values()), 3)

    return GameweekXP(
        element_id=features.element_id,
        gw=gw,
        xp=xp,
        xmins=round(xmins_per_fixture * len(fixtures), 2),
        p_start=round(p_start, 4),
        p_play=round(p_play, 4),
        fixture_count=len(fixtures),
        components=components,
    )
