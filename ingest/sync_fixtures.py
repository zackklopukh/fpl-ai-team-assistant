"""Sync `fixtures/` into the `fixtures` table.

Daily is enough: fixtures move for TV and for cup progress, but not hourly. The
job pulls the whole season rather than one gameweek, because a rescheduled match
changes a row for a gameweek that is not the current one.

Two shapes this has to survive:

* `event` is null for a fixture FPL has not yet assigned to a gameweek — the
  postponed match waiting on a cup replay. That null is carried through to `gw`
  rather than dropped, so the fixture still exists and gets a gameweek later.
* A team appears twice in a gameweek (a double) or not at all (a blank). Nothing
  here assumes one fixture per team per gameweek, and nothing reading these rows
  should either.
"""

from __future__ import annotations

import logging
import sys

import config
import db
from fpl_client import FPLClient
from sync_bootstrap import _now, as_timestamp

log = logging.getLogger(__name__)


def build_fixture_rows(payload: list[dict], season: str = config.SEASON) -> list[dict]:
    """fixtures[] -> `fixtures` rows.

    `kickoff_time` is null until the match is scheduled, and the started/finished
    flags come back null rather than false on those same fixtures — the schema
    wants booleans, so they are coerced.
    """
    updated_at = _now()

    return [
        {
            "season": season,
            "fixture_id": fixture["id"],
            # The API calls the gameweek "event"; null means not yet scheduled.
            "gw": fixture.get("event"),
            "team_h_fpl_id": fixture["team_h"],
            "team_a_fpl_id": fixture["team_a"],
            "team_h_difficulty": fixture.get("team_h_difficulty"),
            "team_a_difficulty": fixture.get("team_a_difficulty"),
            "kickoff_time": as_timestamp(fixture.get("kickoff_time")),
            "started": bool(fixture.get("started")),
            "finished": bool(fixture.get("finished")),
            "finished_provisional": bool(fixture.get("finished_provisional")),
            "minutes": int(fixture.get("minutes") or 0),
            "team_h_score": fixture.get("team_h_score"),
            "team_a_score": fixture.get("team_a_score"),
            "updated_at": updated_at,
        }
        for fixture in payload
    ]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    with FPLClient() as client:
        payload = client.fixtures()

    rows = build_fixture_rows(payload)
    unscheduled = sum(1 for row in rows if row["gw"] is None)

    with db.connect() as conn:
        with db.run_logged(conn, "fixtures", config.SEASON) as result:
            written = db.upsert(conn, "fixtures", rows, ["season", "fixture_id"])
            log.info("fixtures: %d rows (%d unscheduled)", written, unscheduled)
            result["rows"] = written

    return 0


if __name__ == "__main__":
    sys.exit(main())
