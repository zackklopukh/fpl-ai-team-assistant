"""The fitted expected-points model (`fitted-0.1`).

Same decomposition as `baseline-0.1` — appearance, goals, assists, clean sheet
and so on, summed per fixture and then over a gameweek's fixtures — because the
components are what the UI shows as reasoning. The difference is that every
number inside a component is fitted from data rather than set by hand, and every
FPL scoring rule is included rather than the six largest.

How it fits, in one paragraph: `fitted_panel` turns an `AsOf` into a
player-gameweek panel where every row carries only what was known before its
own deadline. Each component is then a small regression of what happened on
what was known — so the parameters are exactly the ones that would have
predicted the past best, and the target is predicted with the same features
built the same way. There is no separate training pipeline to drift out of
sync with production: `fit(asof)` and `predict(asof)` are the whole interface,
and production builds an `AsOf` for today (see `fitted_live`).

The components, per fixture, for a player with availability `a`:

* **Minutes.** `p_start` is a logistic regression on recency-weighted start
  rates (a fast and a slow decay, last season down-weighted), the last
  gameweek's minutes, how long the player has been absent or benched, position
  and point-in-time price. `p_sub` (appearing off the bench, given no start) is
  a second logistic regression. Minutes given a start and the chance of
  lasting 60 are the player's own history shrunk toward his position's pooled
  figures, with shrinkage strength chosen by likelihood.
* **Goals / assists.** Per-90 xG and xA, opponent- and venue-adjusted, recency
  weighted and empirical-Bayes shrunk toward a prior that is a Poisson
  regression on position and log price. The shrinkage strength is chosen by
  predictive likelihood, and a per-position conversion factor turns predicted
  xG into goals and xA into FPL assists (FPL awards ~40% more assists than xA).
* **Team strength.** A multiplicative Poisson model of team xG with fitted home
  advantage (`fitted_panel.team_ratings`). A fixture's expected goals against
  drives clean sheets (Poisson zero with a fitted dispersion factor) and
  goals-conceded penalties; its opponent-defence factor scales attacking output.
* **Saves, cards, own goals, penalties.** Shrunk per-player rates where there is
  signal (saves, yellow cards), pooled per-position rates where there is not
  (reds, own goals, penalty saves), penalty misses in proportion to xG.
* **Bonus.** A Poisson regression of bonus on the fixture's expected returns,
  clean-sheet chance, the player's BPS rate and minutes — fitted, not anchored.
* **Defensive contribution** (2025-26 on): logistic regression of reaching the
  threshold on the player's expected count in the match, from 2025-26 rows only.
  Real counts are under-dispersed, so a Poisson tail understates high-rate
  players; the logistic learns the actual steepness.

Blanks produce no fixture rows and so are exactly zero; a double is the sum of
two fixtures, each with its own opponent and venue.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .fitted_glm import Glm, fit_glm, poisson_floor_mean
from .fitted_panel import DecaySpec, Panel, build_panel
from .model import GameweekXP

MODEL_VERSION = "fitted-0.1"

# --- Scoring rules -----------------------------------------------------------
# Verified by rebuilding `total_points` from the component columns for every row
# of 2023-24 through 2026-27 GW4: exact for all 89,304 rows once defensive
# contribution is added from 2025-26 (DEF at 10+, MID/FWD at 12+, never GKP).
# No other rule changed across these seasons, and no goalkeeper scored.

GOAL_POINTS = {1: 6, 2: 6, 3: 5, 4: 4}
ASSIST_POINTS = 3
CLEAN_SHEET_POINTS = {1: 4, 2: 4, 3: 1, 4: 0}  # needs 60+ minutes
GOALS_CONCEDED_PER_POINT = 2  # GKP and DEF lose 1 per 2 conceded while on the pitch
GOALS_CONCEDED_POSITIONS = (1, 2)
SAVES_PER_POINT = 3
PENALTY_SAVE_POINTS = 5
PENALTY_MISS_POINTS = -2
YELLOW_POINTS = -1
RED_POINTS = -3
OWN_GOAL_POINTS = -2
DEFCON_POINTS = 2
DEFCON_THRESHOLD = {2: 10, 3: 12, 4: 12}
DEFCON_FIRST_SEASON = "2025-26"

POSITIONS = (1, 2, 3, 4)

COMPONENT_KEYS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "goals_conceded",
    "saves",
    "bonus",
    "defensive_contribution",
    "discipline",
)

# Availability by status flag when `chance_of_playing` is null, as in baseline-0.1.
STATUS_AVAILABILITY = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}

MIN_DEFCON_ROWS = 100


@dataclass(frozen=True)
class FittedConfig:
    """The choices that are tuned rather than fitted, all selected on 2024-25.

    Decay rates set how much memory each kind of evidence has; everything else
    (coefficients, shrinkage strengths, conversions) is fitted inside `fit`.
    The 2024-25 walk-forward was flat across the grid tried (Spearman within
    +-0.001, squad points within noise); these values had the lowest MAE. Long
    memory wins for attacking rates and team strength (half-lives of ~35 player-
    gameweeks), short memory for starts, and last season's starts count for a
    quarter — a summer changes roles far more than it changes finishing.
    """

    minutes_fast: DecaySpec = DecaySpec(per_gw=0.5, per_season=0.5)
    minutes_slow: DecaySpec = DecaySpec(per_gw=0.85, per_season=0.25)
    rates: DecaySpec = DecaySpec(per_gw=0.98, per_season=0.6)
    team: DecaySpec = DecaySpec(per_gw=0.98, per_season=0.5)
    team_prior_matches: float = 5.0
    # Rows from the first gameweeks of the earliest loaded season are left out of
    # training: those players look history-less only because the data starts there.
    burn_in_gws: int = 5
    shrinkage_grid: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
    ridge: float = 1.0


DEFAULT_CONFIG = FittedConfig()


# --- Features ----------------------------------------------------------------

MINUTES_FEATURES = (
    "start_rate_fast",
    "start_rate_slow",
    "sub_rate",
    "log_evidence",
    "no_history",
    "last_start",
    "last_min90",
    "log_absent_streak",
    "log_benched_streak",
    "new_season",
    "new_season_x_start_rate",
    "log_season_rows",
    "log_price",
    "price_missing",
    "is_def",
    "is_mid",
    "is_fwd",
)


def _ratio(num: np.ndarray, den: np.ndarray, default: float = 0.0) -> np.ndarray:
    num = np.asarray(num, dtype=float)
    den = np.asarray(den, dtype=float)
    out = np.full_like(num, default)
    ok = den > 1e-9
    out[ok] = num[ok] / den[ok]
    return out


def _log_price(pgw: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    price = pgw["price"].to_numpy(dtype=float)
    missing = np.isnan(price)
    # A player with no price anywhere (a new signing before his first gameweek)
    # gets a cheap squad player's price and a flag the regressions can use.
    return np.log(np.where(missing, 45.0, price) / 50.0), missing.astype(float)


def minutes_features(pgw: pd.DataFrame) -> np.ndarray:
    pos = pgw["element_type"].to_numpy()
    sr_fast = _ratio(pgw["f_starts"], pgw["f_n_fx"])
    sr_slow = _ratio(pgw["s_starts"], pgw["s_n_fx"])
    new_season = (pgw["season_rows"].to_numpy() == 0).astype(float)
    log_price, price_missing = _log_price(pgw)
    return np.column_stack(
        [
            sr_fast,
            sr_slow,
            _ratio(pgw["s_sub_apps"], pgw["s_non_starts"]),
            np.log1p(pgw["s_n_fx"].to_numpy(dtype=float)),
            (~pgw["has_prev"].to_numpy(dtype=bool)).astype(float),
            pgw["last_start"].to_numpy(dtype=float),
            pgw["last_min90"].to_numpy(dtype=float),
            np.log1p(pgw["absent_streak"].to_numpy(dtype=float)),
            np.log1p(pgw["benched_streak"].to_numpy(dtype=float)),
            new_season,
            new_season * sr_slow,
            np.log1p(pgw["season_rows"].to_numpy(dtype=float)),
            log_price,
            price_missing,
            (pos == 2).astype(float),
            (pos == 3).astype(float),
            (pos == 4).astype(float),
        ]
    )


# Per-90 rates that are shrunk toward a price-and-position prior. Each is
# (numerator sum, exposure sum, positions it applies to).
RATE_STATS: dict[str, tuple[str, str, tuple[int, ...]]] = {
    "xg": ("r_xg_adj", "r_min90", POSITIONS),
    "xa": ("r_xa_adj", "r_min90", POSITIONS),
    "bps": ("r_bps", "r_min90", POSITIONS),
    "yc": ("r_yellow_cards", "r_min90", POSITIONS),
    "saves": ("r_saves_adj", "r_gk_min90", (1,)),
    "dc": ("r_dc", "r_dc_min90", (2, 3, 4)),
}

# The same stats' per-gameweek outcomes, for fitting the priors.
_RATE_OUTCOMES = {
    "xg": ("xg_adj", "min90"),
    "xa": ("xa_adj", "min90"),
    "bps": ("bps", "min90"),
    "yc": ("yellow_cards", "min90"),
    "saves": ("saves_adj", "gk_min90"),
    "dc": ("dc", "dc_min90"),
}


# --- Parameters --------------------------------------------------------------


@dataclass
class FittedParams:
    """Every fitted quantity. `describe()` prints them."""

    as_of: tuple[str, int]
    start: Glm
    sub: Glm
    # Pooled minutes by position.
    q60_start: dict[int, float]
    minutes_start: dict[int, float]
    minutes_start_60: dict[int, float]
    minutes_sub: dict[int, float]
    q60_sub: dict[int, float]
    k_q60: float
    k_minutes: float
    # Per-90 priors: stat -> position -> Glm on log price.
    rate_priors: dict[str, dict[int, Glm]]
    rate_k: dict[str, float]
    goal_conversion: dict[int, float]
    assist_conversion: dict[int, float]
    cs_dispersion: float
    red90: dict[int, float]
    own_goal90: dict[int, float]
    penalty_save90: float
    penalty_miss_per_xg: float
    bonus: Glm | None = None  # fitted last: its features use the conversions above
    defcon: dict[int, Glm] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """All parameters as plain numbers, for printing or logging."""

        def r(v: float) -> float:
            return round(float(v), 4)

        return {
            "model_version": MODEL_VERSION,
            "as_of": {"season": self.as_of[0], "gw": self.as_of[1]},
            "p_start": {k: r(v) for k, v in self.start.raw_coefficients().items()},
            "p_sub_given_no_start": {k: r(v) for k, v in self.sub.raw_coefficients().items()},
            "minutes": {
                "p60_given_start": {p: r(v) for p, v in self.q60_start.items()},
                "minutes_given_start": {p: r(v) for p, v in self.minutes_start.items()},
                "minutes_given_start_60plus": {p: r(v) for p, v in self.minutes_start_60.items()},
                "minutes_given_sub": {p: r(v) for p, v in self.minutes_sub.items()},
                "p60_given_sub": {p: r(v) for p, v in self.q60_sub.items()},
                "shrinkage_starts_p60": self.k_q60,
                "shrinkage_starts_minutes": self.k_minutes,
            },
            "rate_priors_per90": {
                stat: {
                    p: {k: r(v) for k, v in glm.raw_coefficients().items()}
                    for p, glm in by_pos.items()
                }
                for stat, by_pos in self.rate_priors.items()
            },
            "rate_shrinkage_90s": self.rate_k,
            "goal_conversion": {p: r(v) for p, v in self.goal_conversion.items()},
            "assist_conversion": {p: r(v) for p, v in self.assist_conversion.items()},
            "clean_sheet_dispersion": r(self.cs_dispersion),
            "red_cards_per90": {p: r(v) for p, v in self.red90.items()},
            "own_goals_per90": {p: r(v) for p, v in self.own_goal90.items()},
            "penalty_saves_per90": r(self.penalty_save90),
            "penalty_misses_per_xg": r(self.penalty_miss_per_xg),
            "bonus": {k: r(v) for k, v in self.bonus.raw_coefficients().items()} if self.bonus else {},
            "defensive_contribution": {
                p: {k: r(v) for k, v in glm.raw_coefficients().items()}
                for p, glm in self.defcon.items()
            },
            "diagnostics": self.diagnostics,
        }


# --- Player state: everything per player-gameweek that the fixtures multiply ---


def player_state(pgw: pd.DataFrame, params: FittedParams) -> pd.DataFrame:
    """Per player-gameweek: minutes probabilities and shrunk per-90 rates."""
    pos = pgw["element_type"].to_numpy()
    x = minutes_features(pgw)
    out = pd.DataFrame(index=pgw.index)
    out["element_type"] = pos
    out["p_start"] = params.start.predict(x)
    out["p_sub"] = params.sub.predict(x)

    def pooled(d: Mapping[int, float]) -> np.ndarray:
        default = float(np.mean(list(d.values())))
        return np.array([d.get(int(p), default) for p in pos], dtype=float)

    q60_pos = pooled(params.q60_start)
    m_pos = pooled(params.minutes_start)
    starts = pgw["s_starts"].to_numpy(dtype=float)
    out["q60"] = (pgw["s_start_60"].to_numpy(dtype=float) + params.k_q60 * q60_pos) / (
        starts + params.k_q60
    )
    out["m_start"] = (pgw["s_start_minutes"].to_numpy(dtype=float) + params.k_minutes * m_pos) / (
        starts + params.k_minutes
    )
    out["m_start"] = out["m_start"].clip(upper=95.0)
    out["m_start_60"] = pooled(params.minutes_start_60)
    out["m_sub"] = pooled(params.minutes_sub)
    out["q60_sub"] = pooled(params.q60_sub)

    log_price, _ = _log_price(pgw)
    for stat, (num, den, positions) in RATE_STATS.items():
        rate = np.zeros(len(pgw))
        priors = params.rate_priors.get(stat, {})
        k = params.rate_k.get(stat, 8.0)
        for p in positions:
            m = pos == p
            if not m.any() or p not in priors:
                continue
            prior = priors[p].predict(log_price[m][:, None])
            n = pgw[num].to_numpy(dtype=float)[m]
            e = pgw[den].to_numpy(dtype=float)[m]
            rate[m] = (n + k * prior) / (e + k)
        out[f"{stat}90"] = rate
    return out


# --- Fitting ------------------------------------------------------------------


def _deviance(y: np.ndarray, mu: np.ndarray) -> float:
    mu = np.maximum(mu, 1e-9)
    term = np.where(y > 0, y * np.log(np.maximum(y, 1e-12) / mu), 0.0)
    return float(2 * (term - (y - mu)).sum())


def _pooled_by_position(frame: pd.DataFrame, num: str, den: str, positions=POSITIONS) -> dict[int, float]:
    out = {}
    for p in positions:
        g = frame[frame["element_type"] == p]
        d = float(g[den].sum())
        out[p] = float(g[num].sum()) / d if d > 0 else 0.0
    return out


def fit(asof, config: FittedConfig = DEFAULT_CONFIG) -> FittedParams:
    """Fit every component on the history in `asof` (strictly before its target)."""
    panel = _panel(asof, [asof.gw], config)
    pgw = panel.pgw
    hist = pgw[~pgw["is_target"]]
    train_pgw = hist[_trainable(hist, panel.first_season, config)]

    rows = panel.rows.copy()
    rows = rows[_trainable(rows, panel.first_season, config)]
    feats = train_pgw.set_index("pgw_id")

    # -- Minutes --------------------------------------------------------------
    # Fit at fixture level (a double is two chances to start) with the features
    # of the player-gameweek the fixture belongs to.
    x_pgw = minutes_features(feats)
    x_index = {pid: i for i, pid in enumerate(feats.index)}
    rows = rows[rows["pgw_id"].isin(x_index)]
    ri = rows["pgw_id"].map(x_index).to_numpy()
    x_rows = x_pgw[ri]
    started = (rows["starts"] > 0).to_numpy()
    start = fit_glm(x_rows, started.astype(float), MINUTES_FEATURES, "logit", ridge=config.ridge)
    played = (rows["minutes"] > 0).to_numpy()
    sub = fit_glm(
        x_rows[~started], played[~started].astype(float), MINUTES_FEATURES, "logit", ridge=config.ridge
    )

    st = rows[started]
    q60_start = {p: float((g["minutes"] >= 60).mean()) for p, g in st.groupby("element_type")}
    minutes_start = {p: float(g["minutes"].mean()) for p, g in st.groupby("element_type")}
    minutes_start_60 = {
        p: float(g.loc[g["minutes"] >= 60, "minutes"].mean()) for p, g in st.groupby("element_type")
    }
    subs = rows[~started & played]
    minutes_sub = {p: float(g["minutes"].mean()) for p, g in subs.groupby("element_type")}
    q60_sub = {p: float((g["minutes"] >= 60).mean()) for p, g in subs.groupby("element_type")}

    # Shrinkage for the player's own minutes-given-start: pick by likelihood.
    st_feats = feats.loc[st["pgw_id"]]
    s_starts = st_feats["s_starts"].to_numpy(dtype=float)
    pos_st = st["element_type"].to_numpy()
    q_pos = np.array([q60_start[p] for p in pos_st])
    m_pos = np.array([minutes_start[p] for p in pos_st])
    y60 = (st["minutes"] >= 60).to_numpy(dtype=float)
    best = (math.inf, 8.0)
    for k in config.shrinkage_grid:
        q = (st_feats["s_start_60"].to_numpy(dtype=float) + k * q_pos) / (s_starts + k)
        q = np.clip(q, 1e-4, 1 - 1e-4)
        ll = -float((y60 * np.log(q) + (1 - y60) * np.log(1 - q)).sum())
        best = min(best, (ll, k))
    k_q60 = best[1]
    best = (math.inf, 8.0)
    ym = st["minutes"].to_numpy(dtype=float)
    for k in config.shrinkage_grid:
        m = (st_feats["s_start_minutes"].to_numpy(dtype=float) + k * m_pos) / (s_starts + k)
        best = min(best, (float(((ym - m) ** 2).sum()), k))
    k_minutes = best[1]

    # -- Per-90 rates: priors on price, shrinkage by predictive likelihood ------
    log_price, _ = _log_price(train_pgw)
    rate_priors: dict[str, dict[int, Glm]] = {}
    rate_k: dict[str, float] = {}
    pos_pgw = train_pgw["element_type"].to_numpy()
    for stat, (num, den, positions) in RATE_STATS.items():
        y_col, e_col = _RATE_OUTCOMES[stat]
        exposure = train_pgw[e_col].to_numpy(dtype=float)
        outcome = train_pgw[y_col].to_numpy(dtype=float)
        priors = {}
        for p in positions:
            m = (pos_pgw == p) & (exposure > 0)
            if m.sum() < 30:
                continue
            priors[p] = fit_glm(
                log_price[m][:, None],
                outcome[m] / exposure[m],
                ("log_price",),
                "log",
                weight=exposure[m],
                ridge=config.ridge,
            )
        rate_priors[stat] = priors
        if not priors:
            continue
        # Choose the shrinkage strength that best predicts each gameweek's outcome
        # from the history before it.
        m = np.isin(pos_pgw, list(priors)) & (exposure > 0)
        prior = np.zeros(m.sum())
        pm = pos_pgw[m]
        for p, glm in priors.items():
            prior[pm == p] = glm.predict(log_price[m][pm == p][:, None])
        n = train_pgw[num].to_numpy(dtype=float)[m]
        e = train_pgw[den].to_numpy(dtype=float)[m]
        best = (math.inf, 8.0)
        for k in config.shrinkage_grid:
            rate = (n + k * prior) / (e + k)
            best = min(best, (_deviance(outcome[m], rate * exposure[m]), k))
        rate_k[stat] = best[1]

    params = FittedParams(
        as_of=(asof.season, asof.gw),
        start=start,
        sub=sub,
        q60_start=q60_start,
        minutes_start=minutes_start,
        minutes_start_60=minutes_start_60,
        minutes_sub=minutes_sub,
        q60_sub=q60_sub,
        k_q60=k_q60,
        k_minutes=k_minutes,
        rate_priors=rate_priors,
        rate_k=rate_k,
        goal_conversion={},
        assist_conversion={},
        cs_dispersion=1.0,
        red90={},
        own_goal90={},
        penalty_save90=0.0,
        penalty_miss_per_xg=0.0,
    )

    # -- Conversions: predicted xG/xA at the minutes actually played -> returns --
    state = player_state(feats, params)
    srow = state.loc[rows["pgw_id"]].reset_index(drop=True)
    r = rows.reset_index(drop=True)
    min90 = r["min90"].to_numpy(dtype=float)
    pred_xg = srow["xg90"].to_numpy() * min90 * r["att_mult"].to_numpy()
    pred_xa = srow["xa90"].to_numpy() * min90 * r["att_mult"].to_numpy()
    tmp = pd.DataFrame(
        {
            "element_type": r["element_type"],
            "goals": r["goals_scored"],
            "assists": r["assists"],
            "pred_xg": pred_xg,
            "pred_xa": pred_xa,
            "min90": min90,
            "red": r["red_cards"],
            "og": r["own_goals"],
        }
    )
    params.goal_conversion = _pooled_by_position(tmp, "goals", "pred_xg")
    params.assist_conversion = _pooled_by_position(tmp, "assists", "pred_xa")
    params.red90 = _pooled_by_position(tmp, "red", "min90")
    params.own_goal90 = _pooled_by_position(tmp, "og", "min90")
    gk = r["element_type"] == 1
    params.penalty_save90 = float(r.loc[gk, "penalties_saved"].sum() / max(r.loc[gk, "min90"].sum(), 1e-9))
    params.penalty_miss_per_xg = float(
        r["penalties_missed"].sum() / max(r["expected_goals"].fillna(0).sum(), 1e-9)
    )

    # -- Clean sheets: dispersion of goals conceded around the team model --------
    team = r.drop_duplicates(["season", "fixture_id", "team_code"])
    team = team[team["team_goals_against"].notna()]
    lam = (team["lam_against"] * team["goals_per_xg"]).to_numpy(dtype=float)
    cs = (team["team_goals_against"].to_numpy(dtype=float) == 0).astype(float)
    best = (math.inf, 1.0)
    for kappa in np.arange(0.6, 1.41, 0.02):
        p = np.clip(np.exp(-kappa * lam), 1e-6, 1 - 1e-6)
        best = min(best, (-float((cs * np.log(p) + (1 - cs) * np.log(1 - p)).sum()), float(kappa)))
    params.cs_dispersion = best[1]

    # -- Bonus: fitted on the rows where the player appeared --------------------
    played_rows = r["minutes"].to_numpy() > 0
    bx = _bonus_features(
        r["element_type"].to_numpy()[played_rows],
        min90[played_rows],
        pred_xg[played_rows],
        pred_xa[played_rows],
        (r["lam_against"] * r["goals_per_xg"]).to_numpy(dtype=float)[played_rows],
        srow["bps90"].to_numpy()[played_rows],
        params,
    )
    params.bonus = fit_glm(
        bx, r["bonus"].to_numpy(dtype=float)[played_rows], BONUS_FEATURES, "log", ridge=config.ridge
    )

    # -- Defensive contribution: 2025-26 rows only ------------------------------
    dc_rows = r["defensive_contribution"].notna().to_numpy() & played_rows
    for group, positions in (("def", (2,)), ("mid_fwd", (3, 4))):
        m = dc_rows & np.isin(r["element_type"].to_numpy(), positions)
        if m.sum() < MIN_DEFCON_ROWS or "dc" not in params.rate_priors:
            continue
        thr = np.array([DEFCON_THRESHOLD[int(p)] for p in r["element_type"].to_numpy()[m]])
        hit = (r["defensive_contribution"].astype("float64").to_numpy()[m] >= thr).astype(float)
        expected = srow["dc90"].to_numpy()[m] * min90[m]
        glm = fit_glm(
            np.log(expected + 0.1)[:, None], hit, ("log_expected_count",), "logit", ridge=config.ridge
        )
        for p in positions:
            params.defcon[p] = glm

    params.diagnostics = {
        "training_fixture_rows": int(len(r)),
        "training_player_gameweeks": int(len(train_pgw)),
        "start_rate": round(float(started.mean()), 4),
        "mean_goals_per_xg_league": round(
            float(panel.ratings.league["goals_per_xg"].iloc[-1]), 4
        ),
        "home_advantage": round(float(panel.ratings.league["home_adv"].iloc[-1]), 4),
        "league_xg_per_team_match": round(float(panel.ratings.league["mu"].iloc[-1]), 4),
    }
    return params


BONUS_FEATURES = (
    "log1p_expected_return_points",
    "clean_sheet_points",
    "log_bps90",
    "log_min90",
    "played_60",
    "is_def",
    "is_mid",
    "is_fwd",
)


def _bonus_features(pos, min90, e_xg, e_xa, lam_goals_against, bps90, params: FittedParams) -> np.ndarray:
    """Bonus is a function of the returns a player is expected to produce."""
    pos = np.asarray(pos)
    gp = np.array([GOAL_POINTS.get(int(p), 4) for p in pos], dtype=float)
    ret = gp * np.array([params.goal_conversion.get(int(p), 1.0) for p in pos]) * e_xg + ASSIST_POINTS * np.array(
        [params.assist_conversion.get(int(p), 1.0) for p in pos]
    ) * e_xa
    csp = np.array([CLEAN_SHEET_POINTS.get(int(p), 0) for p in pos], dtype=float)
    p60 = (min90 >= 60 / 90 - 1e-9).astype(float)
    cs = csp * p60 * np.exp(-params.cs_dispersion * lam_goals_against * np.minimum(min90, 1.0))
    return np.column_stack(
        [
            np.log1p(ret),
            cs,
            np.log(np.maximum(bps90, 1.0)),
            np.log(np.maximum(min90, 1 / 90)),
            p60,
            (pos == 2).astype(float),
            (pos == 3).astype(float),
            (pos == 4).astype(float),
        ]
    )


def _trainable(frame: pd.DataFrame, first_season: str, config: FittedConfig) -> np.ndarray:
    return ~((frame["season"] == first_season) & (frame["gw"] <= config.burn_in_gws)).to_numpy()


def _panel(asof, gws: Sequence[int], config: FittedConfig) -> Panel:
    return build_panel(
        asof,
        list(gws),
        minutes_fast=config.minutes_fast,
        minutes_slow=config.minutes_slow,
        rates=config.rates,
        team_decay=config.team,
        team_prior_matches=config.team_prior_matches,
    )


# --- Prediction ----------------------------------------------------------------


def availability_from_status(status: str | None, chance_of_playing: int | None) -> float:
    """Probability a player is fit and selectable, from FPL's live flags.

    `chance_of_playing` wins when present; a flagged player with no percentage is
    treated as out (same asymmetry as baseline-0.1: projecting points for someone
    who does not play is the costlier error).
    """
    if chance_of_playing is not None and not (isinstance(chance_of_playing, float) and math.isnan(chance_of_playing)):
        return max(0.0, min(1.0, float(chance_of_playing) / 100.0))
    return STATUS_AVAILABILITY.get(status or "a", 0.0)


def fixture_components(fx: pd.DataFrame, params: FittedParams, defcon_live: bool) -> pd.DataFrame:
    """Expected points per component for each (player, fixture) row.

    `fx` holds one row per fixture with the player's state columns (from
    `player_state`), `availability`, and the fixture's team expectations.
    """
    pos = fx["element_type"].to_numpy()
    a = fx["availability"].to_numpy(dtype=float)
    ps = a * fx["p_start"].to_numpy()
    psub = a * (1.0 - fx["p_start"].to_numpy()) * fx["p_sub"].to_numpy()
    q60 = fx["q60"].to_numpy()
    m_s = fx["m_start"].to_numpy()
    m_s60 = fx["m_start_60"].to_numpy()
    m_sub = fx["m_sub"].to_numpy()
    p60 = ps * q60 + psub * fx["q60_sub"].to_numpy()
    xmins = ps * m_s + psub * m_sub
    att = fx["att_mult"].to_numpy(dtype=float)
    lam_ga = (fx["lam_against"] * fx["goals_per_xg"]).to_numpy(dtype=float)

    gp = np.array([GOAL_POINTS.get(int(p), 4) for p in pos], dtype=float)
    gconv = np.array([params.goal_conversion.get(int(p), 1.0) for p in pos])
    aconv = np.array([params.assist_conversion.get(int(p), 1.0) for p in pos])
    e_xg = fx["xg90"].to_numpy() * xmins / 90.0 * att
    e_xa = fx["xa90"].to_numpy() * xmins / 90.0 * att

    out = pd.DataFrame(index=fx.index)
    out["appearance"] = (ps + psub) + p60
    out["goals"] = gp * gconv * e_xg
    out["assists"] = ASSIST_POINTS * aconv * e_xa

    # Clean sheet: on for 60+ and nothing conceded while on. A starter who lasts
    # the hour is on for m_s60 minutes on average.
    csp = np.array([CLEAN_SHEET_POINTS.get(int(p), 0) for p in pos], dtype=float)
    kappa = params.cs_dispersion
    p_cs_60 = np.exp(-kappa * lam_ga * m_s60 / 90.0)
    out["clean_sheet"] = csp * ps * q60 * p_cs_60

    concede = np.isin(pos, GOALS_CONCEDED_POSITIONS)
    gc = ps * poisson_floor_mean(lam_ga * m_s / 90.0, GOALS_CONCEDED_PER_POINT) + psub * poisson_floor_mean(
        lam_ga * m_sub / 90.0, GOALS_CONCEDED_PER_POINT
    )
    out["goals_conceded"] = np.where(concede, -gc, 0.0)

    gk = pos == 1
    save_lam = fx["saves90"].to_numpy() * m_s / 90.0 * fx["opp_att_mult"].to_numpy(dtype=float)
    saves = ps * poisson_floor_mean(save_lam, SAVES_PER_POINT) + PENALTY_SAVE_POINTS * params.penalty_save90 * xmins / 90.0
    out["saves"] = np.where(gk, saves, 0.0)

    # Bonus: the fitted regression, evaluated for the 60+ scenario and the short one.
    bps90 = fx["bps90"].to_numpy()
    long_x = _bonus_features(pos, m_s60 / 90.0, fx["xg90"].to_numpy() * m_s60 / 90 * att,
                             fx["xa90"].to_numpy() * m_s60 / 90 * att, lam_ga, bps90, params)
    short_x = _bonus_features(pos, m_sub / 90.0, fx["xg90"].to_numpy() * m_sub / 90 * att,
                              fx["xa90"].to_numpy() * m_sub / 90 * att, lam_ga, bps90, params)
    out["bonus"] = p60 * params.bonus.predict(long_x) + np.maximum(ps + psub - p60, 0.0) * params.bonus.predict(short_x)

    defcon = np.zeros(len(fx))
    if defcon_live:
        dc90 = fx["dc90"].to_numpy()
        for p, glm in params.defcon.items():
            m = pos == p
            if not m.any():
                continue
            hit_long = glm.predict(np.log(dc90[m] * m_s60[m] / 90.0 + 0.1)[:, None])
            hit_short = glm.predict(np.log(dc90[m] * m_sub[m] / 90.0 + 0.1)[:, None])
            defcon[m] = DEFCON_POINTS * (p60[m] * hit_long + np.maximum(ps[m] + psub[m] - p60[m], 0) * hit_short)
    out["defensive_contribution"] = defcon

    red90 = np.array([params.red90.get(int(p), 0.0) for p in pos])
    og90 = np.array([params.own_goal90.get(int(p), 0.0) for p in pos])
    out["discipline"] = (
        YELLOW_POINTS * fx["yc90"].to_numpy() * xmins / 90.0
        + RED_POINTS * red90 * xmins / 90.0
        + OWN_GOAL_POINTS * og90 * xmins / 90.0
        + PENALTY_MISS_POINTS * params.penalty_miss_per_xg * e_xg
    )

    out["xmins"] = xmins
    out["p_start"] = ps
    out["p_play"] = ps + psub
    return out


def predict(
    asof,
    params: FittedParams,
    gws: Sequence[int] | None = None,
    availability: Mapping[int, float] | None = None,
    config: FittedConfig = DEFAULT_CONFIG,
) -> list[GameweekXP]:
    """One `GameweekXP` per registered player per gameweek in `gws`.

    `availability` maps element id to the probability the player is fit
    (see `availability_from_status`); missing players are treated as available,
    which is the neutral setting a backtest must use.
    """
    gws = [asof.gw] if gws is None else list(gws)
    panel = _panel(asof, gws, config)
    tp = panel.pgw[panel.pgw["is_target"]].set_index("pgw_id")
    state = player_state(tp, params)
    fx = panel.target.merge(
        state.drop(columns=["element_type"]), left_on="pgw_id", right_index=True, how="left"
    )
    avail = availability or {}
    fx["availability"] = [float(avail.get(int(e), 1.0)) for e in fx["element_id"]]
    defcon_live = asof.season >= DEFCON_FIRST_SEASON
    comps = fixture_components(fx, params, defcon_live) if len(fx) else pd.DataFrame(
        columns=[*COMPONENT_KEYS, "xmins", "p_start", "p_play"]
    )
    comps["element_id"] = fx["element_id"].to_numpy()
    comps["gw"] = fx["gw"].to_numpy()

    sums = comps.groupby(["element_id", "gw"]).agg(
        **{k: (k, "sum") for k in (*COMPONENT_KEYS, "xmins")},
        p_start=("p_start", "first"),
        p_play=("p_play", "first"),
        fixture_count=("p_start", "size"),
    )
    results: list[GameweekXP] = []
    for element_id in asof.players["element_id"].astype(int):
        for gw in gws:
            key = (element_id, gw)
            if key not in sums.index:
                # A blank: exactly zero, not a small number.
                results.append(
                    GameweekXP(
                        element_id=element_id, gw=gw, xp=0.0, xmins=0.0, p_start=0.0,
                        p_play=0.0, fixture_count=0, components={k: 0.0 for k in COMPONENT_KEYS},
                    )
                )
                continue
            row = sums.loc[key]
            # Round once, at the edge, so the stored components sum exactly to xp.
            components = {k: round(float(row[k]), 3) for k in COMPONENT_KEYS}
            results.append(
                GameweekXP(
                    element_id=element_id,
                    gw=gw,
                    xp=round(sum(components.values()), 3),
                    xmins=round(float(row["xmins"]), 2),
                    p_start=round(float(row["p_start"]), 4),
                    p_play=round(float(row["p_play"]), 4),
                    fixture_count=int(row["fixture_count"]),
                    components=components,
                )
            )
    return results


class FittedXPModel:
    """`fit` then `predict` on `AsOf` views: the backtest and production share this."""

    def __init__(self, config: FittedConfig = DEFAULT_CONFIG):
        self.config = config
        self.params: FittedParams | None = None

    def fit(self, asof) -> FittedParams:
        self.params = fit(asof, self.config)
        return self.params

    def predict(
        self,
        asof,
        gws: Sequence[int] | None = None,
        availability: Mapping[int, float] | None = None,
    ) -> list[GameweekXP]:
        if self.params is None:
            self.fit(asof)
        return predict(asof, self.params, gws, availability, self.config)


def xp_rows(
    asof,
    model: FittedXPModel,
    gws: Sequence[int],
    availability: Mapping[int, float] | None = None,
) -> list[dict[str, Any]]:
    """Rows in exactly the shape `compute_xp.py` upserts into `xpoints`.

    `asof` is the view for the next gameweek (its `gw` is the first target and
    `as_of_gw` the last finished one); `gws` is the horizon. The same fitted
    parameters and the same as-of features serve every gameweek in the horizon,
    because nothing later than `as_of_gw` is known for any of them.
    """
    return [
        {
            "season": asof.season,
            "element_id": r.element_id,
            "gw": r.gw,
            "model_version": MODEL_VERSION,
            "xp": r.xp,
            "xmins": r.xmins,
            "p_start": r.p_start,
            "p_play": r.p_play,
            "components": r.components,
            "as_of_gw": asof.as_of_gw,
            "fixture_count": r.fixture_count,
        }
        for r in model.predict(asof, gws, availability)
    ]
