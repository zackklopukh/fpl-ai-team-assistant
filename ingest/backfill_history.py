"""Backfill per-gameweek player history into `player_gw_stats`.

Run this now, and keep running it. `element-summary/{id}/` is the only public
source of per-gameweek detail and it only ever exposes the *current* season. Every
week this job does not run is a week of training data that cannot be recovered
later at any price.

The two columns that justify the whole job are `value_tenths` and `selected_by`.
They come from the history row's own `value` and `selected`, which are the price
and ownership *as they were in that gameweek*. Filling them from the `players`
table would put gameweek-38 information into a gameweek-3 row, which is the
lookahead leak ARCHITECTURE.md warns about: it does not crash, it just quietly
makes every backtest number downstream fiction.

Shape of the job: ~659 players, one request each, serialised at the client's
built-in interval. That is roughly seven minutes of wall clock and it is meant to
be. It is also resumable — `--from-element` picks up where a failure left off, and
rows already in the database are skipped rather than re-requested from Postgres.
"""

from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from typing import Any

from config import SEASON
from db import connect, run_logged, upsert
from fpl_client import FPLClient

log = logging.getLogger(__name__)

JOB = "backfill_history"

# The natural key of player_gw_stats. A double gameweek gives one player two rows
# in the same gw with different fixtures, so fixture_id has to be part of it.
CONFLICT_KEYS = ("season", "element_id", "gw", "fixture_id")

# Numeric columns that arrive from the API as strings. Decimal, not float — these
# are not money, but a float round-trip through a numeric(8,2) column is still a
# needless source of drift.
_DECIMAL_FIELDS = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "influence",
    "creativity",
    "threat",
    "ict_index",
)

_INT_FIELDS = (
    "minutes",
    "total_points",
    "starts",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "defensive_contribution",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value)


def history_rows(
    season: str,
    element_id: int,
    summary: dict[str, Any],
    settled_gws: frozenset[int] | set[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Turn one `element-summary/{id}/` payload into `player_gw_stats` rows.

    Pure — this is the part worth testing. `settled_gws` is the set of gameweeks
    bootstrap reports as `data_checked`, i.e. where bonus points are final; rows
    outside it are written with `bonus_settled = false` so the live-results job
    knows it is allowed to rewrite them.

    A player with two fixtures in one gameweek produces two rows. A player with
    none produces none, which is why blanks are an absence here rather than a row.
    """
    rows: list[dict[str, Any]] = []

    for entry in summary.get("history", []):
        gw = entry.get("round")
        fixture_id = entry.get("fixture")

        # Both are part of the primary key. A row missing either is unusable, and
        # in practice only turns up mid-season when FPL has yet to schedule a
        # rearranged fixture.
        if gw is None or fixture_id is None:
            log.warning(
                "element %s: skipping history row with round=%r fixture=%r",
                element_id,
                gw,
                fixture_id,
            )
            continue

        row: dict[str, Any] = {
            "season": season,
            "element_id": element_id,
            "gw": int(gw),
            "fixture_id": int(fixture_id),
        }

        for field in _INT_FIELDS:
            row[field] = _int(entry.get(field))

        for field in _DECIMAL_FIELDS:
            row[field] = _decimal(entry.get(field))

        row["was_home"] = entry.get("was_home")
        row["opponent_team_fpl_id"] = (
            None if entry.get("opponent_team") is None else int(entry["opponent_team"])
        )

        # The whole point of the job. `value` is the price during this gameweek and
        # `selected` the ownership count at the time. Never source these from the
        # players table — that is today's price, and using it to predict a past
        # gameweek is lookahead leakage.
        row["value_tenths"] = (
            None if entry.get("value") is None else int(entry["value"])
        )
        row["selected_by"] = (
            None if entry.get("selected") is None else int(entry["selected"])
        )

        row["bonus_settled"] = int(gw) in settled_gws

        rows.append(row)

    return rows


def settled_gameweeks(bootstrap: dict[str, Any]) -> frozenset[int]:
    """Gameweeks whose bonus points are final, per bootstrap's `data_checked`."""
    return frozenset(
        int(event["id"]) for event in bootstrap.get("events", []) if event.get("data_checked")
    )


def filter_new_rows(
    rows: list[dict[str, Any]], existing: set[tuple[int, int, int]]
) -> list[dict[str, Any]]:
    """Drop rows already written, keyed by (element_id, gw, fixture_id).

    This is what makes a half-finished run cheap to resume: the expensive part is
    the HTTP request, but re-writing thousands of unchanged rows for every retry
    is still wasted work and a needless window for a partial write.
    """
    return [
        row
        for row in rows
        if (row["element_id"], row["gw"], row["fixture_id"]) not in existing
    ]


def existing_keys(conn, season: str) -> set[tuple[int, int, int]]:
    """Every (element_id, gw, fixture_id) already stored for this season."""
    with conn.cursor() as cur:
        cur.execute(
            "select element_id, gw, fixture_id from player_gw_stats where season = %s",
            (season,),
        )
        return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def elements_to_walk(
    bootstrap: dict[str, Any], from_element: int = 0
) -> list[int]:
    """Element ids in ascending order, starting at `from_element`.

    Ascending and stable so that `--from-element N` after a failure at N means
    exactly what it looks like it means.
    """
    return sorted(
        int(e["id"]) for e in bootstrap.get("elements", []) if int(e["id"]) >= from_element
    )


def run(season: str, from_element: int, limit: int | None, dry_run: bool) -> int:
    with FPLClient() as client:
        bootstrap = client.bootstrap_static()
        settled = settled_gameweeks(bootstrap)
        elements = elements_to_walk(bootstrap, from_element)
        if limit is not None:
            elements = elements[:limit]

        if dry_run:
            log.info("dry run: would walk %d elements from %d", len(elements), from_element)
            return 0

        with connect() as conn:
            with run_logged(conn, JOB, season) as result:
                existing = existing_keys(conn, season)
                log.info(
                    "walking %d elements from %d; %d rows already stored",
                    len(elements),
                    from_element,
                    len(existing),
                )

                written = 0
                for index, element_id in enumerate(elements, start=1):
                    summary = client.element_summary(element_id)
                    rows = filter_new_rows(
                        history_rows(season, element_id, summary, settled), existing
                    )

                    if rows:
                        upsert(conn, "player_gw_stats", rows, CONFLICT_KEYS)
                        # Commit per player. A job this long will be interrupted,
                        # and the cost of losing one player's rows is one request.
                        conn.commit()
                        written += len(rows)
                        existing.update(
                            (r["element_id"], r["gw"], r["fixture_id"]) for r in rows
                        )

                    if index % 50 == 0:
                        log.info(
                            "%d/%d elements (last id %d), %d rows written",
                            index,
                            len(elements),
                            element_id,
                            written,
                        )

                result["rows"] = written
                return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=SEASON)
    parser.add_argument(
        "--from-element",
        type=int,
        default=0,
        help="resume at this element id; the log line on failure tells you which",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N players")
    parser.add_argument(
        "--dry-run", action="store_true", help="report the work without touching anything"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rows = run(args.season, args.from_element, args.limit, args.dry_run)
    log.info("%s: %d rows", JOB, rows)


if __name__ == "__main__":
    main()
