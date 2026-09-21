"""Walk-forward evaluation: predict each gameweek using only what came before it.

For every target gameweek, in order, the harness builds an `AsOf` view, lets each
model refit on it if due, asks for predictions, and lines them up against the
actuals — which the harness reads from `History` and no model ever sees.

A model is any object with a `name` and `predict(asof) -> {element_id: xp}`. A
model that learns parameters may also define `fit(asof)`; it is called with the
same view the prediction will use, so training data is everything strictly
before the target and nothing after. That is the only training interface on
offer, which is the point: there is no way to hand a model "the whole dataset".

The one exception is a *recorded reference*: a number that was never a
prediction from data, only a figure someone else wrote down — FPL's recorded
`fpl_xp`. It defines `predict_from_history(history, season, gw)` and is handed
`History` directly, because the value it reports is a target-gameweek field that
`AsOf` deliberately withholds. That door is for replaying recorded benchmarks
only; a candidate model that used it would not be backtested at all.

Evaluation population, decided here once so that every model is judged on the
same players:

* **Registered at the target.** A player is in the population for gameweek `t`
  if he was on a club's books at the deadline (see `History.registered`). Players
  sold before the deadline and January signings not yet arrived are out.
* **Blanks are excluded from the metrics.** A player whose team has no fixture
  scores 0 and every sane model predicts 0; leaving ~40 trivially-correct zeros
  per blank gameweek in would flatter MAE and rank correlation for every model
  alike while telling us nothing. They are kept in the prediction frame, and any
  non-zero prediction for one is counted as a `blank_violation` — that is a bug
  in the model, not a modelling miss.
* **Non-playing squad players stay in.** Most of the ~650 registered players do
  not play in a given week. Excluding them would condition on the outcome
  (minutes) and let a model skip the hardest part of the job, predicting who
  plays. A separate, outcome-free `regular` population (3+ starts in a player's
  last 5 rows, from `AsOf` only) is reported alongside for a less zero-dominated
  view.

Seasons evaluated by default: 2024-25 and 2025-26 in full, and 2026-27 from
gameweek 2. 2023-24 is the earliest season loaded, so a model predicting it has
no prior season to learn from and early 2023-24 would be judged on a regime no
future prediction will ever be in. It is kept as training history and can be
evaluated explicitly (`--seasons 2023-24`), but it is not part of the default
comparison.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from .data import AsOf, History

DEFAULT_SEASONS = ("2024-25", "2025-26", "2026-27")

# Where evaluation starts within a season when it is not gameweek 1.
DEFAULT_FIRST_GW = {"2026-27": 2}

# A "regular": 3+ starts across his last 5 stat rows, which (since FPL writes a
# row for every registered player whose team plays) is roughly his team's last
# five fixtures. Computed from AsOf, so it cannot peek at the target.
REGULAR_WINDOW = 5
REGULAR_MIN_STARTS = 3


@runtime_checkable
class Model(Protocol):
    name: str

    def predict(self, asof: AsOf) -> Mapping[int, float]: ...


@dataclass(frozen=True)
class Target:
    season: str
    gw: int


@dataclass
class ModelRunStats:
    """Bookkeeping per model: what it covered and anything it got structurally wrong."""

    fits: int = 0
    fit_seconds: float = 0.0
    predict_seconds: float = 0.0
    predicted: int = 0  # population rows (non-blank) with a prediction
    missing: int = 0  # population rows (non-blank) with none
    blank_violations: int = 0  # non-zero prediction for a player with no fixture
    unknown_ids: int = 0  # predictions for players not registered at the target
    non_finite: int = 0  # NaN or inf predictions, treated as missing


@dataclass
class BacktestResult:
    targets: list[Target]
    predictions: pd.DataFrame  # one row per (target, registered player, model)
    stats: dict[str, ModelRunStats] = field(default_factory=dict)
    refit_every: int | None = None


def default_targets(
    history: History,
    seasons: Sequence[str] = DEFAULT_SEASONS,
    first_gw: Mapping[str, int] | None = None,
) -> list[Target]:
    """Every gameweek with results in `seasons`, in chronological order."""
    first_gw = DEFAULT_FIRST_GW if first_gw is None else first_gw
    targets = []
    for season in sorted(seasons):
        if season not in history.seasons:
            continue
        start = first_gw.get(season, 1)
        targets.extend(Target(season, gw) for gw in history.gameweeks(season) if gw >= start)
    return targets


def regulars(asof: AsOf) -> set[int]:
    """Players with REGULAR_MIN_STARTS+ starts in their last REGULAR_WINDOW rows."""
    history = asof.player_history
    if history.empty:
        return set()
    recent = history.groupby("element_id", sort=False).tail(REGULAR_WINDOW)
    starts = recent.groupby("element_id")["starts"].sum()
    return {int(e) for e in starts[starts >= REGULAR_MIN_STARTS].index}


def _fit_due(
    last_fit: Target | None, target: Target, targets_since: int, refit_every: int | None
) -> bool:
    if last_fit is None or last_fit.season != target.season:
        return True
    if refit_every is None:
        return False
    return targets_since >= refit_every


def run_backtest(
    history: History,
    models: Sequence[Model],
    targets: Iterable[Target],
    *,
    refit_every: int | None = 1,
    progress: Callable[[Target], None] | None = None,
) -> BacktestResult:
    """Walk forward through `targets`, predicting each with only earlier data.

    `refit_every` is how many target gameweeks pass between calls to a model's
    `fit`: 1 refits before every prediction, the way the nightly job would, and
    None fits once at the start of each season. A model is always refit at the
    first target of a new season, because a new season means new element ids.
    """
    targets = list(targets)
    names = [m.name for m in models]
    if len(set(names)) != len(names):
        raise ValueError(f"model names must be unique, got {names}")

    run_stats = {m.name: ModelRunStats() for m in models}
    last_fit: dict[str, Target | None] = {m.name: None for m in models}
    since_fit: dict[str, int] = {m.name: 0 for m in models}
    frames: list[pd.DataFrame] = []

    for target in targets:
        if progress:
            progress(target)
        actual = history.actuals(target.season, target.gw)
        regular = regulars(history.asof(target.season, target.gw))

        base = actual.assign(
            season=target.season,
            gw=target.gw,
            blank=actual["fixture_count"] == 0,
            regular=actual["element_id"].isin(regular),
        )
        registered = set(int(e) for e in base["element_id"])

        for model in models:
            stats = run_stats[model.name]
            # A fresh view per model: frames are mutable, and one model adding a
            # column or dropping rows must not change what the next one sees.
            asof = history.asof(target.season, target.gw)

            fit = getattr(model, "fit", None)
            if callable(fit) and _fit_due(
                last_fit[model.name], target, since_fit[model.name], refit_every
            ):
                started = time.perf_counter()
                fit(asof)
                stats.fit_seconds += time.perf_counter() - started
                stats.fits += 1
                last_fit[model.name] = target
                since_fit[model.name] = 0

            started = time.perf_counter()
            reference = getattr(model, "predict_from_history", None)
            if callable(reference):
                raw = reference(history, target.season, target.gw) or {}
            else:
                raw = model.predict(asof) or {}
            stats.predict_seconds += time.perf_counter() - started
            since_fit[model.name] += 1

            preds: dict[int, float] = {}
            for element_id, value in raw.items():
                element_id = int(element_id)
                if element_id not in registered:
                    stats.unknown_ids += 1
                    continue
                if value is None or not np.isfinite(float(value)):
                    stats.non_finite += 1
                    continue
                preds[element_id] = float(value)

            frame = base.assign(model=model.name, pred=base["element_id"].map(preds))
            blank = frame["blank"]
            stats.blank_violations += int((blank & frame["pred"].fillna(0).abs().gt(1e-9)).sum())
            stats.predicted += int((~blank & frame["pred"].notna()).sum())
            stats.missing += int((~blank & frame["pred"].isna()).sum())
            frames.append(frame)

    columns = [
        "season",
        "gw",
        "element_id",
        "code",
        "element_type",
        "team_fpl_id",
        "fixture_count",
        "blank",
        "regular",
        "actual",
        "actual_minutes",
        "model",
        "pred",
    ]
    predictions = (
        pd.concat(frames, ignore_index=True)[columns]
        if frames
        else pd.DataFrame(columns=columns)
    )
    return BacktestResult(
        targets=targets,
        predictions=predictions,
        stats=run_stats,
        refit_every=refit_every,
    )


def model_summary(stats: ModelRunStats) -> dict[str, Any]:
    total = stats.predicted + stats.missing
    return {
        "fits": stats.fits,
        "fit_seconds": round(stats.fit_seconds, 2),
        "predict_seconds": round(stats.predict_seconds, 2),
        "coverage": round(stats.predicted / total, 4) if total else 0.0,
        "predicted": stats.predicted,
        "missing": stats.missing,
        "blank_violations": stats.blank_violations,
        "unknown_ids": stats.unknown_ids,
        "non_finite": stats.non_finite,
    }
