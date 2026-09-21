"""Tests for publishing the xP artifact.

Offline: the database reads are replaced by synthetic rows shaped like the
query results, and the pure transforms are exercised directly. The round trip
ends in the optimizer's own loader, because the artifact is only correct if the
thing that reads it agrees about what it says.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

INGEST = Path(__file__).resolve().parent.parent
REPO_ROOT = INGEST.parent
sys.path.insert(0, str(INGEST))
sys.path.insert(0, str(REPO_ROOT))

from publish_xp import (  # noqa: E402
    DEFAULT_OUT,
    PublishError,
    build_artifact,
    latest_computation,
    validate_xp,
    write_atomic,
)
from optimizer.pool import (  # noqa: E402
    DEFAULT_ARTIFACT_PATH,
    ArtifactError,
    ArtifactXPProvider,
    validate_artifact,
)

SEASON = "2026-27"
GWS = [6, 7, 8]
COMPUTED_AT = datetime(2026, 9, 20, 5, 0, 7, tzinfo=UTC)
GENERATED_AT = datetime(2026, 9, 20, 6, 30, 0, tzinfo=UTC)
BLANK_TEAM, DOUBLE_TEAM = 1, 2  # team 1 blanks GW6, team 2 doubles GW7


def world():
    """20 clubs of 15 (2/5/5/3), a fixture list with one blank and one double,
    and xP that varies the way a working model's does."""
    players = []
    eid = 0
    for team in range(1, 21):
        for etype, n in ((1, 2), (2, 5), (3, 5), (4, 3)):
            for _ in range(n):
                eid += 1
                players.append(
                    {
                        "element_id": eid,
                        "web_name": f"P{eid}",
                        "team_fpl_id": team,
                        "element_type": etype,
                        "now_cost_tenths": 40 + (eid * 7) % 90,
                        "selected_by_percent": Decimal("12.30"),
                        "status": "a",
                    }
                )

    fixtures = []
    fid = 0
    for gw in GWS:
        teams = list(range(1, 21))
        if gw == 6:
            teams.remove(BLANK_TEAM)
            teams.remove(20)  # its opponent blanks too
        for h, a in zip(teams[::2], teams[1::2]):
            fid += 1
            fixtures.append({"fixture_id": fid, "gw": gw, "team_h_fpl_id": h, "team_a_fpl_id": a})
        if gw == 7:
            fid += 1
            fixtures.append(
                {"fixture_id": fid, "gw": gw, "team_h_fpl_id": DOUBLE_TEAM, "team_a_fpl_id": 9}
            )
    # An unscheduled fixture belongs to no gameweek and must be ignored.
    fixtures.append({"fixture_id": 999, "gw": None, "team_h_fpl_id": 3, "team_a_fpl_id": 4})

    counts: dict[tuple[int, int], int] = {}
    for f in fixtures:
        if f["gw"] is None:
            continue
        for t in (f["team_h_fpl_id"], f["team_a_fpl_id"]):
            counts[(t, f["gw"])] = counts.get((t, f["gw"]), 0) + 1

    xp_rows = []
    for p in players:
        for gw in GWS:
            n = counts.get((p["team_fpl_id"], gw), 0)
            per_match = (p["element_id"] % 23) * 0.3
            xmins = 0 if p["element_id"] % 5 == 0 else 30 + p["element_id"] % 60
            xp_rows.append(
                {
                    "element_id": p["element_id"],
                    "gw": gw,
                    "xp": Decimal(str(round(per_match * n if xmins else 0.0, 3))),
                    "xmins": Decimal(str(xmins)),
                    "as_of_gw": 5,
                    "fixture_count": n,
                    "computed_at": COMPUTED_AT,
                }
            )
    return players, xp_rows, fixtures


def build(players=None, xp_rows=None, fixtures=None):
    p, x, f = world()
    return build_artifact(
        season=SEASON,
        model_version="baseline-0.1",
        xp_rows=x if xp_rows is None else xp_rows,
        player_rows=p if players is None else players,
        fixture_rows=f if fixtures is None else fixtures,
        ingest_run_id=8,
        generated_at=GENERATED_AT,
    )


def by_id(doc):
    return {p["element_id"]: p for p in doc["players"]}


# --- The happy path ---------------------------------------------------------


def test_a_healthy_matrix_builds_and_validates_cleanly():
    doc, problems = build()
    assert problems == []
    assert validate_xp(doc) == []
    assert validate_artifact(doc, expected_season=SEASON) == []

    assert doc["season"] == SEASON
    assert doc["model_version"] == "baseline-0.1"
    assert doc["generated_at"] == "2026-09-20T06:30:00Z"
    assert doc["xp_computed_at"] == "2026-09-20T05:00:07Z"
    assert doc["as_of_gw"] == 5
    assert doc["target_gws"] == GWS
    assert doc["ingest_run_id"] == 8
    assert len(doc["players"]) == 300


def test_money_stays_integer_tenths():
    doc, _ = build()
    for p in doc["players"]:
        assert type(p["now_cost_tenths"]) is int
    # And it survives JSON as an int, not 55.0.
    assert '"now_cost_tenths":55.0' not in json.dumps(doc, separators=(",", ":"))


def test_fixture_counts_are_one_to_many():
    doc, _ = build()
    players = by_id(doc)
    blank = next(p for p in players.values() if p["team_fpl_id"] == BLANK_TEAM)
    double = next(p for p in players.values() if p["team_fpl_id"] == DOUBLE_TEAM)
    assert blank["n_fixtures"]["6"] == 0
    assert blank["xp"]["6"] == 0.0
    assert double["n_fixtures"]["7"] == 2
    assert all(n == 1 for p in players.values() if p["team_fpl_id"] == 5 for n in p["n_fixtures"].values())


