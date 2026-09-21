"""Metrics that say whether a model would make better squad decisions.

Regression error alone is the wrong yardstick for an optimizer's input. The
solver only ever asks "who is better than whom, among players I can afford", so
a model that predicts everyone 0.5 points too high but orders them perfectly is
worth more than one with lower MAE and a scrambled top twenty. So alongside MAE
and RMSE this reports:

* **Spearman per gameweek, averaged** — ordering within a week, which is what a
  transfer decision uses. Pooling all weeks would reward a model for knowing a
  double gameweek scores more than a single, which is fixture arithmetic, not
  skill.
* **Top-k hit rate** — of the week's actual top k scorers, how many were in the
  model's top k.
* **Decision value** — per position, the mean actual points of the model's top-N
  picks, with N a squad's worth (2 GKP, 5 DEF, 5 MID, 3 FWD). Summed over
  positions it is "points scored per gameweek by the 15 players this model rates
  highest": the closest cheap proxy for what the optimizer cares about. It
  ignores budget, club limits, captaincy and the bench, so read it as a
  ranking-quality number with points as its unit, not as a squad score.
* **Calibration** — actual vs predicted mean by predicted-xP bucket. The solver
  compares absolute numbers across positions and against a 4-point hit, so a "6"
  has to really be a 6.

Every function takes a frame with at least `season`, `gw`, `element_id`,
`element_type`, `pred` and `actual`, already restricted to the population.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

# A squad's worth per position: what the optimizer ultimately picks.
SQUAD_PICKS: dict[int, int] = {1: 2, 2: 5, 3: 5, 4: 3}

POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

TOP_KS = (10, 20)

CALIBRATION_EDGES = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, math.inf)

# Early season: where last season's evidence carries most of the weight and a
# model without cross-season memory is effectively guessing.
EARLY_SEASON_LAST_GW = 6

_GW_KEYS = ["season", "gw"]


def _clean(value: float) -> float | None:
    """JSON-friendly: NaN becomes None, everything else rounded."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return round(float(value), 4)


