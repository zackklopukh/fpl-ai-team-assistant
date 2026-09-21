"""Permutation importance for `gbm-0.1`, walked forward like the backtest.

    .venv/bin/python ingest/xp/gbm_explain.py --season 2024-25

A boosted model has to earn trust the fitted model gets for free, so this says
which inputs the predictions actually lean on. It replays the walk-forward run
for one season (fitting only on each target's `AsOf`), then for each feature
family shuffles that family's columns jointly across players within each
gameweek and measures how much worse the season's predictions get. Families
are shuffled together because the windows are heavily correlated: shuffling
`pts_r5` alone while `pts_r10` survives mostly measures redundancy.

It reads actual points from `History`, so it is an evaluation tool, not model
code — the model never sees them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import metrics  # noqa: E402
from xp.gbm import FEATURE_GROUPS, GbmConfig, GbmXpModel  # noqa: E402


def walk(history: Any, season: str, config: GbmConfig | None = None, refit_every: int = 4,
         first_gw: int = 1) -> list[tuple[GbmXpModel, pd.DataFrame, pd.DataFrame]]:
    """(fitted model, target feature rows, actuals) per gameweek of `season`."""
    out = []
    model = None
    last = None
    for gw in history.gameweeks(season):
        if gw < first_gw:
            continue
        asof = history.asof(season, gw)
        if model is None or gw - last >= refit_every:
            model = GbmXpModel(config).fit(asof)
            last = gw
        frame = model.frame(asof)
        rows = frame[frame["is_target"]].copy()
        actual = history.actuals(season, gw)
        out.append((model, rows, actual))
    return out


def _score(parts: list[tuple[GbmXpModel, pd.DataFrame, pd.DataFrame]]) -> dict[str, float]:
    frames = []
    for model, rows, actual in parts:
        xp = model._predict_components(rows)["xp"].to_numpy()
        if model.calibrator is not None:
            xp = model.calibrator.predict(xp)
        per = pd.Series(xp, index=rows["element_id"].astype("int64")).groupby(level=0).sum()
        a = actual[actual["fixture_count"] > 0].copy()
        a["pred"] = a["element_id"].map(per).fillna(0.0)
        frames.append(a.assign(season="s", gw=len(frames)))
    df = pd.concat(frames, ignore_index=True)
    return {
        "mae": metrics.mae(df),
        "spearman": metrics.mean_spearman(df),
        "squad": metrics.decision_value(df)["squad_points_per_gw"],
    }


def permutation_importance(parts, groups=FEATURE_GROUPS, repeats: int = 3, seed: int = 0) -> pd.DataFrame:
    """Worsening of MAE / Spearman / squad points when each family is shuffled."""
    rng = np.random.default_rng(seed)
    base = _score(parts)
    rows = []
    for name, prefixes in groups.items():
        deltas = []
        for _ in range(repeats):
            shuffled = []
            for model, frame, actual in parts:
                cols = [f for f in model.features if any(f == p or f.startswith(p) for p in prefixes)]
                f = frame.copy()
                if cols:
                    order = rng.permutation(len(f))
                    f[cols] = f[cols].to_numpy()[order]
                shuffled.append((model, f, actual))
            s = _score(shuffled)
            deltas.append((s["mae"] - base["mae"], base["spearman"] - s["spearman"], base["squad"] - s["squad"]))
        d = np.mean(deltas, axis=0)
        rows.append({"family": name, "mae_increase": d[0], "spearman_drop": d[1], "squad_pts_drop": d[2]})
    out = pd.DataFrame(rows).sort_values("spearman_drop", ascending=False).reset_index(drop=True)
    out.attrs["base"] = base
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", default="2024-25")
    p.add_argument("--refit-every", type=int, default=4)
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args(argv)

    import db
    from backtest.data import History, Tables, load_tables

    with db.connect() as conn:
        conn.read_only = True
        t = load_tables(conn)
    keep = lambda df: df[df["season"] <= args.season]  # noqa: E731 — never load later seasons
    history = History(Tables(keep(t.stats), keep(t.players), keep(t.fixtures), keep(t.teams)))
    parts = walk(history, args.season, refit_every=args.refit_every)
    table = permutation_importance(parts, repeats=args.repeats)
    print("unshuffled:", {k: round(v, 3) for k, v in table.attrs["base"].items()})
    print(table.round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