def test_round_trip_publish_then_the_optimizer_loads_the_same_data(tmp_path):
    doc, problems = build()
    assert problems == [] and validate_xp(doc) == []
    out = tmp_path / "nested" / "xp.json"
    write_atomic(doc, out)
    assert not list(out.parent.glob(".*.tmp")), "temp file left behind"

    snap = ArtifactXPProvider(out, expected_season=SEASON).snapshot(SEASON)
    assert snap.season == doc["season"]
    assert snap.model_version == doc["model_version"]
    assert snap.generated_at == doc["generated_at"]
    assert snap.as_of_gw == doc["as_of_gw"]
    assert list(snap.target_gws) == doc["target_gws"]
    assert snap.ingest_run_id == doc["ingest_run_id"]
    assert len(snap.players) == len(doc["players"])

    for row in doc["players"]:
        p = snap.players[row["element_id"]]
        assert p.web_name == row["web_name"]
        assert p.team_fpl_id == row["team_fpl_id"]
        assert p.element_type == row["element_type"]
        assert p.price == row["now_cost_tenths"]
        assert p.ownership == row["selected_by_percent"]
        assert p.status == row["status"]
        assert p.xp_by_gw == {int(g): v for g, v in row["xp"].items()}
        assert p.fixtures_by_gw == {int(g): n for g, n in row["n_fixtures"].items()}


def test_default_out_is_where_the_optimizer_looks_and_is_git_ignored():
    assert DEFAULT_OUT == DEFAULT_ARTIFACT_PATH
    # .gitignore carries `*.local.json`.
    assert DEFAULT_OUT.name.endswith(".local.json")
    assert "*.local.json" in (REPO_ROOT / ".gitignore").read_text()


def test_wrong_season_artifact_is_refused_by_the_loader(tmp_path):
    doc, _ = build()
    doc["season"] = "2025-26"
    write_atomic(doc, tmp_path / "xp.json")
    with pytest.raises(ArtifactError, match="2025-26"):
        ArtifactXPProvider(tmp_path / "xp.json", expected_season=SEASON).snapshot(SEASON)


# --- Degenerate xP is refused ---------------------------------------------


def _mutate_xp(fn):
    players, xp_rows, fixtures = world()
    for r in xp_rows:
        fn(r)
    doc, problems = build(players, xp_rows, fixtures)
    return problems + validate_xp(doc)


def test_the_haaland_at_1_24_failure_is_refused():
    # One gameweek of data: every player with minutes projects about the same
    # small number, with the same expected minutes.
    def collapse(r):
        if r["fixture_count"]:
            r["xp"] = Decimal("1.24") + Decimal(r["element_id"] % 3) / 1000
            r["xmins"] = Decimal("45.00")

    problems = _mutate_xp(collapse)
    joined = " | ".join(problems)
    assert "top projection" in joined
    assert "near-identical" in joined
    assert "identical minutes" in joined


def test_identical_expected_minutes_alone_is_refused():
    def same_minutes(r):
        if Decimal(r["xmins"]) > 0:
            r["xmins"] = Decimal("90.00")

    problems = _mutate_xp(same_minutes)
    assert any("identical minutes" in p for p in problems)


def test_nobody_projecting_anything_is_refused():
    problems = _mutate_xp(lambda r: r.update(xp=Decimal("0")))
    assert any("project any points" in p for p in problems)


def test_a_blank_that_projects_points_is_refused():
    def leak(r):
        if r["fixture_count"] == 0:
            r["xp"] = Decimal("3.1")

    problems = _mutate_xp(leak)
    assert any("blank project points" in p for p in problems)


def test_fixtures_that_moved_since_the_model_ran_are_refused():
    players, xp_rows, fixtures = world()
    # A GW8 fixture is postponed after compute_xp ran: its two teams now blank,
    # but their xP still assumes a match.
    moved = next(f for f in fixtures if f["gw"] == 8)
    moved["gw"] = None
    _, problems = build(players, xp_rows, fixtures)
    assert any("fixture counts changed" in p for p in problems)


def test_a_half_finished_computation_is_refused():
    players, xp_rows, fixtures = world()
    keep = {p["element_id"] for p in players[:150]}
    _, problems = build(players, [r for r in xp_rows if r["element_id"] in keep], fixtures)
    assert any("covers 150/300" in p for p in problems)


def test_players_with_gaps_in_the_horizon_are_refused():
    players, xp_rows, fixtures = world()
    xp_rows = [r for r in xp_rows if not (r["element_id"] == 7 and r["gw"] == 8)]
    _, problems = build(players, xp_rows, fixtures)
    assert any("only some of" in p for p in problems)


def test_publish_error_lists_every_problem():
    err = PublishError(["one", "two"])
    assert err.problems == ["one", "two"]
    assert "one" in str(err) and "two" in str(err)


# --- Picking the computation ------------------------------------------------


def test_only_the_latest_run_of_a_version_is_published():
    _, xp_rows, _ = world()
    older = datetime(2026, 9, 13, 5, 0, tzinfo=UTC)
    stale = [{**r, "gw": 5, "computed_at": older, "as_of_gw": 4} for r in xp_rows if r["gw"] == 6]
    picked = latest_computation(stale + xp_rows)
    assert {r["gw"] for r in picked} == set(GWS)
    assert {r["computed_at"] for r in picked} == {COMPUTED_AT}


def test_rows_that_disagree_on_as_of_gw_are_refused():
    players, xp_rows, fixtures = world()
    xp_rows[0]["as_of_gw"] = 4
    _, problems = build(players, xp_rows, fixtures)
    assert any("as_of_gw" in p for p in problems)
