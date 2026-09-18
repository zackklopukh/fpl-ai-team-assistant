"""Tests for the CLI front door.

Offline: no network, no database. Every job's runner is replaced with a recorder,
so what is under test is the dispatch and the argument wiring, not the jobs.

The failure this guards is the boring one: `run.py live` quietly invoking the
price snapshot, or a `--dry-run` flag that parses and is then never passed on.
Both look fine in a terminal and are wrong in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402

# Every job the workflows and the operator's guide reference. A job added
# without a line in ingest/README.md is a job nobody will run.
EXPECTED_JOBS = {
    "bootstrap",
    "fixtures",
    "live",
    "prices",
    "xp",
    "settle-bonus",
    "backfill",
    "verify",
    "season",
}


@pytest.fixture
def recorded(monkeypatch):
    """Replace every runner with one that records the parsed arguments."""
    calls: list[tuple[str, dict]] = []

    for name, job in run.JOBS.items():
        def recorder(args, _name=name):
            calls.append((_name, vars(args)))
            return 0

        monkeypatch.setattr(job, "call", recorder)

    return calls


class TestRegistry:
    def test_every_scheduled_job_is_reachable(self):
        assert set(run.JOBS) == EXPECTED_JOBS

    def test_each_job_describes_its_schedule_and_what_it_writes(self):
        # The help text is the only place the production schedule and the CLI
        # sit next to each other, so an empty one is a real gap.
        for job in run.JOBS.values():
            assert job.summary.strip()
            assert job.schedule.strip()
            assert job.writes.strip()
            assert job.name in run.build_parser().format_help()


class TestDispatch:
    @pytest.mark.parametrize("name", sorted(EXPECTED_JOBS))
    def test_a_job_name_runs_that_job_and_nothing_else(self, recorded, name):
        assert run.main([name]) == 0

        assert [call[0] for call in recorded] == [name]

    def test_an_unknown_job_is_rejected(self, recorded):
        with pytest.raises(SystemExit) as raised:
            run.main(["definitely-not-a-job"])

        # argparse's own exit code for a usage error.
        assert raised.value.code == 2
        assert recorded == []

    def test_no_job_at_all_is_rejected(self, recorded):
        with pytest.raises(SystemExit) as raised:
            run.main([])

        assert raised.value.code == 2
        assert recorded == []

    def test_help_exits_cleanly_without_running_anything(self, recorded, capsys):
        with pytest.raises(SystemExit) as raised:
            run.main(["--help"])

        assert raised.value.code == 0
        assert recorded == []
        # The help is the documentation, so it has to actually say when things
        # run rather than only listing names.
        assert "01:45" in capsys.readouterr().out


class TestArguments:
    def test_dry_run_reaches_the_jobs_that_support_it(self, recorded):
        for name in ("prices", "settle-bonus", "backfill"):
            recorded.clear()
            run.main([name, "--dry-run"])
            assert recorded[0][1]["dry_run"] is True

    def test_dry_run_defaults_to_off(self, recorded):
        run.main(["prices"])
        assert recorded[0][1]["dry_run"] is False

    def test_a_job_without_a_dry_run_rejects_the_flag(self, recorded):
        # bootstrap writes whatever bootstrap-static currently says; there is no
        # halfway version of that, and pretending otherwise would be worse than
        # not offering the flag.
        with pytest.raises(SystemExit) as raised:
            run.main(["bootstrap", "--dry-run"])

        assert raised.value.code == 2

    def test_the_live_gameweek_is_parsed_as_an_int(self, recorded):
        run.main(["live", "--gameweek", "7"])
        assert recorded[0][1]["gameweek"] == 7

    def test_the_live_gameweek_defaults_to_the_current_one(self, recorded):
        run.main(["live"])
        assert recorded[0][1]["gameweek"] is None

    def test_backfill_resumes_from_an_element_id(self, recorded):
        run.main(["backfill", "--from-element", "412", "--limit", "10"])

        args = recorded[0][1]
        assert args["from_element"] == 412
        assert args["limit"] == 10

    def test_backfill_starts_from_the_beginning_by_default(self, recorded):
        run.main(["backfill"])
        assert recorded[0][1]["from_element"] == 0

    def test_season_is_overridable_on_the_jobs_that_take_one(self, recorded):
        for name in ("prices", "xp", "settle-bonus", "backfill", "verify", "season"):
            recorded.clear()
            run.main([name, "--season", "2027-28"])
            assert recorded[0][1]["season"] == "2027-28"