def _ranked(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Sort best-first, breaking ties on element id so results are reproducible."""
    return df.sort_values([by, "element_id"], ascending=[False, True], kind="mergesort")


# --- Point error -----------------------------------------------------------


def mae(df: pd.DataFrame) -> float:
    if df.empty:
        return math.nan
    return float((df["pred"] - df["actual"]).abs().mean())


def rmse(df: pd.DataFrame) -> float:
    if df.empty:
        return math.nan
    return float(np.sqrt(((df["pred"] - df["actual"]) ** 2).mean()))


def bias(df: pd.DataFrame) -> float:
    """Mean prediction minus mean actual. Positive means over-predicting."""
    if df.empty:
        return math.nan
    return float((df["pred"] - df["actual"]).mean())


# --- Ordering --------------------------------------------------------------


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties.

    Written out rather than taken from scipy, which is not a dependency of the
    ingest jobs. NaN when either side is constant — a model that predicts the
    same number for everyone has no ordering to score.
    """
    ra = pd.Series(np.asarray(a, dtype=float)).rank(method="average").to_numpy()
    rb = pd.Series(np.asarray(b, dtype=float)).rank(method="average").to_numpy()
    if len(ra) < 3 or ra.std() == 0 or rb.std() == 0:
        return math.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def spearman_by_gw(df: pd.DataFrame) -> pd.Series:
    """Spearman within each gameweek."""
    if df.empty:
        return pd.Series(dtype="float64")
    return df.groupby(_GW_KEYS).apply(
        lambda g: spearman(g["pred"], g["actual"]), include_groups=False
    )


def mean_spearman(df: pd.DataFrame) -> float:
    values = spearman_by_gw(df).dropna()
    return float(values.mean()) if len(values) else math.nan


def topk_hits(group: pd.DataFrame, k: int) -> float:
    """Fraction of the model's top k who were among the week's actual top k.

    "Among the actual top k" means scoring at least the k-th highest actual.
    Points are small integers and ties at that line are common; this counts any
    player tied with the k-th best as a hit rather than letting an arbitrary
    tie-break decide, which can nudge the rate slightly above a strict
    set-intersection figure.
    """
    if len(group) < k:
        return math.nan
    threshold = group["actual"].nlargest(k).iloc[-1]
    picks = _ranked(group, "pred").head(k)
    return float((picks["actual"] >= threshold).sum() / k)


def topk_hit_rate(df: pd.DataFrame, k: int) -> float:
    if df.empty:
        return math.nan
    rates = df.groupby(_GW_KEYS).apply(lambda g: topk_hits(g, k), include_groups=False).dropna()
    return float(rates.mean()) if len(rates) else math.nan


# --- Decision value --------------------------------------------------------


def decision_value(
    df: pd.DataFrame,
    picks: Mapping[int, int] = SQUAD_PICKS,
    by: str = "pred",
) -> dict[str, Any]:
    """Mean actual points of the top-N by `by`, per position, averaged over weeks.

    `by="actual"` gives the oracle — the best any ranking could have done — which
    is what makes the model's number interpretable.
    """
    per_position: dict[str, float | None] = {}
    squad_total = 0.0
    complete = True
    for position, n in picks.items():
        pos = df[df["element_type"] == position]
        if pos.empty:
            per_position[POSITION_NAMES[position]] = None
            complete = False
            continue
        weekly = pos.groupby(_GW_KEYS).apply(
            lambda g: _ranked(g, by).head(n)["actual"].mean(), include_groups=False
        )
        mean = float(weekly.mean())
        per_position[POSITION_NAMES[position]] = _clean(mean)
        squad_total += mean * n
    return {
        "per_pick_by_position": per_position,
        "squad_points_per_gw": _clean(squad_total) if complete else None,
    }


# --- Calibration -----------------------------------------------------------


def calibration(
    df: pd.DataFrame, edges: Sequence[float] = CALIBRATION_EDGES
) -> list[dict[str, Any]]:
    """Mean actual against mean predicted, by bucket of predicted xP."""
    if df.empty:
        return []
    buckets = pd.cut(df["pred"], bins=list(edges), right=False, include_lowest=True)
    out = []
    for interval, group in df.groupby(buckets, observed=True):
        hi = "+" if math.isinf(interval.right) else f"{interval.right:g}"
        out.append(
            {
                "bucket": f"{interval.left:g}-{hi}",
                "n": int(len(group)),
                "mean_pred": _clean(group["pred"].mean()),
                "mean_actual": _clean(group["actual"].mean()),
            }
        )
    # Negative predictions fall below the first edge; report them rather than drop.
    below = df[df["pred"] < edges[0]]
    if len(below):
        out.insert(
            0,
            {
                "bucket": f"<{edges[0]:g}",
                "n": int(len(below)),
                "mean_pred": _clean(below["pred"].mean()),
                "mean_actual": _clean(below["actual"].mean()),
            },
        )
    return out


# --- Bundles ---------------------------------------------------------------


def core_metrics(df: pd.DataFrame, picks: Mapping[int, int] = SQUAD_PICKS) -> dict[str, Any]:
    """The headline set for one model on one population."""
    out: dict[str, Any] = {
        "n": int(len(df)),
        "gameweeks": int(df[_GW_KEYS].drop_duplicates().shape[0]) if len(df) else 0,
        "mae": _clean(mae(df)),
        "rmse": _clean(rmse(df)),
        "bias": _clean(bias(df)),
        "mean_pred": _clean(df["pred"].mean()) if len(df) else None,
        "mean_actual": _clean(df["actual"].mean()) if len(df) else None,
        "spearman": _clean(mean_spearman(df)),
    }
    for k in TOP_KS:
        out[f"top{k}"] = _clean(topk_hit_rate(df, k))
    out["decision"] = decision_value(df, picks)
    return out


def breakdowns(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Core metrics by season, by position, by phase of season and by population."""
    out: dict[str, dict[str, Any]] = {"season": {}, "position": {}, "phase": {}, "population": {}}
    for season, group in df.groupby("season"):
        out["season"][str(season)] = core_metrics(group)
    for position, group in df.groupby("element_type"):
        name = POSITION_NAMES.get(int(position), str(position))
        out["position"][name] = core_metrics(group, {int(position): SQUAD_PICKS[int(position)]})
    early = df["gw"] <= EARLY_SEASON_LAST_GW
    out["phase"][f"gw1-{EARLY_SEASON_LAST_GW}"] = core_metrics(df[early])
    out["phase"][f"gw{EARLY_SEASON_LAST_GW + 1}+"] = core_metrics(df[~early])
    if "regular" in df:
        out["population"]["regulars"] = core_metrics(df[df["regular"]])
        out["population"]["non_regulars"] = core_metrics(df[~df["regular"]])
    return out


def evaluate_model(df: pd.DataFrame) -> dict[str, Any]:
    """Everything for one model on one population."""
    return {
        "overall": core_metrics(df),
        "breakdowns": breakdowns(df),
        "calibration": calibration(df),
    }


def oracle(df: pd.DataFrame) -> dict[str, Any]:
    """The ceiling: decision value if every pick were made knowing the result."""
    return decision_value(df, SQUAD_PICKS, by="actual")
