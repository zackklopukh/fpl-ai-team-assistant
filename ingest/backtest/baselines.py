"""Reference models every real model is measured against.

* `fpl_xp` — FPL's recorded projection for the target gameweek, as imported.
  Kept because it is what the plan named as the bar, but it is NOT a fair bar:
  the data shows it was captured after the gameweek and carries its outcome
  (see `data` module docstring). It reads `History` directly — the only model
  that does — and every report that includes it says so.
* `fpl_xp_lag` — FPL's recorded figure from the previous gameweek, per fixture,
  times this week's fixture count. Available before the deadline, read through
  `AsOf` like any real model: the honest version of "what FPL thought". It still
  carries FPL's availability knowledge, which ours lack in backtest (see
  `report.AVAILABILITY_CAVEAT`), and it is one week stale on fixture difficulty.
* `naive` — mean points over a player's last five appearances, times the number
  of fixtures he has this week. The floor: a model that cannot beat this is not
  modelling anything.
* `baseline` — the production `baseline-0.1` model from `ingest/xp`, run through
  the same code path the nightly job uses (`compute_xp.build_xp_rows`) with
  neutral availability.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from .data import AsOf

# --- FPL's projection ------------------------------------------------------


class FplXpModel:
    """FPL's recorded xP for the target gameweek — a recorded reference, not a model.

    Reads `History.recorded_fpl_xp` through the harness's reference path, because
    `AsOf` withholds every target-gameweek field. Predicts nothing where FPL
    recorded nothing (un-captured gameweeks, every API-synced row), so those rows
    show up as missing coverage rather than zeros.
    """

    name = "fpl_xp"

    def predict_from_history(self, history: Any, season: str, gw: int) -> dict[int, float]:
        projection = history.recorded_fpl_xp(season, gw)
        return {int(k): float(v) for k, v in projection.items()}

    def predict(self, asof: AsOf) -> dict[int, float]:
        raise TypeError("fpl_xp is a recorded reference; it has no AsOf prediction")


class FplXpLagModel:
    """FPL's recorded figure from the gameweek before the target, per fixture.

    Everything it reads is in `AsOf.player_history`: the player's rows from
    gameweek `target - 1` of the target season. Their summed `fpl_xp` is divided
    by that week's fixture count and multiplied by this week's, so a double after
    a single projects twice and a blank projects zero. Missing when the player
    had no row that week or FPL recorded nothing for it — never guessed from an
    older week, which would quietly mix staleness levels into the benchmark.
    """

    name = "fpl_xp_lag"

    def predict(self, asof: AsOf) -> dict[int, float]:
        h = asof.player_history
        prev = h[(h["season"] == asof.season) & (h["gw"] == asof.gw - 1)]
        per = prev.groupby("element_id").agg(
            xp=("fpl_xp", "sum"), recorded=("fpl_xp", "count"), fixtures=("fixture_id", "count")
        )
        per = per[per["recorded"] > 0]
        rate = per["xp"] / per["fixtures"]
        players = asof.players.set_index("element_id")
        out = {}
        for element_id, r in rate.items():
            if element_id in players.index:
                out[int(element_id)] = float(r * players.at[element_id, "fixture_count"])
        return out


# --- Naive recent form -----------------------------------------------------


class NaiveModel:
    """Mean points per appearance over the last `window` appearances.

    An appearance is a fixture row with minutes > 0, taken across seasons via the
    `code` link, so gameweek 1 uses the end of last season rather than nothing.
    Scaled by this week's fixture count: 0 for a blank, doubled for a double.
    A player who has never appeared is predicted 0.

    Deliberately ignores how often a player appears: a squad player who scored
    well in his one cameo projects like a starter. That weakness is the point of
    a naive baseline — it is what "just look at recent form" gets you.
    """

    def __init__(self, window: int = 5, name: str = "naive"):
        self.window = window
        self.name = name

    def predict(self, asof: AsOf) -> dict[int, float]:
        history = asof.player_history
        played = history[history["minutes"] > 0]
        per_appearance = (
            played.groupby("element_id", sort=False).tail(self.window).groupby("element_id")[
                "total_points"
            ].mean()
        )
        players = asof.players
        rate = players["element_id"].map(per_appearance).fillna(0.0)
        xp = rate * players["fixture_count"]
        return {int(e): float(v) for e, v in zip(players["element_id"], xp, strict=True)}


# --- baseline-0.1 ----------------------------------------------------------


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame rows as plain dicts, with every missing value as None.

    The xp code treats None as "no value" and would carry a NaN straight into a
    sum, so NaN and pd.NA must not reach it.
    """
    clean = df.astype(object).where(df.notna(), None)
    return clean.to_dict("records")


