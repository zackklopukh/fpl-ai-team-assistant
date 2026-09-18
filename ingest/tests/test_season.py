"""Tests for season and deadline state.

Offline: no network, no database. Every function under test takes its clock as an
argument, which is the only reason a season boundary can be tested at all.

What is worth guarding here is the difference between an answer and a guess. The
calendar fallback is correct roughly always and wrong exactly when it matters —
the year the season starts early, or the November week with a long gap between
deadlines — so the tests below check that a populated gameweek table overrules it
and that the fallback is reached only when there is nothing to read.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from season import (  # noqa: E402
    DEADLINE_WINDOW_HOURS,
    SEASON_LOOKAHEAD_DAYS,
    SEASON_TAIL_DAYS,
    SOURCE_CALENDAR,
    SOURCE_GAMEWEEKS,
    is_calendar_off_season,
    next_deadline,
    previous_deadline,
    season_state,
)


def moment(year, month, day, hour=12, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# A realistic 2026-27 calendar: weekly deadlines from mid-August, with a long
# international break in the middle to prove a gap is not mistaken for summer.
SEASON_START = moment(2026, 8, 21, 17, 30)
DEADLINES = (
    [SEASON_START + timedelta(weeks=n) for n in range(4)]
    # 25 days with no football, which is longer than the lookahead window.
    + [SEASON_START + timedelta(weeks=3, days=25)]
    + [SEASON_START + timedelta(weeks=3, days=25 + 7 * n) for n in range(1, 30)]
)
SEASON_END = max(DEADLINES)


class TestCalendarFallback:
    def test_midsummer_is_off_season(self):
        assert is_calendar_off_season(moment(2026, 6, 15))
        assert is_calendar_off_season(moment(2026, 7, 1))

    def test_the_window_is_closed_at_both_declared_edges(self):
        assert is_calendar_off_season(moment(2026, 5, 25))
        assert not is_calendar_off_season(moment(2026, 5, 24))
        assert is_calendar_off_season(moment(2026, 8, 10))
        assert not is_calendar_off_season(moment(2026, 8, 11))

    def test_midseason_is_in_season(self):
        assert not is_calendar_off_season(moment(2026, 12, 20))

    def test_no_deadlines_falls_back_and_says_so(self):
        state = season_state([], moment(2026, 7, 1))

        assert state.source == SOURCE_CALENDAR
        assert state.is_guess
        assert state.in_season is False
        assert "no gameweek deadlines" in state.reason

    def test_none_is_treated_as_unreachable_not_as_empty_season(self):
        # `deadlines_or_none` returns None when the database cannot be reached.
        # That has to reach the calendar, not be read as "the season has no
        # gameweeks" and reported as a confident off-season.
        state = season_state(None, moment(2026, 12, 20))

        assert state.source == SOURCE_CALENDAR
        assert state.in_season is True


class TestSeasonStateFromDeadlines:
    def test_a_real_gameweek_list_overrules_the_calendar(self):
        # 8 August: the calendar heuristic says summer, but the first deadline
        # is 13 days out, so there is very much a season starting.
        now = moment(2026, 8, 8)
        assert is_calendar_off_season(now)

        state = season_state(DEADLINES, now)

        assert state.source == SOURCE_GAMEWEEKS
        assert state.in_season is True
        assert state.is_guess is False

    def test_before_the_lookahead_window_is_off_season(self):
        now = SEASON_START - timedelta(days=SEASON_LOOKAHEAD_DAYS + 1)
        state = season_state(DEADLINES, now)

        assert state.in_season is False
        assert state.source == SOURCE_GAMEWEEKS
        assert state.previous_deadline is None
        assert state.next_deadline == SEASON_START

    def test_the_lookahead_boundary_is_inclusive(self):
        just_inside = SEASON_START - timedelta(days=SEASON_LOOKAHEAD_DAYS)
        just_outside = just_inside - timedelta(minutes=1)

        assert season_state(DEADLINES, just_inside).in_season is True
        assert season_state(DEADLINES, just_outside).in_season is False

    def test_a_long_midseason_gap_is_not_the_summer(self):
        # Inside the 25-day break. No deadline is within the lookahead window in
        # either direction, and it is still the middle of the season.
        now = DEADLINES[3] + timedelta(days=12)
        state = season_state(DEADLINES, now)

        assert state.in_season is True
        assert "between gameweek deadlines" in state.reason

    def test_the_tail_outlives_the_final_deadline(self):
        inside = SEASON_END + timedelta(days=SEASON_TAIL_DAYS)
        outside = inside + timedelta(minutes=1)

        assert season_state(DEADLINES, inside).in_season is True
        # The final gameweek's bonus has settled by now; nothing left to poll.
        assert season_state(DEADLINES, outside).in_season is False

    def test_after_the_season_reports_no_next_deadline(self):
        state = season_state(DEADLINES, SEASON_END + timedelta(days=30))

        assert state.next_deadline is None
        assert state.time_until_deadline is None
        assert state.previous_deadline == SEASON_END

    def test_deadlines_need_not_arrive_sorted(self):
        shuffled = list(reversed(DEADLINES))
        now = moment(2026, 8, 8)

        assert season_state(shuffled, now) == season_state(DEADLINES, now)


class TestDeadlinePredicates:
    def test_next_and_previous_split_at_the_deadline_itself(self):
        exactly = DEADLINES[2]

        # A deadline that has just passed is behind us, not ahead: picks are
        # locked the instant it lands.
        assert previous_deadline(DEADLINES, exactly) == exactly
        assert next_deadline(DEADLINES, exactly) == DEADLINES[3]

    def test_time_until_deadline_counts_down(self):
        now = DEADLINES[2] - timedelta(hours=5)
        state = season_state(DEADLINES, now)

        assert state.next_deadline == DEADLINES[2]
        assert state.time_until_deadline == timedelta(hours=5)

    def test_sync_often_turns_on_at_the_window_edge(self):
        deadline = DEADLINES[2]
        inside = deadline - timedelta(hours=DEADLINE_WINDOW_HOURS)
        outside = inside - timedelta(minutes=1)

        assert season_state(DEADLINES, inside).should_sync_often is True
        assert season_state(DEADLINES, outside).should_sync_often is False

    def test_sync_often_stops_the_moment_the_deadline_passes(self):
        deadline = DEADLINES[2]

        assert season_state(DEADLINES, deadline - timedelta(minutes=1)).should_sync_often
        # One second later the next deadline is a week away and the news that
        # mattered is already in the squad.
        assert not season_state(DEADLINES, deadline + timedelta(seconds=1)).should_sync_often

    def test_the_window_covers_a_friday_press_conference(self):
        # ARCHITECTURE.md's actual case: news lands Friday afternoon, the
        # deadline is Saturday morning, and a nightly cron has already missed it.
        deadline = moment(2026, 11, 7, 11, 30)
        friday_afternoon = moment(2026, 11, 6, 14, 0)

        assert season_state([deadline], friday_afternoon).should_sync_often is True

    def test_off_season_never_asks_for_more_syncing(self):
        # A deadline three weeks out is inside nobody's news window, but the
        # predicate also has to be false for the off-season itself.
        state = season_state([], moment(2026, 7, 1))
        assert state.should_sync_often is False

    def test_describe_names_the_evidence_when_it_is_a_guess(self):
        guessed = season_state([], moment(2026, 7, 1))
        known = season_state(DEADLINES, moment(2026, 12, 20))

        assert "calendar" in guessed.describe()
        assert "calendar" not in known.describe()
