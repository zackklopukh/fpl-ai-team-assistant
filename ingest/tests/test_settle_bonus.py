"""Tests for the bonus settlement job.

Offline: no network, no database. The FPL client and the connection are both
fakes, and the fake client counts its calls — because half of what this job has
to get right is *not* making a request.

Three things these guard:

* A gameweek with no provisional rows must cost nothing. This runs on a
  schedule and the usual answer is "nothing to do"; if that answer costs three
  HTTP requests it is a job that gets throttled for no reason.
* A settled gameweek's rows must actually change — final bonus in, flag flipped.
* The per-gameweek snapshot columns must survive a settle. `value_tenths` is the
  price during that gameweek, and the payload this job reads carries today's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settle_bonus  # noqa: E402
from settle_bonus import (  # noqa: E402
    PRESERVED_ON_SETTLE,
    gameweeks_to_settle,
    run,
    settle_update_columns,
)

SEASON = "2026-27"


# --- Fakes -----------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split()).lower()
        self.conn.queries.append(text)

        if "from player_gw_stats" in text:
            self._result = [(gw, count) for gw, count in self.conn.unsettled]
        elif "insert into ingest_runs" in text:
            self._result = [(1,)]
        else:
            self._result = []

    def executemany(self, query, rows):
        self.conn.written.extend(rows)

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class FakeConn:
    """Just enough connection for `unsettled_gameweeks` and `run_logged`."""

    def __init__(self, unsettled=()):
        self.unsettled = list(unsettled)
        self.queries: list[str] = []
        self.written: list[dict] = []
        self.commits = 0

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def explain_entry(fixture_id, **values):
    """One `explain` block, in the shape the live endpoint returns it."""
    return {
        "fixture": fixture_id,
        "stats": [
            {"identifier": identifier, "value": value, "points": 0}
            for identifier, value in values.items()
        ],
    }


FINAL_STATS = {
    "minutes": 90,
    "goals_scored": 1,
    "assists": 0,
    "clean_sheets": 1,
    "goals_conceded": 0,
    "starts": 1,
    "saves": 0,
    # The whole point of the job: live scoring had this at 2, the checked data
    # has it at 3.
    "bonus": 3,
    "bps": 41,
    "defensive_contribution": 8,
    "total_points": 12,
    "expected_goals": "0.42",
}


class FakeClient:
    """Records every call, so a test can assert one did not happen."""

    def __init__(self, events, live=None, fixtures=None):
        self._bootstrap = {"total_players": 1_000_000, "events": events, "elements": [
            {"id": 10, "team": 5, "now_cost": 78, "selected_by_percent": "12.0"}
        ]}
        self._live = live or {
            "elements": [
                {
                    "id": 10,
                    "stats": FINAL_STATS,
                    "explain": [explain_entry(101, minutes=90)],
                }
            ]
        }
        self._fixtures = fixtures or [{"id": 101, "team_h": 5, "team_a": 9}]
        self.calls: list[str] = []

    def bootstrap_static(self):
        self.calls.append("bootstrap")
        return self._bootstrap

    def fixtures(self, event=None):
        self.calls.append(f"fixtures:{event}")
        return self._fixtures

    def event_live(self, gw):
        self.calls.append(f"live:{gw}")
        return self._live


def events(**checked_by_gw):
    """`events[]` entries, as `{"gw4": True}` -> gw 4 is data_checked."""
    return [
        {"id": int(name.removeprefix("gw")), "data_checked": checked}
        for name, checked in checked_by_gw.items()
    ]


# --- Pure ------------------------------------------------------------------


class TestGameweeksToSettle:
    def test_only_the_intersection_is_settled(self):
        assert gameweeks_to_settle([3, 4, 5], {4, 5, 6}) == [4, 5]

    def test_an_unchecked_gameweek_is_left_alone(self):
        # Provisional rows FPL has not finalised. Rewriting them would produce
        # the same provisional numbers and spend two requests doing it.
        assert gameweeks_to_settle([7], {1, 2, 3}) == []

    def test_the_result_is_sorted_oldest_first(self):
        assert gameweeks_to_settle([9, 2, 5], {2, 5, 9}) == [2, 5, 9]


class TestSettleUpdateColumns:
    def test_the_gameweek_snapshot_columns_are_never_overwritten(self):
        row = {
            "season": SEASON,
            "element_id": 10,
            "gw": 4,
            "fixture_id": 101,
            "bonus": 3,
            "value_tenths": 78,
            "selected_by": 120_000,
            "bonus_settled": True,
        }
        columns = settle_update_columns(row)

        for preserved in PRESERVED_ON_SETTLE:
            assert preserved not in columns
        assert "bonus" in columns
        assert "bonus_settled" in columns

    def test_the_key_is_not_in_the_update_set(self):
        row = {"season": SEASON, "element_id": 10, "gw": 4, "fixture_id": 101, "bps": 41}
        assert settle_update_columns(row) == ["bps"]

    def test_a_new_column_is_picked_up_without_being_listed_here(self):
        # The list is derived from the row the transform produced, so a column
        # added to sync_live.build_stat_rows lands in the update automatically.
        row = {"season": SEASON, "element_id": 1, "gw": 1, "fixture_id": 1, "invented": 9}
        assert "invented" in settle_update_columns(row)


# --- The job ---------------------------------------------------------------


class TestRun:
    def test_nothing_unsettled_makes_no_request_at_all(self):
        conn = FakeConn(unsettled=[])
        client = FakeClient(events(gw4=True))

        assert run(client, conn, SEASON) == 0
        assert client.calls == []
        assert conn.written == []

    def test_an_unchecked_gameweek_costs_one_bootstrap_and_stops(self):
        conn = FakeConn(unsettled=[(7, 480)])
        client = FakeClient(events(gw7=False))

        assert run(client, conn, SEASON) == 0
        # Bootstrap is the only way to learn data_checked, so it is fetched;
        # the two per-gameweek endpoints are not.
        assert client.calls == ["bootstrap"]
        assert conn.written == []

    def test_a_checked_gameweek_is_rewritten_as_final(self, monkeypatch):
        conn = FakeConn(unsettled=[(4, 480)])
        client = FakeClient(events(gw4=True))
        captured = {}

        def fake_upsert(conn_, table, rows, conflict_keys, update_columns=None):
            captured.update(
                table=table,
                rows=rows,
                conflict_keys=list(conflict_keys),
                update_columns=update_columns,
            )
            return len(rows)

        monkeypatch.setattr(settle_bonus.db, "upsert", fake_upsert)

        written = run(client, conn, SEASON)

        assert written == 1
        assert client.calls == ["bootstrap", "fixtures:4", "live:4"]
        assert captured["table"] == "player_gw_stats"
        assert captured["conflict_keys"] == [
            "season",
            "element_id",
            "gw",
            "fixture_id",
        ]

        row = captured["rows"][0]
        assert row["gw"] == 4
        assert row["element_id"] == 10
        assert row["bonus"] == 3
        assert row["total_points"] == 12
        assert row["bonus_settled"] is True

    def test_the_settle_does_not_backdate_todays_price(self, monkeypatch):
        # The row still carries value_tenths for the insert case, but the
        # ON CONFLICT update must leave the stored one alone: it is the price
        # during that gameweek, and this payload only knows today's.
        conn = FakeConn(unsettled=[(4, 480)])
        client = FakeClient(events(gw4=True))
        captured = {}

        def fake_upsert(conn_, table, rows, conflict_keys, update_columns=None):
            captured["update_columns"] = update_columns
            captured["rows"] = rows
            return len(rows)

        monkeypatch.setattr(settle_bonus.db, "upsert", fake_upsert)
        run(client, conn, SEASON)

        assert "value_tenths" in captured["rows"][0]
        assert "value_tenths" not in captured["update_columns"]
        assert "selected_by" not in captured["update_columns"]

    def test_only_the_checked_half_of_a_mixed_backlog_is_settled(self, monkeypatch):
        conn = FakeConn(unsettled=[(4, 480), (5, 470)])
        client = FakeClient(events(gw4=True, gw5=False))
        monkeypatch.setattr(
            settle_bonus.db, "upsert", lambda *a, **k: len(a[2])
        )

        run(client, conn, SEASON)

        assert "live:4" in client.calls
        assert "live:5" not in client.calls

    def test_a_dry_run_reports_without_fetching_the_gameweek(self):
        conn = FakeConn(unsettled=[(4, 480)])
        client = FakeClient(events(gw4=True))

        assert run(client, conn, SEASON, dry_run=True) == 0
        assert client.calls == ["bootstrap"]
        assert conn.written == []

    def test_only_gw_narrows_to_one_gameweek(self, monkeypatch):
        conn = FakeConn(unsettled=[(4, 480), (5, 470)])
        client = FakeClient(events(gw4=True, gw5=True))
        monkeypatch.setattr(settle_bonus.db, "upsert", lambda *a, **k: len(a[2]))

        run(client, conn, SEASON, only_gw=5)

        assert "live:5" in client.calls
        assert "live:4" not in client.calls

    def test_only_gw_on_a_gameweek_that_is_not_ready_does_nothing(self):
        conn = FakeConn(unsettled=[(4, 480)])
        client = FakeClient(events(gw4=True))

        assert run(client, conn, SEASON, only_gw=9) == 0
        assert client.calls == ["bootstrap"]

    def test_a_checked_gameweek_with_an_empty_payload_writes_nothing(self, monkeypatch):
        conn = FakeConn(unsettled=[(4, 480)])
        client = FakeClient(events(gw4=True), live={"elements": []})
        monkeypatch.setattr(settle_bonus.db, "upsert", lambda *a, **k: pytest.fail(
            "should not upsert an empty row set"
        ))

        assert run(client, conn, SEASON) == 0

    def test_the_transform_is_the_one_sync_live_uses(self):
        # Not a behaviour test — a wiring test. Two copies of this mapping is how
        # the bonus column ends up disagreeing with the points column, so the
        # import is the thing being asserted.
        import sync_live

        assert settle_bonus.build_stat_rows is sync_live.build_stat_rows
        assert settle_bonus.snapshot_context is sync_live.snapshot_context
