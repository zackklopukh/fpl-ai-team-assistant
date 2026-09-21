"""Tests for the HTTP surface. No network, no database -- TestClient only.

Every test runs against the synthetic seed, pinned explicitly: the service's own
default would pick up a locally published artifact if one exists, and a test
suite whose answer depends on what is lying around in data/ tests nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import optimizer.app as app_module
from optimizer.app import app
from optimizer.contract import GREEDY_VERSION, MIP_VERSION
from optimizer.greedy import MAX_PER_CLUB, SQUAD_QUOTA
from optimizer.pool import ArtifactXPProvider, FixtureXPProvider

SEED = Path(__file__).parent / "seed_xp.json"
SEED_PROVIDER = FixtureXPProvider(SEED)
SNAPSHOT = SEED_PROVIDER.snapshot("2026-27")
PREMIUM_ID = 112  # owned by 64.8% in the seed data
REPO_ROOT = Path(__file__).resolve().parents[2]

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_provider_and_cold_cache(monkeypatch):
    monkeypatch.setattr(app_module, "provider", SEED_PROVIDER)
    # A narrower pool than production's 150 keeps the suite fast. The seed's
    # cheapest-possible squads invite ten-transfer rebuilds, which is the MIP's
    # hardest case; nothing asserted here depends on the 41st-best player.
    monkeypatch.setattr(app_module, "POOL_PER_POSITION", 40)
    # Each test starts from a cold cache, so a cache hit is never mistaken for a
    # correct answer computed twice.
    app_module._cache.clear()
    yield
    app_module._cache.clear()


def squad_players(preferred_club: int | None = None):
    picked = []
    clubs: dict[int, int] = {}
    ordered = sorted(SNAPSHOT.players.values(), key=lambda p: (p.price, p.element_id))
    for etype, need in SQUAD_QUOTA.items():
        have = 0
        pool = ordered
        if preferred_club is not None:
            pool = sorted(ordered, key=lambda p: (p.team_fpl_id != preferred_club,))
        for p in pool:
            if p.element_type != etype or clubs.get(p.team_fpl_id, 0) >= MAX_PER_CLUB:
                continue
            picked.append(p)
            clubs[p.team_fpl_id] = clubs.get(p.team_fpl_id, 0) + 1
            have += 1
            if have == need:
                break
    return picked


def make_request(squad=None, selling=None, **overrides):
    squad = squad if squad is not None else squad_players()
    selling = selling or {}
    body = {
        "squad": [
            {
                "element_id": p.element_id,
                "selling_price": selling.get(p.element_id, p.price),
                "purchase_price": p.price,
            }
            for p in squad
        ],
        "bank": 200,
        "free_transfers": 1,
        "current_gw": 6,
        "horizon": 3,
        "season": "2026-27",
    }
    body.update(overrides)
    return body


# --- /health ----------------------------------------------------------------


def test_health_reports_the_loaded_snapshot():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["solver_version"] == MIP_VERSION
    assert body["season"] == SNAPSHOT.season
    assert body["model_version"] == SNAPSHOT.model_version
    assert body["generated_at"] == SNAPSHOT.generated_at
    assert body["data_as_of"] == SNAPSHOT.generated_at
    assert body["players_loaded"] == len(SNAPSHOT.players)


def test_health_says_loudly_when_the_data_is_synthetic():
    body = client.get("/health").json()
    assert body["synthetic"] is True
    assert "SYNTHETIC SEED" in body["source"]
    assert "SYNTHETIC SEED" in body["warning"]


def test_health_reports_a_real_artifact_and_its_provenance(tmp_path, monkeypatch):
    path = write_artifact(tmp_path)
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(path, expected_season="2026-27"))
    body = client.get("/health").json()
    assert body["synthetic"] is False
    assert "warning" not in body
    assert body["source"] == str(path)
    assert body["as_of_gw"] == 5
    assert body["target_gws"] == [6, 7, 8, 9, 10]
    assert body["ingest_run_id"] == 42
    assert body["model_version"] == "artifact-test-0.1"


def test_health_is_503_not_a_crash_when_the_artifact_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(tmp_path / "missing.json"))
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert "missing.json" in r.json()["error"]


# --- /optimize: the MIP ----------------------------------------------------


def test_optimize_returns_ranked_mip_plans_including_a_hold():
    r = client.post("/optimize", json=make_request())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["solver_version"] == MIP_VERSION
    assert body["season"] == "2026-27"
    assert body["baseline_xp"] > 0
    assert 2 <= len(body["plans"]) <= 3
    labels = [p["label"] for p in body["plans"]]
    assert "Hold" in labels

    hold = next(p for p in body["plans"] if p["label"] == "Hold")
    assert hold["transfers_in"] == [] and hold["transfers_out"] == []
    assert hold["bank_after"] == 200
    assert len(hold["xi"]) == 11
    assert len(hold["bench_order"]) == 4
    assert hold["captain"] in hold["xi"]
    assert hold["vice_captain"] in hold["xi"]

    assert all(p["reasoning"] for p in body["plans"])
    assert body["model_version"] == SNAPSHOT.model_version
    assert body["solve_ms"] >= 0
    assert body["truncated"] is False


def test_breakdown_carries_real_fixture_counts():
    # The solver refuses to invent one fixture per player; the service has to
    # pass the counts through, and they have to be the snapshot's.
    body = client.post("/optimize", json=make_request(current_gw=6, horizon=5)).json()
    seen_blank_or_double = False
    for plan in body["plans"]:
        for gw in plan["per_gw_breakdown"]:
            assert gw["n_fixtures"], "n_fixtures must not be empty"
            for eid, n in gw["n_fixtures"].items():
                assert n == SNAPSHOT.players[int(eid)].fixtures(gw["gw"])
                seen_blank_or_double |= n != 1
    assert seen_blank_or_double, "the seed has blanks and doubles; none surfaced"


def test_transfers_out_are_priced_at_the_requests_selling_price_not_list():
    squad = squad_players()
    # Every player sells for 0.2m less than list: bought 0.4m ago, and FPL lets
    # the seller keep only half of a rise. The MIP itself reports list price.
    selling = {p.element_id: p.price - 2 for p in squad}
    bank = 200
    body = client.post("/optimize", json=make_request(squad, selling=selling, bank=bank)).json()
    assert body["solver_version"] == MIP_VERSION

    moved = [p for p in body["plans"] if p["transfers_out"]]
    assert moved, "a cheapest-possible squad with 20.0m in the bank must have a move"
    for plan in moved:
        for t in plan["transfers_out"]:
            assert t["price"] == selling[t["element_id"]]
            assert t["price"] != SNAPSHOT.players[t["element_id"]].price
        for t in plan["transfers_in"]:
            assert t["price"] == SNAPSHOT.players[t["element_id"]].price
        expected_bank = (
            bank
            + sum(t["price"] for t in plan["transfers_out"])
            - sum(t["price"] for t in plan["transfers_in"])
        )
        assert plan["bank_after"] == expected_bank
        assert plan["bank_after"] >= 0


def test_recommended_transfers_stay_inside_budget():
    squad = squad_players()
    selling = {p.element_id: p.price for p in squad}
    bank = 30
    r = client.post("/optimize", json=make_request(squad, bank=bank))
    for plan in r.json()["plans"]:
        spent = sum(t["price"] for t in plan["transfers_in"])
        freed = sum(selling[t["element_id"]] for t in plan["transfers_out"])
        assert spent <= freed + bank


def test_max_ownership_excludes_a_heavily_owned_player():
    squad = squad_players()
    body = make_request(squad, bank=300)

    wide = client.post("/optimize", json=body).json()
    bought = {t["element_id"] for p in wide["plans"] for t in p["transfers_in"]}
    assert PREMIUM_ID in bought  # he is the obvious pick when ownership is ignored

    app_module._cache.clear()
    body["max_ownership"] = 40.0
    narrow = client.post("/optimize", json=body).json()
    bought = {t["element_id"] for p in narrow["plans"] for t in p["transfers_in"]}
    assert PREMIUM_ID not in bought
    assert bought, "differential mode still has to recommend somebody"


def test_solver_time_limit_is_passed_through(monkeypatch):
    seen = {}
    real = app_module.solver.solve

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.solver, "solve", spy)
    monkeypatch.setattr(app_module, "SOLVER_TIME_LIMIT_S", 7.5)
    client.post("/optimize", json=make_request())
    assert seen["time_limit"] == 7.5
    assert seen["fixtures"], "fixture counts must reach the solver"


def test_truncated_comes_from_the_solver(monkeypatch):
    real = app_module.solver.solve

    def truncating(*args, **kwargs):
        result = real(*args, **kwargs)
        return type(result)(
            plans=result.plans,
            baseline_xp=result.baseline_xp,
            truncated=True,
            solve_ms=result.solve_ms,
        )

    monkeypatch.setattr(app_module.solver, "solve", truncating)
    body = client.post("/optimize", json=make_request()).json()
    assert body["truncated"] is True
    assert body["solver_version"] == MIP_VERSION


# --- /optimize: the greedy fallback ------------------------------------------


def test_mip_failure_falls_back_to_greedy_and_says_so(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("CBC exploded")

    monkeypatch.setattr(app_module.solver, "solve", broken)
    squad = squad_players()
    selling = {p.element_id: p.price - 1 for p in squad}
    with caplog.at_level("ERROR", logger="optimizer"):
        r = client.post("/optimize", json=make_request(squad, selling=selling))
    assert r.status_code == 200, r.text
    body = r.json()

    # The label is the whole point: a greedy answer stamped as MIP corrupts the
    # recommendation log.
    assert body["solver_version"] == GREEDY_VERSION
    assert body["truncated"] is False
    assert "Hold" in [p["label"] for p in body["plans"]]
    for plan in body["plans"]:
        for t in plan["transfers_out"]:
            assert t["price"] == selling[t["element_id"]]
    assert "CBC exploded" in caplog.text

    # A fallback is never cached: the next identical request tries the MIP again.
    assert len(app_module._cache) == 0
    monkeypatch.undo()
    monkeypatch.setattr(app_module, "provider", SEED_PROVIDER)
    again = client.post("/optimize", json=make_request(squad, selling=selling)).json()
    assert again["solver_version"] == MIP_VERSION


# --- /optimize: refusals --------------------------------------------------


def test_data_as_of_is_the_snapshot_time_not_now():
    # A recommendation computed at 22:00 can be invalid at 02:00, so it carries
    # the as-of time of the data it used rather than the time it was computed.
    r = client.post("/optimize", json=make_request())
    assert r.json()["data_as_of"] == SNAPSHOT.generated_at
    assert r.json()["data_as_of"] == "2026-09-16T05:04:11Z"


def test_fourteen_player_squad_is_rejected():
    body = make_request()
    body["squad"] = body["squad"][:-1]
    r = client.post("/optimize", json=body)
    assert r.status_code == 422


def test_broken_composition_is_rejected_by_name():
    squad = squad_players()
    spare_def = next(
        p for p in SNAPSHOT.players.values() if p.element_type == 2 and p not in squad
    )
    broken = [p for p in squad if p.element_type != 4] + [spare_def] + [
        p for p in squad if p.element_type == 4
    ][:2]
    r = client.post("/optimize", json=make_request(broken))
    assert r.status_code == 422
    assert "6 defenders, expected 5" in r.json()["detail"]


def test_four_from_one_club_is_rejected_by_name():
    squad = squad_players(preferred_club=5)
    victim = next(p for p in squad if p.team_fpl_id != 5)
    fourth = next(
        p
        for p in SNAPSHOT.players.values()
        if p.team_fpl_id == 5 and p.element_type == victim.element_type and p not in squad
    )
    broken = [p for p in squad if p is not victim] + [fourth]
    r = client.post("/optimize", json=make_request(broken))
    assert r.status_code == 422
    assert "more than 3 players from one club" in r.json()["detail"]
    assert "team 5" in r.json()["detail"]


def test_unknown_element_id_is_rejected():
    body = make_request()
    body["squad"][0]["element_id"] = 999999
    r = client.post("/optimize", json=body)
    assert r.status_code == 422
    assert "999999" in r.json()["detail"]


def test_request_for_another_season_is_refused():
    r = client.post("/optimize", json=make_request(season="2025-26"))
    assert r.status_code == 422
    assert "2025-26" in r.json()["detail"] and "2026-27" in r.json()["detail"]


def test_gameweek_the_artifact_does_not_cover_is_refused(tmp_path, monkeypatch):
    path = write_artifact(tmp_path)  # covers GW6-10
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(path, expected_season="2026-27"))
    r = client.post("/optimize", json=make_request(current_gw=12))
    assert r.status_code == 422
    assert "GW6-10" in r.json()["detail"]

    # A horizon running past the artifact's last week is clipped, not zero-filled.
    body = client.post("/optimize", json=make_request(current_gw=8, horizon=5)).json()
    assert [b["gw"] for b in body["plans"][0]["per_gw_breakdown"]] == [8, 9, 10]
    assert body["solver_version"] == MIP_VERSION


def test_unreadable_artifact_is_a_503_naming_it(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(tmp_path / "gone.json"))
    r = client.post("/optimize", json=make_request())
    assert r.status_code == 503
    assert "gone.json" in r.json()["detail"]


def test_horizon_beyond_the_season_is_clipped_not_rejected():
    r = client.post("/optimize", json=make_request(current_gw=37, horizon=5))
    assert r.status_code == 200
    gws = [b["gw"] for b in r.json()["plans"][0]["per_gw_breakdown"]]
    assert gws == [37, 38]


def test_horizon_above_the_cap_is_rejected_by_the_contract():
    # MAX_HORIZON is 5: beyond that the fixture information is too noisy.
    r = client.post("/optimize", json=make_request(horizon=8))
    assert r.status_code == 422


# --- Cache ----------------------------------------------------------------


def test_identical_requests_get_an_identical_answer_from_the_cache():
    body = make_request()
    first = client.post("/optimize", json=body).json()
    assert len(app_module._cache) == 1

    second = client.post("/optimize", json=body).json()
    assert second == first
    assert len(app_module._cache) == 1  # served from cache, not solved again

    # A different bank is a different question and must not hit that entry.
    other = make_request(bank=5)
    client.post("/optimize", json=other)
    assert len(app_module._cache) == 2


def test_a_new_artifact_invalidates_the_cache(tmp_path, monkeypatch):
    first_path = write_artifact(tmp_path / "a", generated_at="2026-09-20T05:00:00Z")
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(first_path))
    body = make_request()
    client.post("/optimize", json=body)
    assert len(app_module._cache) == 1

    # Same model version, newer data: prices may have moved overnight.
    newer = write_artifact(tmp_path / "b", generated_at="2026-09-21T05:00:00Z")
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(newer))
    r = client.post("/optimize", json=body).json()
    assert r["data_as_of"] == "2026-09-21T05:00:00Z"
    assert len(app_module._cache) == 2

    # And a new model version over the same data is a different answer too.
    other_model = write_artifact(
        tmp_path / "c", generated_at="2026-09-21T05:00:00Z", model_version="artifact-test-0.2"
    )
    monkeypatch.setattr(app_module, "provider", ArtifactXPProvider(other_model))
    client.post("/optimize", json=body)
    assert len(app_module._cache) == 3


# --- Statelessness --------------------------------------------------------


def test_service_imports_and_serves_with_no_database_credentials(tmp_path):
    """CLAUDE.md invariant 5. Run in a clean interpreter so nothing this test
    process has already imported (the ingest suite loads psycopg and .env) can
    make it pass by accident."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["OPTIMIZER_XP_PATH"] = str(write_artifact(tmp_path))
    code = (
        "import sys, os\n"
        "assert 'DATABASE_URL' not in os.environ\n"
        "from fastapi.testclient import TestClient\n"
        "import optimizer.app as m\n"
        "c = TestClient(m.app)\n"
        "h = c.get('/health').json()\n"
        "assert h['ok'] and not h['synthetic'], h\n"
        "banned = [x for x in ('psycopg', 'psycopg2', 'dotenv', 'db', 'config') if x in sys.modules]\n"
        "assert not banned, banned\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# --- Helpers --------------------------------------------------------------


def write_artifact(
    directory: Path,
    *,
    generated_at: str = "2026-09-20T05:00:00Z",
    model_version: str = "artifact-test-0.1",
) -> Path:
    """The seed, re-shaped as a publish_xp artifact covering GW6-10."""
    seed = json.loads(SEED.read_text())
    gws = ["6", "7", "8", "9", "10"]
    doc = {
        "format": 1,
        "season": "2026-27",
        "model_version": model_version,
        "generated_at": generated_at,
        "as_of_gw": 5,
        "target_gws": [int(g) for g in gws],
        "ingest_run_id": 42,
        "players": [
            {
                **{k: v for k, v in p.items() if k not in ("xp", "n_fixtures")},
                "status": "a",
                "xp": {g: p["xp"][g] for g in gws},
                "n_fixtures": {g: p["n_fixtures"][g] for g in gws},
            }
            for p in seed["players"]
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "xp.json"
    path.write_text(json.dumps(doc))
    return path
