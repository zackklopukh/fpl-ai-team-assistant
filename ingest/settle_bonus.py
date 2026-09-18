"""Rewrite a gameweek's stats once its bonus points are final.

The scheduled job from ARCHITECTURE.md's ingestion table: "~2h after last match,
rewrite that gameweek's stats once bonus points are final."

Everything `sync_live.py` writes while matches are on is provisional. FPL derives
bonus from BPS as a match runs, publishes it live, and only fixes it some hours
later once the data is checked — a BPS correction after full time moves the three
bonus points to a different player, and the rows written during the match still
say otherwise. `player_gw_stats.bonus_settled` is the flag for which of the two a
row is, and `gameweeks.data_checked` (`bootstrap-static`'s `events[].data_checked`)
is FPL telling us the gameweek is now final.

So the job is: find gameweeks with unsettled rows, ask bootstrap which of them
FPL now considers checked, and re-pull and rewrite exactly those.

Three properties worth stating, because each one is a way this could go wrong:

* **A settled gameweek costs nothing.** The first thing this does is a single
  query against `player_gw_stats`. If nothing is unsettled it returns without
  making an HTTP request at all — this runs on a schedule, and the common case is
  that there is nothing to do.
* **The row transform is imported, not reimplemented.** `sync_live.build_stat_rows`
  is the one mapping from a live payload to a stat row. A second copy here would
  drift, and the first symptom of the drift would be the bonus column disagreeing
  with the points column for one gameweek in March.
* **The per-gameweek snapshot columns are not overwritten.** `value_tenths` and
  `selected_by` are the player's price and ownership *during that gameweek*.
  `snapshot_context` reads them from today's bootstrap, which is right when a
  gameweek settles two hours after the whistle and badly wrong when an old
  gameweek is settled late. They are written on insert and left alone on update,
  so a settle can never backdate today's price into a past row — the lookahead
  leak CLAUDE.md warns about.

Re-running is safe: every write is an upsert on (season, element_id, gw,
fixture_id), and a gameweek already marked settled is simply not selected again.

    python ingest/settle_bonus.py --dry-run
    python ingest/run.py settle-bonus
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import db  # noqa: E402
from backfill_history import settled_gameweeks  # noqa: E402
from fpl_client import FPLClient  # noqa: E402
from sync_live import build_stat_rows, fixture_sides, snapshot_context  # noqa: E402

log = logging.getLogger(__name__)

JOB = "settle_bonus"

CONFLICT_KEYS = ("season", "element_id", "gw", "fixture_id")

# Written when the row is first inserted, never rewritten by a settle. See the
# module docstring: these describe the gameweek, not today.
PRESERVED_ON_SETTLE = ("value_tenths", "selected_by")


# --- Pure ------------------------------------------------------------------


def gameweeks_to_settle(
    unsettled: Sequence[int], checked: Sequence[int] | frozenset[int]
) -> list[int]:
    """Which unsettled gameweeks FPL now reports as final, oldest first.

    The intersection and nothing more. A gameweek with provisional rows that FPL
    has not checked yet is left alone — rewriting it would produce the same
    provisional numbers and burn two requests doing it.
    """
    checked_set = set(checked)
    return sorted(gw for gw in set(unsettled) if gw in checked_set)


def settle_update_columns(row: dict[str, Any]) -> list[str]:
    """Columns a settle is allowed to overwrite on an existing row.

    Derived from the row the transform actually produced rather than listed by
    hand, so a column added to `sync_live.build_stat_rows` is carried here
    automatically instead of being silently dropped from the update.
    """
    return [
        column
        for column in row
        if column not in CONFLICT_KEYS and column not in PRESERVED_ON_SETTLE
    ]


# --- Database edges --------------------------------------------------------


def unsettled_gameweeks(conn, season: str) -> list[tuple[int, int]]:
    """(gameweek, row count) for every gameweek holding provisional rows.

    The cheap check that lets the common case — nothing to do — cost one query
    and no HTTP at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select gw, count(*) from player_gw_stats "
            "where season = %s and bonus_settled = false "
            "group by gw order by gw",
            (season,),
        )
        return [(int(row[0]), int(row[1])) for row in cur.fetchall()]


def settle_gameweek(client, conn, season: str, gw: int, context: dict) -> int:
    """Re-pull one gameweek's live payload and rewrite its rows as final."""
    fixtures = client.fixtures(event=gw)
    live = client.event_live(gw)

    rows = build_stat_rows(
        live,
        gw=gw,
        sides=fixture_sides(fixtures),
        context=context,
        data_checked=True,
        season=season,
    )

    if not rows:
        # The gameweek is checked but the live payload has no per-fixture
        # detail for anyone. Nothing to write, and nothing that a retry fixes.
        log.warning("gw %d: live payload produced no rows", gw)
        return 0

    written = db.upsert(
        conn,
        "player_gw_stats",
        rows,
        CONFLICT_KEYS,
        update_columns=settle_update_columns(rows[0]),
    )
    # Commit per gameweek: settling three at once should not lose the first two
    # because the third failed.
    conn.commit()

    log.info("gw %d: settled %d rows", gw, written)
    return written


def run(
    client,
    conn,
    season: str,
    *,
    only_gw: int | None = None,
    dry_run: bool = False,
) -> int:
    """Settle every gameweek that is ready. Returns rows written."""
    pending = unsettled_gameweeks(conn, season)

    if not pending:
        log.info("%s: no provisional rows for %s — nothing to settle", JOB, season)
        return 0

    log.info(
        "provisional rows in %s",
        ", ".join(f"gw {gw} ({count})" for gw, count in pending),
    )

    # Only now is an HTTP request justified.
    bootstrap = client.bootstrap_static()
    checked = settled_gameweeks(bootstrap)

    targets = gameweeks_to_settle([gw for gw, _ in pending], checked)
    if only_gw is not None:
        targets = [gw for gw in targets if gw == only_gw]
        if not targets:
            log.warning(
                "gw %d is not both unsettled and data_checked — nothing to do",
                only_gw,
            )
            return 0

    if not targets:
        log.info(
            "%s: bonus is still provisional for %s — FPL has not checked them yet",
            JOB,
            ", ".join(f"gw {gw}" for gw, _ in pending),
        )
        return 0

    if dry_run:
        log.info(
            "dry run: would rewrite %s", ", ".join(f"gw {gw}" for gw in targets)
        )
        return 0

    context = snapshot_context(bootstrap)

    with db.run_logged(conn, JOB, season) as result:
        for gw in targets:
            result["rows"] += settle_gameweek(client, conn, season, gw, context)
        return result["rows"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", default=config.SEASON)
    parser.add_argument(
        "--gameweek",
        type=int,
        default=None,
        help="settle only this gameweek, if it is both unsettled and checked",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be rewritten without writing anything",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    # Database first: an unconfigured DATABASE_URL should fail before an HTTP
    # client is opened, not after.
    with db.connect() as conn:
        with FPLClient() as client:
            rows = run(
                client,
                conn,
                args.season,
                only_gw=args.gameweek,
                dry_run=args.dry_run,
            )

    log.info("%s: %d rows", JOB, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
