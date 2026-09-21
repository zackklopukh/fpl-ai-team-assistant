"""One entry point for every ingestion job.

`python ingest/run.py <job>` runs the same code the GitHub Actions workflows run,
so a job debugged locally is the job that runs in production. The per-script
entry points still work and the workflows still reference them by path — this is
a front door, not a replacement.

`--help` lists every job with what it writes and when it runs in production,
because the schedule is the part nobody remembers and it is currently only
written down in ARCHITECTURE.md and `.github/README.md`.

Each job is imported inside its own runner rather than at module level. A missing
DATABASE_URL should not stop `--help` from printing, and listing the jobs should
not open a database connection.

    python ingest/run.py --help
    python ingest/run.py verify
    python ingest/run.py prices --dry-run
    python ingest/run.py backfill --from-element 412
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


@dataclass
class Job:
    """A runnable job: what it is, when it runs, and how to invoke it."""

    name: str
    summary: str
    schedule: str
    writes: str
    call: Callable[[argparse.Namespace], int]
    add_arguments: Callable[[argparse.ArgumentParser], None] | None = None

    def help_line(self) -> str:
        return f"{self.summary} Runs {self.schedule}. Writes {self.writes}."


# --- Runners ---------------------------------------------------------------
#
# Each one returns a process exit code. Several of the underlying jobs return a
# row count from `main()`; returning that as an exit status would turn a
# successful run that wrote 300 rows into exit code 300, so row counts are
# logged by the jobs themselves and discarded here.


def _bootstrap(args: argparse.Namespace) -> int:
    import sync_bootstrap

    return sync_bootstrap.main() or 0


def _fixtures(args: argparse.Namespace) -> int:
    import sync_fixtures

    return sync_fixtures.main() or 0


def _live(args: argparse.Namespace) -> int:
    import sync_live

    return sync_live.main(args.gameweek) or 0


def _prices(args: argparse.Namespace) -> int:
    import datetime as dt
    import logging

    import snapshot_prices

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    as_of = dt.date.fromisoformat(args.date) if args.date else snapshot_prices.today_utc()
    snapshot_prices.run(args.season, as_of, args.dry_run)
    return 0


def _xp(args: argparse.Namespace) -> int:
    import compute_xp

    compute_xp.main(args.season)
    return 0


def _settle_bonus(args: argparse.Namespace) -> int:
    import settle_bonus

    argv = ["--season", args.season]
    if args.gameweek is not None:
        argv += ["--gameweek", str(args.gameweek)]
    if args.dry_run:
        argv.append("--dry-run")
    return settle_bonus.main(argv)


def _backfill(args: argparse.Namespace) -> int:
    import logging

    import backfill_history

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    backfill_history.run(args.season, args.from_element, args.limit, args.dry_run)
    return 0


def _verify(args: argparse.Namespace) -> int:
    import verify_db

    return verify_db.main(["--season", args.season])


def _score(args: argparse.Namespace) -> int:
    import score_models

    return score_models.main(["--season", args.season])


def _publish_xp(args: argparse.Namespace) -> int:
    import publish_xp

    argv = ["--season", args.season]
    if args.out:
        argv += ["--out", args.out]
    return publish_xp.main(argv)


def _import_history(args: argparse.Namespace) -> int:
    import import_history

    argv = ["--seasons", *args.seasons] if args.seasons else []
    if args.dry_run:
        argv.append("--dry-run")
    return import_history.main(argv)


def _season(args: argparse.Namespace) -> int:
    import season

    argv = ["--season", args.season]
    if args.exit_code:
        argv.append("--exit-code")
    return season.main(argv)


# --- Per-job arguments -----------------------------------------------------


def _season_arg(parser: argparse.ArgumentParser) -> None:
    import config

    parser.add_argument("--season", default=config.SEASON)


def _live_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gameweek",
        type=int,
        default=None,
        help="gameweek to sync; defaults to the one FPL marks as current",
    )


def _prices_args(parser: argparse.ArgumentParser) -> None:
    _season_arg(parser)
    parser.add_argument(
        "--date", default=None, help="ISO date to stamp the snapshot with (UTC today)"
    )
    parser.add_argument("--dry-run", action="store_true")


def _settle_args(parser: argparse.ArgumentParser) -> None:
    _season_arg(parser)
    parser.add_argument("--gameweek", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")


def _backfill_args(parser: argparse.ArgumentParser) -> None:
    _season_arg(parser)
    parser.add_argument(
        "--from-element",
        type=int,
        default=0,
        help="resume at this element id; the failure log tells you which",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N players")
    parser.add_argument("--dry-run", action="store_true")


def _season_job_args(parser: argparse.ArgumentParser) -> None:
    _season_arg(parser)
    parser.add_argument(
        "--exit-code", action="store_true", help="exit 1 when off-season"
    )


def _publish_args(parser: argparse.ArgumentParser) -> None:
    _season_arg(parser)
    parser.add_argument("--out", default=None, help="artifact path (default: git-ignored data/)")


def _history_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seasons", nargs="*", default=None, help="default: 2023-24 to 2025-26")
    parser.add_argument("--dry-run", action="store_true")


JOBS: dict[str, Job] = {
    job.name: job
    for job in [
        Job(
            name="bootstrap",
            summary="Sync bootstrap-static into players, teams and gameweeks.",
            schedule="every 2 hours in season",
            writes="players, teams, gameweeks — prices, status, injury news",
            call=_bootstrap,
            add_arguments=None,
        ),
        Job(
            name="fixtures",
            summary="Sync the whole season's fixture list.",
            schedule="daily at 04:00 UTC",
            writes="fixtures — FDR, kickoff times, results",
            call=_fixtures,
            add_arguments=None,
        ),
        Job(
            name="live",
            summary="Pull provisional per-player stats for a gameweek in progress.",
            schedule="every 15 minutes during match windows",
            writes="player_gw_stats — bonus provisional until settled",
            call=_live,
            add_arguments=_live_args,
        ),
        Job(
            name="prices",
            summary="Snapshot today's prices and ownership.",
            schedule="daily at 01:45 UTC, after FPL's ~01:30 price changes",
            writes="price_history — one row per player per day",
            call=_prices,
            add_arguments=_prices_args,
        ),
        Job(
            name="xp",
            summary="Recompute expected points for the next five gameweeks.",
            schedule="nightly at 05:00 UTC",
            writes="xpoints — keyed by model_version, never overwritten",
            call=_xp,
            add_arguments=_season_arg,
        ),
        Job(
            name="settle-bonus",
            summary="Rewrite a gameweek's stats once bonus points are final.",
            schedule="roughly 2 hours after the last match of a gameweek",
            writes="player_gw_stats — final bonus, bonus_settled = true",
            call=_settle_bonus,
            add_arguments=_settle_args,
        ),
        Job(
            name="backfill",
            summary=(
                "Walk every player's element-summary for this season's history. "
                "Slow by design (~659 serialised requests) and resumable."
            ),
            schedule="manually, and once on a fresh database",
            writes="player_gw_stats — historical rows with per-gameweek prices",
            call=_backfill,
            add_arguments=_backfill_args,
        ),
        Job(
            name="score",
            summary=(
                "Grade every xP model on this season's finished gameweeks, "
                "using only the prediction each had frozen at the deadline."
            ),
            schedule="nightly after xp, and by hand when deciding the live model",
            writes="nothing — read only",
            call=_score,
            add_arguments=_season_arg,
        ),
        Job(
            name="publish-xp",
            summary="Write the live model's xP artifact for the optimizer.",
            schedule="after xp, once artifact delivery is decided",
            writes="a JSON file — never the database",
            call=_publish_xp,
            add_arguments=_publish_args,
        ),
        Job(
            name="import-history",
            summary="Load past seasons from the public vaastav dataset.",
            schedule="once, and when a season ends",
            writes="teams, players, fixtures, player_gw_stats — source='history'",
            call=_import_history,
            add_arguments=_history_args,
        ),
        Job(
            name="verify",
            summary="Check the database against db/schema.sql and report drift.",
            schedule="by hand, first thing after credentials land",
            writes="nothing — read only",
            call=_verify,
            add_arguments=_season_arg,
        ),
        Job(
            name="season",
            summary="Report season state and the next deadline.",
            schedule="by hand, or as a guard in front of the frequent jobs",
            writes="nothing — read only",
            call=_season,
            add_arguments=_season_job_args,
        ),
    ]
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python ingest/run.py",
        description=(
            "Run an ingestion job. Cron is the only writer to Postgres; these "
            "are the jobs that do the writing."
        ),
        epilog=(
            "Every write is an upsert on the natural key, so re-running a job "
            "after a failure is safe. See ingest/README.md for the bring-up "
            "order on a fresh database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="job", metavar="job", required=True)

    for job in JOBS.values():
        subparser = subparsers.add_parser(
            job.name,
            help=job.help_line(),
            description=job.help_line(),
        )
        if job.add_arguments:
            job.add_arguments(subparser)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    job = JOBS.get(args.job)
    if job is None:  # argparse rejects unknown names before this
        parser.error(f"unknown job {args.job!r}")

    import psycopg

    try:
        return job.call(args) or 0
    except psycopg.OperationalError as exc:
        # The first-run failure, and the one where a stack trace tells the
        # reader nothing they can act on. `verify` is the script that explains
        # it properly, so point at that rather than reprinting its advice here.
        print(f"\nCould not connect to the database: {str(exc).strip()}", file=sys.stderr)
        print(
            "Run `python ingest/run.py verify` for what to check.", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