class BaselineModel:
    """`baseline-0.1`, run through AsOf with neutral availability.

    Uses `compute_xp.build_xp_rows` — the nightly job's own pure function — so
    what is evaluated is what ships, not a reimplementation of it. The inputs
    mirror production exactly, with two substitutions that the backtest forces:

    * `as_of_gw` is the gameweek before the target, and the stat rows are the
      target season's rows before it. baseline-0.1 is within-season only: it
      has no cross-season memory, so it is given none (its feature builder
      filters on `gw` alone and would mix seasons if handed older rows).
    * Availability is neutral — status 'a', no chance-of-playing — because no
      historical availability exists. In production this is the model's largest
      single input.

    The club is the club at the target (from AsOf), which is what production
    reads from `players.team_fpl_id` on the day.
    """

    def __init__(self, name: str = "baseline"):
        from xp.model import MODEL_VERSION

        self.name = name
        self.model_version = MODEL_VERSION

    def predict(self, asof: AsOf) -> dict[int, float]:
        from compute_xp import build_xp_rows

        players = asof.players[asof.players["team_fpl_id"].notna()]
        player_rows = [
            {
                "element_id": int(p.element_id),
                "element_type": int(p.element_type),
                "team_fpl_id": int(p.team_fpl_id),
                "status": "a",
                "chance_of_playing_next_round": None,
            }
            for p in players.itertuples(index=False)
        ]
        fixtures = asof.fixtures[asof.fixtures["season"] == asof.season]
        rows = build_xp_rows(
            season=asof.season,
            player_rows=player_rows,
            stat_rows=_records(asof.season_stats()),
            fixture_rows=_records(fixtures),
            target_gws=[asof.gw],
            as_of_gw=asof.as_of_gw,
        )
        return {int(r["element_id"]): float(r["xp"]) for r in rows}


# --- Registry --------------------------------------------------------------


def _lazy(module: str, attr: str) -> type:
    """Resolve a challenger model on first use.

    The fitted and boosted models live in their own modules and pull in heavier
    dependencies; importing them lazily keeps `--models naive baseline` fast and
    working even before those modules exist.
    """

    def factory(*args: Any, **kwargs: Any) -> Any:
        import importlib

        try:
            cls = getattr(importlib.import_module(module), attr)
        except (ImportError, AttributeError) as exc:
            raise SystemExit(f"model {attr} is not available: {exc}") from exc
        return cls(*args, **kwargs)

    return factory  # type: ignore[return-value]


def available_models() -> Mapping[str, type]:
    return {
        "fpl_xp": FplXpModel,
        "fpl_xp_lag": FplXpLagModel,
        "naive": NaiveModel,
        "baseline": BaselineModel,
        # The two challengers. The backtest decides which one ships.
        "fitted": _lazy("backtest.model_fitted", "FittedModel"),
        "gbm": _lazy("backtest.model_gbm", "GbmModel"),
    }


def build_models(names: list[str]) -> list[Any]:
    registry = available_models()
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise SystemExit(f"unknown model(s) {unknown}; choose from {sorted(registry)}")
    return [registry[n]() for n in names]
