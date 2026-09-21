"""POST /squad/ideal — the team from scratch and the wildcard, over HTTP.

The money rules and the freeze are pinned by hand in test_solve_squad.py; these
check the service around them: validation, the from-scratch versus wildcard
split, caching, and that an impossible budget is a 422 with a reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import optimizer.app as app_module
from optimizer.app import app
from optimizer.contract import MIP_VERSION
from optimizer.greedy import MAX_PER_CLUB, SQUAD_QUOTA
from optimizer.pool import FixtureXPProvider

SEED_PROVIDER = FixtureXPProvider(Path(__file__).parent / "seed_xp.json")
SNAPSHOT = SEED_PROVIDER.snapshot("2026-27")
client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_provider_and_cold_cache(monkeypatch):
    monkeypatch.setattr(app_module, "provider", SEED_PROVIDER)
    monkeypatch.setattr(app_module, "POOL_PER_POSITION", 40)
    app_module._cache.clear()
    yield
    app_module._cache.clear()


def cheapest_squad():
    picked, clubs = [], {}
    for etype, need in SQUAD_QUOTA.items():
        have = 0
        for p in sorted(SNAPSHOT.players.values(), key=lambda p: (p.price, p.element_id)):
            if p.element_type != etype or clubs.get(p.team_fpl_id, 0) >= MAX_PER_CLUB:
                continue
            picked.append(p)
            clubs[p.team_fpl_id] = clubs.get(p.team_fpl_id, 0) + 1
            have += 1
            if have == need:
                break
    return picked


def assert_legal(body):
    players = body["players"]
    assert len(players) == 15 and len({p["element_id"] for p in players}) == 15
    by_pos, clubs = {}, {}
    for p in players:
        by_pos[p["element_type"]] = by_pos.get(p["element_type"], 0) + 1
        clubs[p["team_fpl_id"]] = clubs.get(p["team_fpl_id"], 0) + 1
    assert by_pos == {1: 2, 2: 5, 3: 5, 4: 3}
    assert max(clubs.values()) <= MAX_PER_CLUB
    assert len(body["xi"]) == 11 and body["captain"] in body["xi"]
    assert body["cost"] + body["bank_after"] == body["budget"]
    assert body["bank_after"] >= 0


def test_a_team_from_scratch_spends_no_more_than_the_budget():
    r = client.post("/squad/ideal", json={"current_gw": 6, "horizon": 2, "budget": 1000})

    assert r.status_code == 200, r.text
    body = r.json()
    assert_legal(body)
    assert body["budget"] == 1000
    assert body["cost"] <= 1000
    assert body["solver_version"] == MIP_VERSION
    assert body["kept_count"] is None and body["gain_vs_hold"] is None
    assert all(p["cost"] == p["price"] for p in body["players"])
    assert len(body["per_gw_breakdown"]) == 2


def test_a_bigger_budget_never_projects_fewer_points():
    small = client.post("/squad/ideal", json={"current_gw": 6, "horizon": 1, "budget": 800}).json()
    large = client.post("/squad/ideal", json={"current_gw": 6, "horizon": 1, "budget": 1000}).json()

    assert large["total_xp"] >= small["total_xp"] - 1e-6


def test_an_impossible_budget_is_a_422_that_says_why():
    r = client.post("/squad/ideal", json={"current_gw": 6, "horizon": 1, "budget": 100})

    assert r.status_code == 422
    assert "budget" in r.json()["detail"]


def test_a_wildcard_uses_the_squad_and_bank_as_its_money():
    squad = cheapest_squad()
    body = {
        "current_gw": 6,
        "horizon": 2,
        "bank": 30,
        "squad": [{"element_id": p.element_id, "selling_price": p.price} for p in squad],
    }
    r = client.post("/squad/ideal", json=body)

    assert r.status_code == 200, r.text
    out = r.json()
    assert_legal(out)
    assert out["budget"] == 30 + sum(p.price for p in squad)
    assert out["kept_count"] is not None
    # Holding is always a legal wildcard, so a wildcard never loses to it.
    assert out["gain_vs_hold"] >= -1e-6


def test_a_wildcard_with_the_wrong_number_of_players_is_rejected():
    squad = cheapest_squad()[:14]
    body = {
        "current_gw": 6,
        "squad": [{"element_id": p.element_id, "selling_price": p.price} for p in squad],
    }
    r = client.post("/squad/ideal", json=body)

    assert r.status_code == 422
    assert "15" in r.json()["detail"]


def test_the_from_scratch_team_is_cached_for_every_visitor():
    """It is the same answer for everyone, so it must only be solved once."""
    request = {"current_gw": 6, "horizon": 1, "budget": 1000}
    first = client.post("/squad/ideal", json=request).json()
    calls = []
    original = app_module.solver.solve_squad
    app_module.solver.solve_squad = lambda *a, **k: calls.append(1) or original(*a, **k)
    try:
        second = client.post("/squad/ideal", json=request).json()
    finally:
        app_module.solver.solve_squad = original

    assert calls == []
    assert second == first
