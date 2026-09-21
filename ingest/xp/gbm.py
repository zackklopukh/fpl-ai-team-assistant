"""The gradient-boosting expected-points model, `gbm-0.1`.

A challenger to `baseline-0.1` built on scikit-learn's HistGradientBoosting — the
same histogram algorithm as LightGBM, without LightGBM's system OpenMP
dependency. The statistics live here; the solver never changes because of it
(CLAUDE.md: the model and the solver are separate concerns).

**Unit of prediction: one fixture.** A gameweek is the sum of its fixtures'
predictions, so a double is two predictions added and a blank is exactly 0 by
construction — there is no fixture to predict.

**Two parts, because points are two different questions.** Most of the error in
FPL projection is "does he play", and points given playing is a different,
heavy-tailed distribution. So:

1. A classifier over minutes classes — did not play, came off the bench, started
   but went off before 60, started and played 60+ — gives `p_play`, `p_start`
   and expected minutes directly.
2. A regressor for points *given* the minutes class, trained only on
   appearances, with the class as an input.

Expected points = sum over classes of P(class) x E[points | class]. The
non-playing class scores exactly 0, which is a fact rather than something a
regressor has to learn from 60% zeros.

**Loss.** Squared error on points given playing. Squared error estimates the
conditional mean, which is what the solver needs (an expectation it can add up
and double for the captain); Poisson would too but assumes non-negative targets
and points go to -3. The top-end compression that squared error is blamed for
comes mostly from averaging non-players into the same target, which the two-part
split removes: the regressor never sees the zeros, so a nailed-on premium
forward is compared with other appearances, not with the bench. What remains is
checked by the calibration table in the backtest, and a monotone post-hoc
calibration fitted out-of-time on training data is available (`calibrate=True`)
if the top end still sits low.

**Leakage.** Every feature is built by `gbm_features.build_features`, which
stops every window at the first row of the row's own gameweek. The model is only
ever handed an `AsOf`; `fpl_xp` is never read.

**Availability.** There is no historical injury data, so nothing is learned
from it. In production the live `status` / `chance_of_playing_next_round` is
applied as a multiplier on the minutes-class probabilities (`availability`);
the backtest passes none, which is neutral.

**Production.** The nightly job builds an `AsOf` for the next gameweek with
`live_asof`, fits on it, then predicts each horizon gameweek's `AsOf`, and
`xp_rows` returns rows shaped exactly like `compute_xp.build_xp_rows`. See
`compute_live` for the whole call. A fit takes seconds, so the model is refit
nightly and never persisted.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression

from .gbm_features import (
    MIN_CLASS_FULL_START,
    MIN_CLASS_NONE,
    MIN_CLASS_SHORT_START,
    MIN_CLASS_SUB,
    build_features,
    feature_columns,
)

MODEL_VERSION = "gbm-0.1"

PLAYING_CLASSES = (MIN_CLASS_SUB, MIN_CLASS_SHORT_START, MIN_CLASS_FULL_START)

# Appearance points by minutes class: 1 for playing, 2 for 60+ minutes.
APPEARANCE_BY_CLASS = {MIN_CLASS_SUB: 1.0, MIN_CLASS_SHORT_START: 1.0, MIN_CLASS_FULL_START: 2.0}

# Availability by status flag when chance_of_playing is null — the same table as
# baseline-0.1, so the two models treat injury news identically.
STATUS_AVAILABILITY = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}

COMPONENT_KEYS = ("appearance", "returns")


@dataclass(frozen=True)
class GbmConfig:
    """Every knob, in one place, so a backtest run records exactly what it tested."""

    # Stage 1, minutes classifier.
    clf_learning_rate: float = 0.1
    clf_max_iter: int = 100
    clf_max_leaf_nodes: int = 31
    clf_min_samples_leaf: int = 200
    # Stage 2, points given playing.
    reg_learning_rate: float = 0.05
    reg_max_iter: int = 200
    reg_max_leaf_nodes: int = 15
    reg_min_samples_leaf: int = 200
    reg_l2: float = 1.0
    reg_loss: str = "squared_error"
    # "twostage" (above) or "direct": one regressor on all rows, for comparison.
    structure: str = "twostage"
    # Fit an isotonic map from raw to calibrated per-fixture xP on an out-of-time
    # slice of the training data. See `_fit_calibration`.
    calibrate: bool = False
    calibration_gws: int = 8
    # Older seasons count less: a player's role and the scoring rules drift.
    season_decay: float = 1.0
    random_state: int = 0
    drop_features: tuple[str, ...] = ()


@dataclass
class FitInfo:
    rows: int = 0
    appearances: int = 0
    seconds: float = 0.0
    features: list[str] = field(default_factory=list)
    class_minutes: dict[int, float] = field(default_factory=dict)


def _hgb_classifier(cfg: GbmConfig) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=cfg.clf_learning_rate,
        max_iter=cfg.clf_max_iter,
        max_leaf_nodes=cfg.clf_max_leaf_nodes,
        min_samples_leaf=cfg.clf_min_samples_leaf,
        early_stopping=False,  # its validation split is random rows, i.e. not out-of-time
        random_state=cfg.random_state,
    )


def _hgb_regressor(cfg: GbmConfig) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss=cfg.reg_loss,
        learning_rate=cfg.reg_learning_rate,
        max_iter=cfg.reg_max_iter,
        max_leaf_nodes=cfg.reg_max_leaf_nodes,
        min_samples_leaf=cfg.reg_min_samples_leaf,
        l2_regularization=cfg.reg_l2,
        early_stopping=False,
        random_state=cfg.random_state,
    )


# --- Target rows -----------------------------------------------------------------


def target_rows(asof: Any) -> pd.DataFrame:
    """Pseudo rows for the target gameweek: one per registered player per fixture.

    Built from `asof.players` (club at the target) and `asof.target_fixtures()`.
    A player whose club blanks gets no row — he has no fixture to predict — and
    a double gets two. No stat column is filled in: they are what is predicted.
    """
    players = asof.players[asof.players["team_fpl_id"].notna()]
    fx = asof.target_fixtures()
    if players.empty or fx.empty:
        return pd.DataFrame(
            columns=["element_id", "code", "season", "gw", "fixture_id", "element_type",
                     "team_code", "opp_code", "was_home"]
        )
    sides = pd.concat(
        [
            fx.assign(team_fpl_id=fx["team_h_fpl_id"], team_code=fx["team_h_code"],
                      opp_code=fx["team_a_code"], was_home=True),
            fx.assign(team_fpl_id=fx["team_a_fpl_id"], team_code=fx["team_a_code"],
                      opp_code=fx["team_h_code"], was_home=False),
        ]
    )[["fixture_id", "team_fpl_id", "team_code", "opp_code", "was_home"]]
    sides["team_fpl_id"] = sides["team_fpl_id"].astype("int64")
    p = players[["element_id", "code", "element_type", "team_fpl_id"]].copy()
    p["team_fpl_id"] = p["team_fpl_id"].astype("int64")
    rows = p.merge(sides, on="team_fpl_id", how="inner")
    rows["season"] = asof.season
    rows["gw"] = int(asof.gw)
    return rows.drop(columns=["team_fpl_id"]).reset_index(drop=True)


def availability_multiplier(status: str | None, chance_of_playing: Any) -> float:
    """Live availability as a probability the player is selectable.

    `chance_of_playing` wins when present; otherwise the status letter decides.
    Mirrors `xp.model.availability` so both models read injury news the same way.
    """
    if chance_of_playing is not None and not pd.isna(chance_of_playing):
        return max(0.0, min(1.0, float(chance_of_playing) / 100.0))
    return STATUS_AVAILABILITY.get(status or "a", 0.0)


# --- The model ---------------------------------------------------------------------


class GbmXpModel:
    """Fit on an `AsOf`, predict an `AsOf`. Holds nothing between fits but the trees."""

    def __init__(self, config: GbmConfig | None = None):
        self.config = config or GbmConfig()
        self.clf: dict[str, HistGradientBoostingClassifier] | None = None
        self.reg: HistGradientBoostingRegressor | None = None
        self.calibrator: IsotonicRegression | None = None
        self.features: list[str] = []
        self.info = FitInfo()
        # Features built during `fit` are reused by a `predict` on the same view.
        # The view itself is held, not its id: an id can be reused once the
        # object is freed, and a stale hit would predict from the wrong week.
        self._cache: tuple[Any, pd.DataFrame] | None = None

    # -- Features -----------------------------------------------------------------

    def frame(self, asof: Any, with_targets: bool = True) -> pd.DataFrame:
        """History rows and target pseudo rows for `asof`, with every feature."""
        if self._cache is not None and self._cache[0] is asof:
            return self._cache[1]
        targets = target_rows(asof) if with_targets else None
        frame = build_features(asof.stats, asof.fixtures, targets=targets)
        if targets is not None and len(targets):
            ids = targets.set_index(["code", "fixture_id"])["element_id"]
            key = pd.MultiIndex.from_arrays([frame["code"], frame["fixture_id"]])
            frame["element_id"] = np.where(frame["is_target"], ids.reindex(key).to_numpy(), -1)
        else:
            frame["element_id"] = -1
        self._cache = (asof, frame)
        return frame

    # -- Fitting ------------------------------------------------------------------

    def fit(self, asof: Any) -> GbmXpModel:
        started = time.perf_counter()
        frame = self.frame(asof)
        train = frame[~frame["is_target"]]
        self._fit_frame(train)
        if self.config.calibrate:
            self._fit_calibration(train)
        self.info.seconds = time.perf_counter() - started
        return self

    def _weights(self, train: pd.DataFrame) -> np.ndarray | None:
        if self.config.season_decay >= 1.0:
            return None
        latest = train["t"].max() // 100
        age = latest - train["t"] // 100
        return np.power(self.config.season_decay, age.to_numpy(dtype="float64"))

    def _fit_frame(self, train: pd.DataFrame) -> None:
        cfg = self.config
        candidates = [f for f in feature_columns(train) if f not in cfg.drop_features and f != "element_id"]
        # A column with fewer than two distinct values carries nothing to split
        # on — defensive contribution before 2025-26 is all missing, the rule-era
        # flag is constant — and HistGradientBoosting's binning rejects it.
        self.features = [f for f in candidates if train[f].nunique(dropna=True) >= 2]
        X = train[self.features].to_numpy(dtype="float64")
        w = self._weights(train)
        cls = train["min_class"].to_numpy()
        self.info = FitInfo(rows=len(train), features=list(self.features))
        mins = train["y_minutes"].to_numpy()
        self.info.class_minutes = {
            int(c): float(mins[cls == c].mean()) if (cls == c).any() else 0.0
            for c in (MIN_CLASS_NONE, *PLAYING_CLASSES)
        }

        self.clf = self._fit_minutes(X, cls, w)
        if cfg.structure == "direct":
            self.reg = _hgb_regressor(cfg).fit(X, train["y"].to_numpy(), sample_weight=w)
            self.info.appearances = int((cls > 0).sum())
            return

        played = cls != MIN_CLASS_NONE
        Xp = np.column_stack([X[played], cls[played].astype("float64")])
        self.reg = _hgb_regressor(cfg).fit(
            Xp, train["y"].to_numpy()[played], sample_weight=None if w is None else w[played]
        )
        self.info.appearances = int(played.sum())

    def _fit_minutes(self, X: np.ndarray, cls: np.ndarray, w: np.ndarray | None) -> dict[str, Any]:
        """Three chained binary classifiers: plays; starts given plays; 60+ given starts.

        Equivalent in what it can express to one four-class classifier, at about
        a third of the cost (a multiclass booster grows a tree per class per
        round), and each link is a question a person would ask about a player.
        """
        cfg = self.config

        def sub(mask: np.ndarray) -> np.ndarray | None:
            return None if w is None else w[mask]

        plays = cls != MIN_CLASS_NONE
        starts = (cls == MIN_CLASS_SHORT_START) | (cls == MIN_CLASS_FULL_START)
        return {
            "play": _hgb_classifier(cfg).fit(X, plays.astype(int), sample_weight=w),
            "start": _hgb_classifier(cfg).fit(X[plays], starts[plays].astype(int), sample_weight=sub(plays)),
            "sixty": _hgb_classifier(cfg).fit(
                X[starts], (cls[starts] == MIN_CLASS_FULL_START).astype(int), sample_weight=sub(starts)
            ),
        }

    def _fit_calibration(self, train: pd.DataFrame) -> None:
        """Isotonic raw -> calibrated per-fixture xP, fitted out-of-time.

        A copy of the model is fitted on training rows before the last
        `calibration_gws` gameweeks and scored on those gameweeks, whose features
        are as-of their own deadlines like any other row. The isotonic map from
        that out-of-time prediction to actual points is then applied to the full
        model. Nothing here reads beyond the training view.
        """
        times = np.sort(train["t"].unique())
        if len(times) <= self.config.calibration_gws + 5:
            self.calibrator = None
            return
        cut = times[-self.config.calibration_gws]
        early, late = train[train["t"] < cut], train[train["t"] >= cut]
        helper = GbmXpModel(replace(self.config, calibrate=False))
        helper._fit_frame(early)
        raw = helper._predict_components(late)["xp"].to_numpy()
        self.calibrator = IsotonicRegression(out_of_bounds="clip").fit(raw, late["y"].to_numpy())

    # -- Prediction ---------------------------------------------------------------

    def _predict_components(self, rows: pd.DataFrame) -> pd.DataFrame:
        """Per-fixture predictions and their parts, before availability."""
        X = rows[self.features].to_numpy(dtype="float64")
        def positive(name: str) -> np.ndarray:
            model = self.clf[name]
            p = model.predict_proba(X)
            classes = list(model.classes_)
            return p[:, classes.index(1)] if 1 in classes else np.zeros(len(X))

        p_play, p_start, p_sixty = positive("play"), positive("start"), positive("sixty")
        proba = np.column_stack(
            [
                1.0 - p_play,
                p_play * (1.0 - p_start),
                p_play * p_start * (1.0 - p_sixty),
                p_play * p_start * p_sixty,
            ]
        )
        out = pd.DataFrame(index=rows.index)
        for c in range(4):
            out[f"p{c}"] = proba[:, c]
        if self.config.structure == "direct":
            out["xp"] = self.reg.predict(X)
            appearance = sum(proba[:, c] * APPEARANCE_BY_CLASS[c] for c in PLAYING_CLASSES)
            out["appearance"] = appearance
        else:
            xp = np.zeros(len(rows))
            appearance = np.zeros(len(rows))
            for c in PLAYING_CLASSES:
                pts = self.reg.predict(np.column_stack([X, np.full(len(rows), float(c))]))
                out[f"pts_given_{c}"] = pts
                xp += proba[:, c] * pts
                appearance += proba[:, c] * APPEARANCE_BY_CLASS[c]
            out["xp"] = xp
            out["appearance"] = appearance
        out["xmins"] = sum(proba[:, c] * self.info.class_minutes.get(c, 0.0) for c in PLAYING_CLASSES)
        return out

    def predict_fixtures(
        self, asof: Any, availability: Mapping[int, float] | None = None
    ) -> pd.DataFrame:
        """One row per (player, target fixture) with xp and its parts."""
        if self.clf is None:
            raise RuntimeError("fit() before predict()")
        frame = self.frame(asof)
        rows = frame[frame["is_target"]]
        if rows.empty:
            return pd.DataFrame(columns=["element_id", "fixture_id", "xp", "xmins", "p_start", "p_play", "appearance"])
        parts = self._predict_components(rows)
        xp = parts["xp"].to_numpy()
        if self.calibrator is not None:
            xp = self.calibrator.predict(xp)
        out = pd.DataFrame(
            {
                "element_id": rows["element_id"].astype("int64").to_numpy(),
                "fixture_id": rows["fixture_id"].to_numpy(),
                "xp": xp,
                "xmins": parts["xmins"].to_numpy(),
                "p_start": (parts["p2"] + parts["p3"]).to_numpy(),
                "p_play": (1.0 - parts["p0"]).to_numpy(),
                "appearance": parts["appearance"].to_numpy(),
            }
        )
        if availability:
            # Unavailability scales every playing class down together: an injured
            # player's expected points, minutes and appearance all go to ~0.
            mult = out["element_id"].map(lambda e: availability.get(int(e), 1.0)).to_numpy(dtype="float64")
            for c in ("xp", "xmins", "p_start", "p_play", "appearance"):
                out[c] = out[c] * mult
        out["returns"] = out["xp"] - out["appearance"]
        return out

    def predict_gameweek(
        self, asof: Any, availability: Mapping[int, float] | None = None
    ) -> pd.DataFrame:
        """One row per registered player: fixture predictions summed over the gameweek.

        Every registered player appears. A blank sums zero fixtures and is exactly
        0; a double sums two.
        """
        per_fixture = self.predict_fixtures(asof, availability)
        players = asof.players[["element_id"]].copy()
        players["element_id"] = players["element_id"].astype("int64")
        cols = ["xp", "xmins", "p_start", "p_play", "appearance", "returns"]
        if per_fixture.empty:
            summed = pd.DataFrame(columns=cols + ["fixture_count"])
        else:
            agg = {c: (c, "sum") for c in cols}
            # Probabilities are per fixture: report the chance of playing at least
            # once, not a sum that could exceed 1.
            agg["p_start"] = ("p_start", lambda s: 1.0 - np.prod(1.0 - s.to_numpy()))
            agg["p_play"] = ("p_play", lambda s: 1.0 - np.prod(1.0 - s.to_numpy()))
            agg["fixture_count"] = ("fixture_id", "count")
            summed = per_fixture.groupby("element_id").agg(**agg)
        out = players.merge(summed, left_on="element_id", right_index=True, how="left")
        for c in cols + ["fixture_count"]:
            out[c] = out[c].fillna(0.0)
        out["fixture_count"] = out["fixture_count"].astype("int64")
        return out.reset_index(drop=True)

    def predict(self, asof: Any, availability: Mapping[int, float] | None = None) -> dict[int, float]:
        g = self.predict_gameweek(asof, availability)
        return {int(e): float(v) for e, v in zip(g["element_id"], g["xp"], strict=True)}

    # -- Explanation --------------------------------------------------------------

    def explain(self, asof: Any, element_id: int, groups: Mapping[str, Sequence[str]] | None = None) -> dict[str, float]:
        """Per-prediction breakdown by feature family, for one player.

        Trees have no additive decomposition, so this is an occlusion estimate:
        each family in turn is replaced by its value for an average player of the
        same position this gameweek, and the change in xP is that family's
        contribution. The parts need not sum exactly to the prediction minus the
        reference (interactions), so the remainder is reported as `interaction`.
        """
        groups = groups or FEATURE_GROUPS
        frame = self.frame(asof)
        rows = frame[frame["is_target"] & (frame["element_id"] == element_id)]
        if rows.empty:
            return {}
        pos = rows["element_type"].iloc[0]
        peers = frame[frame["is_target"] & (frame["element_type"] == pos)]
        reference = peers[self.features].median(numeric_only=True)
        base_rows = rows.copy()
        for f in self.features:
            base_rows[f] = reference.get(f, np.nan)
        full = float(self._predict_components(rows)["xp"].sum())
        ref = float(self._predict_components(base_rows)["xp"].sum())
        out = {"prediction": round(full, 3), "position_reference": round(ref, 3)}
        total = 0.0
        for name, prefixes in groups.items():
            cols = [f for f in self.features if any(f == p or f.startswith(p) for p in prefixes)]
            if not cols:
                continue
            swapped = rows.copy()
            for f in cols:
                swapped[f] = reference.get(f, np.nan)
            delta = full - float(self._predict_components(swapped)["xp"].sum())
            out[name] = round(delta, 3)
            total += delta
        out["interaction"] = round(full - ref - total, 3)
        return out


# Feature families for `explain` and grouped permutation importance.
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "minutes_role": ("mins_", "starts_", "rows_", "apps_", "mins_per_app", "prev_mins", "prev_start", "prev_apps", "prev_rows"),
    "recent_points": ("pts_", "pts90", "bonus", "prev_pts", "prev_bonus"),
    "attack_underlying": ("xg", "xa", "xgi", "goals_", "assists_", "thr", "cre", "ict", "inf", "prev_xgi"),
    "defence_underlying": ("xgc", "cs_", "saves", "defcon", "prev_xgc", "prev_defcon"),
    "bps": ("bps", "prev_bps"),
    "price_ownership": ("price", "owned_pct"),
    "fixture": ("home", "opp_", "team_", "att_vs_def", "def_vs_att", "gw_fixtures"),
    "context": ("position", "gw_num", "defcon_era"),
}


# --- Output rows ---------------------------------------------------------------------


def xp_rows(
    model: GbmXpModel,
    asof: Any,
    *,
    as_of_gw: int,
    availability: Mapping[int, float] | None = None,
) -> list[dict[str, Any]]:
    """`xpoints` rows for `asof.gw`, shaped exactly like `compute_xp.build_xp_rows`.

    `components` holds `appearance` (expected appearance points from the
    minutes classifier: 1 for playing, 2 for 60+) and `returns` (everything
    else — goals, assists, clean sheets, bonus, defensive contribution, minus
    cards — from the points-given-playing regressor). They sum to `xp`. The
    boosted model has no finer additive split; `GbmXpModel.explain` gives a
    per-feature-family breakdown for inspection.
    """
    g = model.predict_gameweek(asof, availability)
    rows = []
    for r in g.itertuples(index=False):
        appearance = round(float(r.appearance), 3)
        returns = round(float(r.returns), 3)
        rows.append(
            {
                "season": asof.season,
                "element_id": int(r.element_id),
                "gw": int(asof.gw),
                "model_version": MODEL_VERSION,
                # Rounded once so that the stored components sum to the headline.
                "xp": round(appearance + returns, 3),
                "xmins": round(float(r.xmins), 2),
                "p_start": round(float(r.p_start), 4),
                "p_play": round(float(r.p_play), 4),
                "components": {"appearance": appearance, "returns": returns},
                "as_of_gw": int(as_of_gw),
                "fixture_count": int(r.fixture_count),
            }
        )
    return rows


# --- Production -----------------------------------------------------------------------


def live_asof(history: Any, season: str, gw: int, live_players: pd.DataFrame) -> Any:
    """An `AsOf` for a gameweek that has not happened yet, from today's data.

    `History.asof` decides who is registered from rows on both sides of the
    target, which only exists in hindsight. Before the deadline the registered
    players are simply today's `players` table, so this takes them from
    `live_players` (`element_id`, `team_fpl_id`; identity is joined from
    `history.players`). Everything else is built exactly as `History.asof`
    builds it: all stat rows (every one is before the target), fixture results
    blanked from the target on, price and ownership from each player's last row.
    """
    from backtest.data import AsOf, FIXTURE_RESULT_COLUMNS, _fixture_counts

    stats = history.stats[
        (history.stats["season"] < season)
        | ((history.stats["season"] == season) & (history.stats["gw"] < gw))
    ].copy()
    ident = history.players[history.players["season"] == season]
    live = live_players[["element_id", "team_fpl_id"]].copy()
    live["element_id"] = live["element_id"].astype("int64")
    players = ident.merge(live, on="element_id", how="inner")
    players["team_fpl_id"] = players["team_fpl_id"].astype("Int64")
    team_code = history.teams[history.teams["season"] == season].set_index("fpl_id")["code"]
    players["team_code"] = players["team_fpl_id"].map(team_code).astype("Int64")
    counts = _fixture_counts(history.fixtures, season, gw)
    players["fixture_count"] = players["team_fpl_id"].map(counts).fillna(0).astype("int64")
    prior = stats[stats["season"] == season].sort_values(["element_id", "gw", "fixture_id"])
    last_seen = prior.groupby("element_id", as_index=False).last()[["element_id", "value_tenths", "selected_by"]]
    players = players.merge(last_seen.rename(columns={"value_tenths": "price_tenths"}), on="element_id", how="left")

    code_to_element = dict(zip(players["code"], players["element_id"], strict=True))
    ph = stats[stats["code"].isin(code_to_element)].copy()
    ph = ph.rename(columns={"element_id": "row_element_id"})
    ph.insert(1, "element_id", ph["code"].map(code_to_element).astype("int64"))

    fixtures = history.fixtures[history.fixtures["season"] <= season].copy()
    unresolved = (fixtures["season"] == season) & (fixtures["gw"].isna() | (fixtures["gw"] >= gw))
    for column in FIXTURE_RESULT_COLUMNS:
        fixtures[column] = fixtures[column].astype("object")
        fixtures.loc[unresolved, column] = None

    return AsOf(
        season=season,
        gw=gw,
        stats=stats.reset_index(drop=True),
        player_history=ph.sort_values(["element_id", "season", "gw", "fixture_id"]).reset_index(drop=True),
        players=players.reset_index(drop=True),
        fixtures=fixtures.reset_index(drop=True),
        teams=history.teams[history.teams["season"] <= season].reset_index(drop=True),
    )


def compute_live(
    history: Any,
    season: str,
    live_players: pd.DataFrame,
    target_gws: Sequence[int],
    as_of_gw: int,
    config: GbmConfig | None = None,
) -> list[dict[str, Any]]:
    """The nightly job's call: fit once, project every horizon gameweek.

    `live_players` is `players` for the season with `element_id, team_fpl_id,
    status, chance_of_playing_next_round`. The model is fitted on the view for
    the first target gameweek — everything that has happened — and the same
    trees project every later gameweek in the horizon, whose views differ only
    in the fixtures (no new results exist between now and then).
    """
    if not target_gws:
        return []
    availability = {
        int(r.element_id): availability_multiplier(r.status, r.chance_of_playing_next_round)
        for r in live_players.itertuples(index=False)
    }
    model = GbmXpModel(config)
    views = [live_asof(history, season, gw, live_players) for gw in target_gws]
    model.fit(views[0])
    rows: list[dict[str, Any]] = []
    for view in views:
        rows.extend(xp_rows(model, view, as_of_gw=as_of_gw, availability=availability))
    return rows
