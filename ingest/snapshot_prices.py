"""Daily price snapshot into `price_history`.

FPL moves prices at roughly 01:30 UTC, so this runs at 01:45 — late enough that
the new prices are live, early enough that nobody has yet planned a transfer
against them.

Why a whole table for one number a day: purchase-price reconstruction (see
`money.reconstruct_purchase_prices`) leans on knowing what a player cost on a past
date, and the API will not tell you retrospectively. It is also the training data
for predicting overnight rises, which is a feature people actually want.

Keyed `(season, element_id, as_of_date)`, so a re-run on the same day overwrites
that day's row instead of duplicating it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from config import SEASON
from db import connect, run_logged, upsert
from fpl_client import FPLClient

log = logging.getLogger(__name__)

JOB = "snapshot_prices"

CONFLICT_KEYS = ("season", "element_id", "as_of_date")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def price_rows(
    season: str, bootstrap: dict[str, Any], as_of: dt.date
) -> list[dict[str, Any]]:
    """One row per player from a bootstrap payload. Pure.

    `cost_tenths` is `now_cost` straight through — integer tenths, as FPL sends
    it. There is no arithmetic here on purpose; the moment a price passes through
    a float it can come back a tenth light.
    """
    rows: list[dict[str, Any]] = []

    for element in bootstrap.get("elements", []):
        rows.append(
            {
                "season": season,
                "element_id": int(element["id"]),
                "as_of_date": as_of,
                "cost_tenths": int(element["now_cost"]),
                "cost_change_event_tenths": int(element.get("cost_change_event") or 0),
                "selected_by_percent": _decimal(element.get("selected_by_percent")),
                "transfers_in_event": (
                    None
                    if element.get("transfers_in_event") is None
                    else int(element["transfers_in_event"])
                ),
                "transfers_out_event": (
                    None
                    if element.get("transfers_out_event") is None
                    else int(element["transfers_out_event"])
                ),
                # Availability as of today. Nothing else in the schema records it
                # historically, and it is the dominant input to expected minutes,
                # so a backtest has no way to reproduce the model without it.
                "status": (element.get("status") or None),
                "chance_of_playing_next_round": (
                    None
                    if element.get("chance_of_playing_next_round") is None
                    else int(element["chance_of_playing_next_round"])
                ),
            }
        )

    return rows


def today_utc() -> dt.date:
    """The snapshot's date. UTC, because the deadline clock is UTC."""
    return dt.datetime.now(dt.UTC).date()


def run(season: str, as_of: dt.date, dry_run: bool) -> int:
    with FPLClient() as client:
        bootstrap = client.bootstrap_static()

    rows = price_rows(season, bootstrap, as_of)

    if dry_run:
        log.info("dry run: %d price rows for %s", len(rows), as_of)
        return 0

    with connect() as conn:
        with run_logged(conn, JOB, season) as result:
            result["rows"] = upsert(conn, "price_history", rows, CONFLICT_KEYS)
            return result["rows"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=SEASON)
    parser.add_argument(
        "--date",
        default=None,
        help="ISO date to stamp the snapshot with; defaults to today in UTC",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    as_of = dt.date.fromisoformat(args.date) if args.date else today_utc()
    rows = run(args.season, as_of, args.dry_run)
    log.info("%s: %d rows for %s", JOB, rows, as_of)


if __name__ == "__main__":
    main()
