"""Tests for the HTTP surface. No network, no database -- TestClient only."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import optimizer.app as app_module
from optimizer.app import app
from optimizer.contract import SOLVER_VERSION
from optimizer.greedy import MAX_PER_CLUB, SQUAD_QUOTA
from optimizer.pool import FixtureXPProvider

SEED = Path(__file__).parent / "seed_xp.json"
SNAPSHOT = FixtureXPProvider(SEED).snapshot("2026-27")
PREMIUM_ID = 112  # owned by 64.8% in the seed data

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_cache():
    # Each test starts from a cold cache, so a cache hit is never mistaken for a
    # correct answer computed twice.
    app_module._cache.clear()
    yield


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


def make_request(squad=None, **overrides):
    squad = squad if squad is not None else squad_players()
    body = {
        "squad": [
            {"element_id": p.element_id, "selling_price": p.price, "purchase_price": p.price}
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


def test_health_reports_the_loaded_snapshot():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["solver_version"] == SOLVER_VERSION
    assert body["model_version"] == SNAPSHOT.model_version
    assert body["data_as_of"] == SNAPSHOT.generated_at
    assert body["players_loaded"] == len(SNAPSHOT.players)


def test_optimize_returns_ranked_plans_including_a_hold():
    r = client.post("/optimize", json=make_request())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["baseline_xp"] > 0
    assert 2 <= len(body["plans"]) <= 3
    labels = [p["label"] for p in body["plans"]]
    assert "Hold" in labels

    hold = next(p for p in body["plans"] if p["label"] == "Hold")
    assert hold["transfers_in"] == [] and hold["transfers_out"] == []
    assert len(hold["xi"]) == 11
    assert len(hold["bench_order"]) == 4
    assert hold["captain"] in hold["xi"]
    assert hold["vice_captain"] in hold["xi"]

    deltas = [p["delta_xp"] for p in body["plans"]]
    assert deltas == sorted(deltas, reverse=True)
    assert all(p["reasoning"] for p in body["plans"])

    assert body["solver_version"] == SOLVER_VERSION
    assert body["model_version"] == SNAPSHOT.model_version
    assert body["solve_ms"] >= 0
    assert body["truncated"] is False


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


def test_recommended_transfers_stay_inside_budget():
    squad = squad_players()
    selling = {p.element_id: p.price for p in squad}
    bank = 30
    r = client.post("/optimize", json=make_request(squad, bank=bank))
    for plan in r.json()["plans"]:
        for t_in, t_out in zip(plan["transfers_in"], plan["transfers_out"]):
            assert t_in["price"] <= selling[t_out["element_id"]] + bank


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


def test_horizon_beyond_the_season_is_clipped_not_rejected():
    r = client.post("/optimize", json=make_request(current_gw=37, horizon=5))
    assert r.status_code == 200
    gws = [b["gw"] for b in r.json()["plans"][0]["per_gw_breakdown"]]
    assert gws == [37, 38]


def test_horizon_above_the_cap_is_rejected_by_the_contract():
    # MAX_HORIZON is 5: beyond that the fixture information is too noisy.
    r = client.post("/optimize", json=make_request(horizon=8))
    assert r.status_code == 422
