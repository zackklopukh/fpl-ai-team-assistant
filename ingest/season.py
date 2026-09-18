"""Season and deadline state, read from the database rather than guessed.

Two questions the rest of the ingestion layer keeps asking, and until now had no
shared answer to:

**"Is there a season on right now?"** Between late May and mid-August element ids
are reassigned, three clubs are replaced, fixtures do not exist and most of the
API returns nulls or last season's data (ARCHITECTURE.md, "The summer"). Polling
that every 15 minutes for three months buys nothing and raises the odds of a
block. The workflows currently guard it with a hard-coded calendar window, which
their own comments call a heuristic: the real first and last fixture move by a
week or two each year and a summer tournament moves them further.

The honest version is a database read — "is there a deadline near enough to
matter" — because `gameweeks.deadline_time` is the actual calendar, published by
FPL in June. The calendar window survives here as an explicit fallback for the
two cases where the honest answer is unavailable: the table is empty (before the
first bootstrap sync, which is exactly where this project is today) or the
database is unreachable. A fallback that is named in the output is a heuristic
you can see; one buried in a bash step is a heuristic you trip over.

**"Should we be syncing more often than usual?"** Press conferences land Friday
afternoon and a nightly cron is already wrong by Saturday morning, which is when
people actually use the tool (ARCHITECTURE.md, "Stale injury news"). Traffic
concentrates in the same window. `should_sync_often` is that predicate, exposed
so a workflow can eventually branch on it instead of running one cadence all
week.

Everything above `load_deadlines` is pure: it takes a list of deadlines and a
clock and returns an answer. The clock is always a parameter, never
`datetime.now()` reached for mid-function, because a season boundary that cannot
be tested at a boundary is a season boundary that is wrong at one.

Run it to see where things stand:

    python ingest/season.py
    python ingest/run.py season
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

log = logging.getLogger(__name__)

# How far ahead a deadline can be and still mean "the season is starting".
# Three weeks: fixtures and prices are published well before GW1, and the
# bootstrap payload is worth syncing from the moment they are.
SEASON_LOOKAHEAD_DAYS = 21

# How long after the final deadline the season is still considered live. The
# last gameweek's matches run for days after its deadline and bonus settles
# after that, so the tail has to outlive the deadline itself.
SEASON_TAIL_DAYS = 7

# The high-traffic, high-news window before a deadline. Friday press
# conferences to a Saturday 11:30 deadline is a little under 24 hours, so 24
# is the smallest number that covers the case this exists for.
DEADLINE_WINDOW_HOURS = 24

# The calendar fallback, matching the window the workflows use today. Loose at
# both ends on purpose: it is better to skip a day that had nothing in it than
# to guess the season started and hammer a dead API.
_OFF_SEASON_FROM = (5, 25)  # 25 May
_OFF_SEASON_UNTIL = (8, 10)  # 10 August, inclusive

SOURCE_GAMEWEEKS = "gameweeks"
SOURCE_CALENDAR = "calendar"


def utcnow() -> datetime:
    """The clock everything derives from, in UTC. Deadlines are stored in UTC."""
    return datetime.now(UTC)


# --- Pure state -----------------------------------------------------------


@dataclass(frozen=True)
class SeasonState:
    """What we believe about the season, and on what evidence.

    `source` is part of the answer, not decoration: "off-season, and we are
    guessing" and "off-season, and the gameweek table says so" call for
    different responses from whoever is reading the output.
    """

    now: datetime
    in_season: bool
    source: str
    reason: str
    next_deadline: datetime | None = None
    previous_deadline: datetime | None = None

    @property
    def is_guess(self) -> bool:
        return self.source == SOURCE_CALENDAR

    @property
    def time_until_deadline(self) -> timedelta | None:
        """How long until the next deadline, or None if there is not one."""
        if self.next_deadline is None:
            return None
        return self.next_deadline - self.now

    @property
    def in_deadline_window(self) -> bool:
        """True inside the hours before a deadline, when news and traffic land."""
        remaining = self.time_until_deadline
        if remaining is None:
            return False
        return timedelta(0) <= remaining <= timedelta(hours=DEADLINE_WINDOW_HOURS)

    @property
    def should_sync_often(self) -> bool:
        """The predicate a workflow can branch its cadence on.

        Syncing more often is only ever worth it when there is a season on and a
        deadline close enough that a price change or an injury flag would change
        someone's team in the next few hours.
        """
        return self.in_season and self.in_deadline_window

    def describe(self) -> str:
        """One line, for a log or a workflow step summary."""
        state = "in season" if self.in_season else "off-season"
        via = f" (via {self.source})" if self.is_guess else ""
        return f"{state}{via}: {self.reason}"


def is_calendar_off_season(now: datetime) -> bool:
    """The fallback: is today inside the usual summer gap?

    Used only when the gameweek table cannot answer. Deliberately the same
    window the workflows already apply, so replacing theirs with this one does
    not quietly change behaviour on the day it lands.
    """
    month, day = now.month, now.day

    if month in (6, 7):
        return True
    if (month, day) >= _OFF_SEASON_FROM and month == _OFF_SEASON_FROM[0]:
        return True  # late May
    if month == _OFF_SEASON_UNTIL[0] and day <= _OFF_SEASON_UNTIL[1]:
        return True  # early August
    return False


def next_deadline(deadlines: Sequence[datetime], now: datetime) -> datetime | None:
    """The first deadline strictly after `now`, or None once they have all passed."""
    future = [d for d in deadlines if d > now]
    return min(future) if future else None


def previous_deadline(deadlines: Sequence[datetime], now: datetime) -> datetime | None:
    """The most recent deadline at or before `now`, or None before the first."""
    past = [d for d in deadlines if d <= now]
    return max(past) if past else None


def season_state(
    deadlines: Sequence[datetime] | None,
    now: datetime,
    *,
    lookahead_days: int = SEASON_LOOKAHEAD_DAYS,
    tail_days: int = SEASON_TAIL_DAYS,
) -> SeasonState:
    """Decide whether a season is on, from deadlines if there are any.

    `deadlines` is every `gameweeks.deadline_time` for the season, in any order.
    None or empty means the table could not answer — no rows yet, or no database
    — and the calendar window decides instead.

    With deadlines, three cases, in this order:

    * Between the first and last deadline: in season, whatever the gap. A
      mid-season break can run longer than the lookahead window — an
      international break or a winter World Cup — and "no deadline for 25 days"
      in November is not the summer.
    * Before the first: in season only once that deadline is within
      `lookahead_days`, which is when prices and fixtures become real.
    * After the last: in season for `tail_days` more, because the final
      gameweek's matches and its bonus settlement both outlive its deadline.
    """
    if not deadlines:
        off = is_calendar_off_season(now)
        return SeasonState(
            now=now,
            in_season=not off,
            source=SOURCE_CALENDAR,
            reason=(
                "no gameweek deadlines available, and the date is inside the "
                "usual summer gap"
                if off
                else "no gameweek deadlines available, but the date is outside "
                "the usual summer gap"
            ),
        )

    ordered = sorted(deadlines)
    first, last = ordered[0], ordered[-1]
    nxt = next_deadline(ordered, now)
    prev = previous_deadline(ordered, now)

    def state(in_season: bool, reason: str) -> SeasonState:
        return SeasonState(
            now=now,
            in_season=in_season,
            source=SOURCE_GAMEWEEKS,
            reason=reason,
            next_deadline=nxt,
            previous_deadline=prev,
        )

    if prev is not None and nxt is not None:
        return state(True, f"between gameweek deadlines; next is {_stamp(nxt)}")

    if prev is None:
        away = first - now
        if away <= timedelta(days=lookahead_days):
            return state(True, f"first deadline is in {_humanise(away)}")
        return state(
            False,
            f"first deadline is {_humanise(away)} away, more than "
            f"{lookahead_days} days",
        )

    since = now - last
    if since <= timedelta(days=tail_days):
        return state(True, f"final deadline was {_humanise(since)} ago")
    return state(
        False,
        f"final deadline was {_humanise(since)} ago, more than {tail_days} days",
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _humanise(delta: timedelta) -> str:
    """A duration a person reads at a glance, not a timedelta repr."""
    seconds = int(abs(delta).total_seconds())
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --- Database edge --------------------------------------------------------


def load_deadlines(conn, season: str) -> list[datetime]:
    """Every deadline for a season, oldest first. May legitimately be empty."""
    with conn.cursor() as cur:
        cur.execute(
            "select deadline_time from gameweeks where season = %s "
            "order by deadline_time",
            (season,),
        )
        return [row[0] for row in cur.fetchall()]


def deadlines_or_none(season: str) -> list[datetime] | None:
    """Read the deadlines, or None if the database cannot be reached.

    Swallowing the connection error is the point: a season check that raises is
    a season check no caller will put in front of a job, and "unreachable" and
    "empty" both mean the same thing here — fall back to the calendar. The
    failure is logged rather than hidden, because a database that is down during
    the season is a real problem wearing an off-season costume.
    """
    try:
        import db  # imported here so an unconfigured DATABASE_URL is not fatal

        with db.connect() as conn:
            return load_deadlines(conn, season)
    except SystemExit as exc:  # require_database_url
        log.warning("season: %s — falling back to the calendar", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — any driver error means "unreachable"
        log.warning(
            "season: could not read gameweeks (%s) — falling back to the calendar",
            exc,
        )
        return None


def current_state(season: str | None = None, now: datetime | None = None) -> SeasonState:
    """The state right now, from the database where possible."""
    season = season or config.SEASON
    return season_state(deadlines_or_none(season), now or utcnow())


# --- CLI ------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report season and deadline state.",
        epilog=(
            "Exits 0 normally. With --exit-code, exits 1 when the season is off, "
            "so a workflow step can gate on it."
        ),
    )
    parser.add_argument("--season", default=config.SEASON)
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 when off-season instead of only reporting it",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    state = current_state(args.season)

    print(f"season          {args.season}")
    print(f"now             {_stamp(state.now)}")
    print(f"state           {state.describe()}")
    print(f"evidence        {state.source}")
    if state.previous_deadline:
        print(f"last deadline   {_stamp(state.previous_deadline)}")
    if state.next_deadline:
        remaining = state.time_until_deadline
        print(
            f"next deadline   {_stamp(state.next_deadline)} "
            f"(in {_humanise(remaining)})"
        )
    else:
        print("next deadline   none known")
    print(f"sync often      {'yes' if state.should_sync_often else 'no'}")

    if args.exit_code and not state.in_season:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
