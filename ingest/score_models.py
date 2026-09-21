"""Grade every xP model on this season's gameweeks, as they finish.

The one-season backtest could not separate the fitted, boosted and baseline
models (2026-09-21: every 95% interval on squad points and ranking crossed
zero). The honest way to settle it is out-of-sample evidence nobody tuned on,
and this season supplies a fresh gameweek every week. compute_xp.py predicts
every model in compute_xp.MODELS nightly; this grades them.

What makes the grade fair:

* **Frozen predictions only.** A model is graded on the row it had written
  before the gameweek's deadline — `xpoints.computed_at < deadline_time`. A row
  written afterwards could have seen team news, or the result, and is reported
  as late and excluded rather than trusted. compute_xp.plan_gameweeks stops
  rewriting a gameweek at its deadline, so late rows should not exist.
* **The same players for every model.** Grading is over the intersection of
  what every model predicted, so a model cannot look better by skipping
  awkward players.
* **Blanks are out; non-appearances are in.** A player whose club has no
  fixture is excluded, as in the backtest. A registered player whose club did
  play but who did not is graded with 0 points — dropping him would mean
  filtering on the result.
* **Paired uncertainty.** Each shadow model's per-gameweek difference from the
  live model is bootstrapped, so "ahead" means ahead by more than noise.

Read-only. Metrics are the backtest's own (backtest/metrics.py), so a number
here means exactly what it means there.

    python ingest/score_models.py
    python ingest/score_models.py --season 2026-27 --from-gw 6
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

import db
from backtest.metrics import SQUAD_PICKS, decision_value, mae, spearman
from config import LIVE_MODEL_VERSION, SEASON

log = logging.getLogger(__name__)

BOOTSTRAP_SAMPLES = 20_000


# --- Pure grading ------------------------------------------------------------


def graded_frame(
    xp: pd.DataFrame,
    stats: pd.DataFrame,
    players: pd.DataFrame,
    fixtures: pd.DataFrame,
    deadlines: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (graded, late).

    `graded` has one row per (model_version, gw, element_id) with `pred`,
    `actual`, `element_type` — only frozen predictions, only non-blank players,
    only players every model predicted for that gameweek. `late` lists the
    (model_version, gw) predictions excluded for being written after the
    deadline.

    Inputs: `xp` (season, element_id, gw, model_version, xp, computed_at),
    `stats` (element_id, gw, total_points), `players` (element_id,
    element_type, team_fpl_id), `fixtures` (gw, team_h_fpl_id,
    team_a_fpl_id), `deadlines` (gw, deadline_time, finished).
    """
    finished = deadlines[deadlines["finished"]][["gw", "deadline_time"]]
    xp = xp.merge(finished, on="gw", how="inner")

    on_time = xp["computed_at"] < xp["deadline_time"]
    late = (
        xp[~on_time][["model_version", "gw"]]
        .drop_duplicates()
        .sort_values(["gw", "model_version"])
        .reset_index(drop=True)
    )
    xp = xp[on_time]

    # A club's fixture count per gameweek: 0 is a blank. Counted per fixture,
    # never "the fixture", so a double is two and a blank is absent.
    sides = pd.concat(
        [
            fixtures[["gw", "team_h_fpl_id"]].rename(columns={"team_h_fpl_id": "team_fpl_id"}),
            fixtures[["gw", "team_a_fpl_id"]].rename(columns={"team_a_fpl_id": "team_fpl_id"}),
        ]
    )
    playing = sides.drop_duplicates()

    actual = stats.groupby(["element_id", "gw"], as_index=False)["total_points"].sum()

    frame = (
        xp[["model_version", "element_id", "gw", "xp"]]
        .rename(columns={"xp": "pred"})
        .merge(players[["element_id", "element_type", "team_fpl_id"]], on="element_id")
        .merge(playing, on=["gw", "team_fpl_id"], how="inner")  # drops blanks
        .merge(actual, on=["element_id", "gw"], how="left")
    )
    frame["actual"] = frame["total_points"].fillna(0).astype(float)
    frame["pred"] = frame["pred"].astype(float)

    # Common population: only (gw, element) pairs every graded model predicted.
    models_per_gw = frame.groupby("gw")["model_version"].nunique()
    counts = frame.groupby(["gw", "element_id"])["model_version"].nunique().rename("n_models")
    frame = frame.join(counts, on=["gw", "element_id"])
    frame = frame[frame["n_models"] == frame["gw"].map(models_per_gw)]

    frame["season"] = "graded"  # metrics group by (season, gw)
    cols = ["season", "gw", "model_version", "element_id", "element_type", "pred", "actual"]
    return frame[cols].reset_index(drop=True), late


def weekly_scores(graded: pd.DataFrame) -> pd.DataFrame:
    """Per gameweek per model: squad points, ranking and error."""
    rows = []
    for (gw, model), week in graded.groupby(["gw", "model_version"]):
        rows.append(
            {
                "gw": gw,
                "model_version": model,
                "squad_pts": decision_value(week, SQUAD_PICKS)["squad_points_per_gw"],
                "spearman": spearman(week["pred"], week["actual"]),
                "mae": mae(week),
                "n": len(week),
            }
        )
    return pd.DataFrame(rows)


