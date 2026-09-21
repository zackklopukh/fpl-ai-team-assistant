"""Run the walk-forward backtest from the command line.

    .venv/bin/python ingest/backtest/run_backtest.py --models fpl_xp_lag fpl_xp naive baseline

Reads Postgres once (read-only: four selects), then everything runs in memory.
Prints the comparison table and writes the full results as JSON to `data/raw/`,
which is git-ignored — results depend on the data as imported and are not
something to commit.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.baselines import available_models, build_models  # noqa: E402
from backtest.data import History, load_tables  # noqa: E402
from backtest.harness import DEFAULT_FIRST_GW, DEFAULT_SEASONS, default_targets, run_backtest  # noqa: E402
from backtest.report import build_results, render, write_json  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "raw" / "backtest"

log = logging.getLogger("backtest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--models",
        nargs="+",
        default=["fpl_xp_lag", "fpl_xp", "naive", "baseline"],
        help=f"models to evaluate, from {sorted(available_models())}",
    )
    p.add_argument(
        "--seasons",
        nargs="+",
        default=list(DEFAULT_SEASONS),
        help="seasons to evaluate (history from earlier seasons is always loaded)",
    )
    p.add_argument(
        "--first-gw",
        nargs="*",
        default=[f"{s}={g}" for s, g in DEFAULT_FIRST_GW.items()],
        metavar="SEASON=GW",
        help="first gameweek to evaluate per season (default: 2026-27=2)",
    )
    p.add_argument("--max-gw", type=int, default=None, help="stop each season at this gameweek")
    p.add_argument(
        "--refit-every",
        type=int,
        default=1,
        help="target gameweeks between fit() calls for models that have one; 0 = once per season",
    )
    p.add_argument("--out", type=Path, default=None, help="JSON results path")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    models = build_models(args.models)
    first_gw = {}
    for item in args.first_gw or []:
        season, _, gw = item.partition("=")
        first_gw[season] = int(gw)

    import db  # imported late: tests exercise everything above without a database

    started = time.perf_counter()
    with db.connect() as conn:
        # Everything up to the last evaluated season, so earlier seasons serve as
        # history. Later seasons are never loaded at all.
        conn.read_only = True
        tables = load_tables(conn)
    last = max(args.seasons)
    tables = type(tables)(
        stats=tables.stats[tables.stats["season"] <= last],
        players=tables.players[tables.players["season"] <= last],
        fixtures=tables.fixtures[tables.fixtures["season"] <= last],
        teams=tables.teams[tables.teams["season"] <= last],
    )
    history = History(tables)
    log.info(
        "loaded %d stat rows across %s in %.1fs",
        len(history.stats),
        ", ".join(history.seasons),
        time.perf_counter() - started,
    )

    targets = default_targets(history, args.seasons, first_gw)
    if args.max_gw is not None:
        targets = [t for t in targets if t.gw <= args.max_gw]
    missing = [s for s in args.seasons if s not in history.seasons]
    if missing:
        log.warning("no stat rows for season(s) %s — skipped", ", ".join(missing))
    if not targets:
        raise SystemExit("no target gameweeks to evaluate")

    result = run_backtest(
        history,
        models,
        targets,
        refit_every=args.refit_every or None,
        progress=lambda t: log.info("predicting %s gw%d", t.season, t.gw)
        if t.gw % 10 == 1 or t == targets[0]
        else None,
    )
    results = build_results(result, [m.name for m in models], history.diagnostics())
    print()
    print(render(results))

    out = args.out or DEFAULT_OUT_DIR / f"backtest-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    write_json(results, out)
    print(f"\nresults written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
