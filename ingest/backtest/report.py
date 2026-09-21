"""Turn a backtest run into a comparison someone can make a shipping decision from.

Views, all over non-blank registered player-gameweeks, each scoring every model
in it on exactly the same rows:

* **Head-to-head vs fpl_xp_lag** — FPL's recorded figure from the week before,
  which is genuinely pre-deadline. The honest benchmark, and the one the report
  leads with.
* **Head-to-head vs fpl_xp (recorded)** — FPL's recorded figure for the target
  week itself. It contains that week's outcome (see `data` module docstring and
  the `fpl_xp_haul_jump_*` diagnostics), so it is an upper bound nobody could
  have had before the deadline, not a bar to beat. Shown, flagged, never led with.
* **Full season** — our models only, every gameweek evaluated. This is the view
  that picks between our own models.

Both FPL benchmarks exist only where the source captured `fpl_xp` (2025-26 has
it for barely a third of its gameweeks, API-synced 2026-27 rows never), so each
head-to-head states how many gameweeks it covers per season. Missing fpl_xp is
never read as 0: that would make FPL look terrible for the wrong reason. Each
model's results on its own coverage are also written to the JSON.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .harness import BacktestResult, model_summary
from .metrics import CALIBRATION_EDGES, evaluate_model, oracle

# Benchmarks, in the order the report shows them. The honest one leads.
BENCHMARKS = ("fpl_xp_lag", "fpl_xp")
LEAKY_BENCHMARK = "fpl_xp"

LEAKY_CAVEAT = (
    "fpl_xp (recorded) caveat: the recorded figure for a gameweek carries that "
    "gameweek's outcome — players who haul show a ~3-point jump in fpl_xp INTO the "
    "haul week and none after it (see fpl_xp_haul_jump_* in data diagnostics). It was "
    "captured after the gameweek, not before the deadline. Treat it as an unreachable "
    "ceiling, not a bar; compare against fpl_xp_lag."
)

AVAILABILITY_CAVEAT = (
    "Availability caveat: no historical injury or availability data exists, so every "
    "model here runs with neutral availability (everyone fit). FPL's recorded fpl_xp "
    "was computed WITH that week's injury news. The comparison is therefore biased "
    "against our models: a gap to fpl_xp is partly information we cannot have in "
    "backtest, not only modelling quality. In production our models do receive "
    "availability, so the live gap should be smaller than the backtest gap."
)


def _population(predictions: pd.DataFrame) -> pd.DataFrame:
    """Non-blank registered players — see harness.py for why blanks are out."""
    return predictions[~predictions["blank"]]


def common_frame(predictions: pd.DataFrame, models: Sequence[str]) -> pd.DataFrame:
    """Long frame for `models`, restricted to player-gameweeks every one predicted."""
    pop = _population(predictions)
    pop = pop[pop["model"].isin(models)]
    wide = pop.pivot_table(
        index=["season", "gw", "element_id"], columns="model", values="pred", aggfunc="first"
    )
    wide = wide.reindex(columns=list(models))
    keys = wide.dropna().index
    indexed = pop.set_index(["season", "gw", "element_id"])
    return indexed[indexed.index.isin(keys)].reset_index()


# Kept under its original name for callers that compare every requested model.
benchmark_frame = common_frame


def _view(predictions: pd.DataFrame, models: Sequence[str], population: str) -> dict[str, Any]:
    common = common_frame(predictions, models)
    gws = common[["season", "gw"]].drop_duplicates()
    view: dict[str, Any] = {
        "models": list(models),
        "population": population,
        "rows_per_model": int(len(common) // max(len(models), 1)),
        "gameweeks_by_season": {
            str(season): sorted(int(g) for g in group["gw"])
            for season, group in gws.groupby("season")
        },
        "results": {},
        "oracle": None,
    }
    if len(common):
        for name in models:
            view["results"][name] = evaluate_model(common[common["model"] == name])
        view["oracle"] = oracle(common[common["model"] == models[0]])
    return view


def build_results(
    result: BacktestResult,
    models: Sequence[str],
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    preds = result.predictions
    targets: dict[str, list[int]] = {}
    for t in result.targets:
        targets.setdefault(t.season, []).append(t.gw)

    views: dict[str, Any] = {}
    ours = [m for m in models if m not in BENCHMARKS]
    for bench in BENCHMARKS:
        if bench in models:
            views[f"vs_{bench}"] = _view(
                preds,
                [bench, *ours],
                f"non-blank registered player-gameweeks where {bench} exists "
                "(and every model predicted)",
            )
    if ours:
        views["full_season"] = _view(
            preds, ours, "every non-blank registered player-gameweek evaluated"
        )

    own: dict[str, Any] = {}
    pop = _population(preds)
    for name in models:
        rows = pop[(pop["model"] == name) & pop["pred"].notna()]
        own[name] = evaluate_model(rows) if len(rows) else None

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": list(models),
        "targets": targets,
        "refit_every": result.refit_every,
        "caveats": [
            AVAILABILITY_CAVEAT,
            *([LEAKY_CAVEAT] if LEAKY_BENCHMARK in models else []),
            *_data_caveats(diagnostics or {}),
        ],
        "runs": {name: model_summary(stats) for name, stats in result.stats.items()},
        "views": views,
        "own_coverage": own,
        "data_diagnostics": diagnostics or {},
    }


def _data_caveats(diagnostics: Mapping[str, Any]) -> list[str]:
    """Plain-language warnings derived from the data health counts."""
    out = []
    for season, d in diagnostics.items():
        if d.get("rows_fixture_gw_mismatch"):
            out.append(
                f"{season}: {d['rows_fixture_gw_mismatch']} stat rows sit in a different "
                "gameweek from their fixture's final one (postponements). The fixture "
                "list is the final schedule, so a match postponed after a deadline looks "
                "like a known blank in hindsight."
            )
        if d.get("rows_without_fixture"):
            out.append(
                f"{season}: {d['rows_without_fixture']} stat rows reference a fixture "
                "not in `fixtures`; their club at that gameweek is unknown."
            )
        if d.get("double_gw_fpl_xp_on_both_rows"):
            out.append(
                f"{season}: {d['double_gw_fpl_xp_on_both_rows']} double-gameweek players have "
                "fpl_xp on both fixture rows; the per-gameweek sum would double-count them."
            )
        into = d.get("fpl_xp_haul_jump_into_gw")
        after = d.get("fpl_xp_haul_jump_after_gw")
        if into is not None and after is not None and into > 1.0 and into > after:
            out.append(
                f"{season}: recorded fpl_xp jumps {into:+.2f} INTO a 10+ point haul week and "
                f"{after:+.2f} after it ({d['fpl_xp_haul_weeks']} hauls) — the recorded "
                "figure knows the outcome."
            )
        missing = d.get("fpl_xp_missing_gameweeks") or []
        if missing and d.get("fpl_xp_gameweeks"):
            out.append(
                f"{season}: fpl_xp exists for {d['fpl_xp_gameweeks']}/{d['gameweeks']} "
                f"gameweeks (missing {_ranges(missing)}); the head-to-head covers only those."
            )
    return out


def _ranges(gws: Sequence[int]) -> str:
    """[7, 10, 11, 12] -> "7, 10-12"."""
    runs: list[list[int]] = []
    for gw in sorted(gws):
        if runs and runs[-1][1] == gw - 1:
            runs[-1][1] = gw
        else:
            runs.append([gw, gw])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


# --- Terminal rendering ----------------------------------------------------


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [[_fmt(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = []
    for index, row in enumerate(cells):
        lines.append(
            "  ".join(
                cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
                for i, cell in enumerate(row)
            )
        )
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _headline_rows(
    results: Mapping[str, Any], models: Sequence[str], benchmark: str | None = None
) -> list[list[Any]]:
    base = results[benchmark]["overall"] if benchmark in results else None
    rows = []
    for name in models:
        m = results[name]["overall"]
        squad = m["decision"]["squad_points_per_gw"]
        delta = None
        if base is not None and name != benchmark and squad is not None:
            b = base["decision"]["squad_points_per_gw"]
            delta = None if b is None else squad - b
        rows.append(
            [
                name,
                m["n"],
                m["mae"],
                m["rmse"],
                m["bias"],
                m["spearman"],
                m["top10"],
                m["top20"],
                squad,
                delta,
            ]
        )
    return rows


HEADLINE = ["model", "n", "MAE", "RMSE", "bias", "spearman", "top10", "top20", "squad pts/gw", "vs bench"]


def _section(
    title: str, key: str, results: Mapping[str, Any], models: Sequence[str]
) -> str | None:
    groups: list[str] = []
    for name in models:
        for group in results[name]["breakdowns"][key]:
            if group not in groups:
                groups.append(group)
    if not groups:
        return None
    rows = []
    for group in groups:
        for name in models:
            m = results[name]["breakdowns"][key].get(group)
            if not m or not m["n"]:
                continue
            rows.append(
                [
                    f"{group} {name}",
                    m["n"],
                    m["mae"],
                    m["spearman"],
                    m["top10"],
                    m["top20"],
                    m["decision"]["squad_points_per_gw"]
                    if key != "position"
                    else next(iter(m["decision"]["per_pick_by_position"].values())),
                ]
            )
    last = "pts/pick" if key == "position" else "squad pts/gw"
    return f"{title}\n" + _table(["group / model", "n", "MAE", "spearman", "top10", "top20", last], rows)


def _calibration_table(results: Mapping[str, Any], models: Sequence[str]) -> str:
    by_model = {name: {c["bucket"]: c for c in results[name]["calibration"]} for name in models}
    edges = CALIBRATION_EDGES
    order = [f"<{edges[0]:g}"] + [
        f"{lo:g}-{'+' if math.isinf(hi) else f'{hi:g}'}" for lo, hi in zip(edges, edges[1:])
    ]
    buckets = [b for b in order if any(b in by_model[n] for n in models)]
    rows = []
    for b in buckets:
        row: list[Any] = [b]
        for name in models:
            c = by_model[name].get(b)
            row.append(f"{c['mean_actual']:.2f} ({c['n']})" if c else "-")
        rows.append(row)
    return (
        "Calibration: mean ACTUAL points (n) by predicted-xP bucket — a well-calibrated "
        "model's actual sits inside its bucket\n" + _table(["pred xP", *models], rows)
    )


def _render_view(title: str, view: Mapping[str, Any]) -> list[str]:
    models = view["models"]
    out: list[str] = []
    coverage = "; ".join(
        f"{season}: {len(gws)} gw ({_ranges(gws)})"
        for season, gws in view["gameweeks_by_season"].items()
    )
    out.append(f"{title} — {view['population']}")
    out.append(f"  gameweeks covered: {coverage or 'none'}")
    out.append(f"  {view['rows_per_model']} player-gameweeks per model")
    if not view["results"]:
        out.append("  (no rows)")
        return out
    bench = models[0] if models[0] in BENCHMARKS else None
    headers = HEADLINE if bench else HEADLINE[:-1]
    rows = _headline_rows(view["results"], models, bench)
    out.append(_table(headers, rows if bench else [r[:-1] for r in rows]))
    if view["oracle"]:
        out.append(
            f"oracle (perfect hindsight) squad pts/gw: "
            f"{_fmt(view['oracle']['squad_points_per_gw'])} — the ceiling for 'squad pts/gw'"
        )
    out.append("")
    for heading, key in (
        ("By season", "season"),
        ("Early season vs rest", "phase"),
        ("By position (pts/pick = mean actual of the model's top-N at that position)", "position"),
        ("Regulars (3+ starts in last 5 rows, known pre-deadline) vs the rest", "population"),
    ):
        section = _section(heading, key, view["results"], models)
        if section:
            out.append(section)
            out.append("")
    out.append(_calibration_table(view["results"], models))
    out.append("")
    return out


def render(results: Mapping[str, Any]) -> str:
    models = results["models"]
    out: list[str] = []
    targets = ", ".join(
        f"{s} gw{min(g)}-{max(g)} ({len(g)})" for s, g in results["targets"].items()
    )
    out.append(
        f"Backtest: {', '.join(models)}   targets: {targets}   "
        f"refit_every={results['refit_every']}"
    )
    out.append("")
    out.append(AVAILABILITY_CAVEAT)
    out.append("")

    views = results["views"]
    titles = {
        "vs_fpl_xp_lag": "HEAD-TO-HEAD vs fpl_xp_lag (FPL's previous-week figure; pre-deadline)",
        "vs_fpl_xp": "HEAD-TO-HEAD vs fpl_xp RECORDED (contains the outcome — a ceiling, not a bar)",
    }
    if not any(k in views for k in titles):
        out.append("HEAD-TO-HEAD: not run (no FPL benchmark among --models)")
        out.append("")
    for key, title in titles.items():
        if key in views:
            if key == "vs_fpl_xp":
                out.append(LEAKY_CAVEAT)
            out.extend(_render_view(title, views[key]))
    if "full_season" in views:
        out.extend(_render_view("FULL SEASON (our models, no FPL benchmark)", views["full_season"]))

    runs = results["runs"]
    out.append(
        _table(
            ["run", "coverage", "missing", "blank violations", "fits", "predict s"],
            [
                [
                    n,
                    r["coverage"],
                    r["missing"],
                    r["blank_violations"],
                    r["fits"],
                    r["predict_seconds"],
                ]
                for n, r in runs.items()
            ],
        )
    )
    data_caveats = [c for c in results["caveats"] if c not in (AVAILABILITY_CAVEAT, LEAKY_CAVEAT)]
    if data_caveats:
        out.append("")
        out.append("Data caveats:")
        out.extend(f"  - {c}" for c in data_caveats)
    return "\n".join(out)


def write_json(results: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, default=str))
    return path