def paired_vs_live(
    weekly: pd.DataFrame, live: str, metric: str, seed: int = 20260921
) -> pd.DataFrame:
    """Each shadow model minus the live model, per gameweek, bootstrapped."""
    wide = weekly.pivot(index="gw", columns="model_version", values=metric).dropna()
    rng = np.random.default_rng(seed)
    out = []
    for model in wide.columns:
        if model == live or live not in wide.columns:
            continue
        diff = (wide[model] - wide[live]).to_numpy()
        if len(diff) < 2:
            lo = hi = float("nan")
        else:
            boots = rng.choice(diff, size=(BOOTSTRAP_SAMPLES, len(diff))).mean(axis=1)
            lo, hi = np.percentile(boots, [2.5, 97.5])
        out.append(
            {
                "model_version": model,
                "gameweeks": len(diff),
                "mean_diff": float(diff.mean()),
                "ci_low": float(lo),
                "ci_high": float(hi),
                "wins": int((diff > 0).sum()),
            }
        )
    return pd.DataFrame(out)


# --- I/O ---------------------------------------------------------------------


def load(conn: Any, season: str, from_gw: int) -> dict[str, pd.DataFrame]:
    def frame(sql: str, params: Sequence[Any]) -> pd.DataFrame:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

    return {
        "xp": frame(
            "select element_id, gw, model_version, xp, computed_at from xpoints "
            "where season = %s and gw >= %s",
            (season, from_gw),
        ),
        "stats": frame(
            "select element_id, gw, total_points from player_gw_stats "
            "where season = %s and gw >= %s",
            (season, from_gw),
        ),
        "players": frame(
            "select element_id, element_type, team_fpl_id from players where season = %s",
            (season,),
        ),
        "fixtures": frame(
            "select gw, team_h_fpl_id, team_a_fpl_id from fixtures "
            "where season = %s and gw >= %s",
            (season, from_gw),
        ),
        "deadlines": frame(
            "select gw, deadline_time, finished from gameweeks "
            "where season = %s and gw >= %s",
            (season, from_gw),
        ),
    }


def render(weekly: pd.DataFrame, late: pd.DataFrame, live: str) -> str:
    if weekly.empty:
        return (
            "No finished gameweek has a frozen prediction yet. The first "
            "gameweek every model predicted before its deadline is GW6 "
            "(deadline 2026-10-10)."
        )

    lines = []
    totals = (
        weekly.groupby("model_version")
        .agg(
            gameweeks=("gw", "nunique"),
            squad_pts=("squad_pts", "mean"),
            spearman=("spearman", "mean"),
            mae=("mae", "mean"),
        )
        .sort_values("squad_pts", ascending=False)
    )
    gws = sorted(weekly["gw"].unique())
    lines.append(f"Graded gameweeks: {', '.join(f'GW{g}' for g in gws)}   live model: {live}")
    lines.append("")
    lines.append(f"{'model':<14}{'gws':>5}{'squad pts/gw':>14}{'spearman':>10}{'MAE':>8}")
    for model, r in totals.iterrows():
        marker = "  <- live" if model == live else ""
        lines.append(
            f"{model:<14}{int(r.gameweeks):>5}{r.squad_pts:>14.2f}{r.spearman:>10.3f}{r.mae:>8.3f}{marker}"
        )

    for metric, label in (("squad_pts", "squad pts/gw"), ("spearman", "ranking")):
        paired = paired_vs_live(weekly, live, metric)
        if paired.empty:
            continue
        lines.append("")
        lines.append(f"vs {live}, {label} (per-gameweek difference, 95% bootstrap interval):")
        for r in paired.itertuples():
            verdict = (
                "ahead" if r.ci_low > 0 else "behind" if r.ci_high < 0 else "not distinguishable"
            )
            fmt = "+.2f" if metric == "squad_pts" else "+.4f"
            lines.append(
                f"  {r.model_version:<14} {r.mean_diff:{fmt}}  "
                f"[{r.ci_low:{fmt}}, {r.ci_high:{fmt}}]  wins {r.wins}/{r.gameweeks}  -> {verdict}"
            )

    if len(gws) < 10:
        lines.append("")
        lines.append(
            f"Only {len(gws)} graded gameweek(s): expect every interval to be wide. "
            "The decision is revisited around GW15."
        )
    if not late.empty:
        lines.append("")
        lines.append(
            "Excluded as written after the deadline: "
            + ", ".join(f"{r.model_version} GW{r.gw}" for r in late.itertuples())
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", default=SEASON)
    parser.add_argument("--from-gw", type=int, default=1)
    parser.add_argument("--live", default=LIVE_MODEL_VERSION)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    with db.connect() as conn:
        data = load(conn, args.season, args.from_gw)

    graded, late = graded_frame(**data)
    print(render(weekly_scores(graded), late, args.live))
    return 0


if __name__ == "__main__":
    sys.exit(main())
