"""Tests for the fitted expected-points model (`fitted-0.1`).

Offline and synthetic: a seeded six-club league over two seasons, generated
with the real scoring rules, so the model has something to fit and the answers
are checkable. Two planted players carry the edge cases:

* OUTLIER — a midfielder whose only minutes are one 30-minute cameo with 1.5 xG.
  A raw per-90 rate would make him the best attacker in the league.
* VETERAN — a regular starter last season who moves club over the summer and
  gets a new element id. At gameweek 1 his only evidence is last season's.

Structural properties (blank = 0, double = two singles, components sum to xp,
unavailable = 0) are tested by editing a real `AsOf`'s fixtures, so the rest of
the model runs exactly as in the backtest.
"""

from __future__ import annotations

import dataclasses
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data import (  # noqa: E402
    FIXTURE_COLUMNS,
    PLAYER_IDENTITY_COLUMNS,
    STAT_COLUMNS,
    TEAM_IDENTITY_COLUMNS,
    History,
    Tables,
)
from backtest.harness import default_targets, run_backtest  # noqa: E402
from backtest.model_fitted import FittedModel  # noqa: E402
from xp import fitted  # noqa: E402
from xp.fitted import COMPONENT_KEYS, MODEL_VERSION, FittedXPModel, xp_rows  # noqa: E402
from xp.fitted_glm import fit_glm, poisson_floor_mean  # noqa: E402
from xp.fitted_live import live_asof, live_availability  # noqa: E402

S1, S2 = "2024-25", "2025-26"
N_TEAMS = 6
N_GW = 10
OUTLIER, VETERAN = 9001, 9002

# Squad template per club: (position, role). Starters start most weeks; bench
# players rarely start and sometimes come on.
SQUAD = (
    [(1, "start"), (1, "bench")]
    + [(2, "start")] * 4
    + [(2, "bench")]
    + [(3, "start")] * 4
    + [(3, "bench")] * 2
    + [(4, "start")] * 2
    + [(4, "bench")]
)
XG90 = {1: 0.0, 2: 0.06, 3: 0.2, 4: 0.45}
XA90 = {1: 0.01, 2: 0.07, 3: 0.15, 4: 0.12}
DC90 = {1: 0.0, 2: 9.0, 3: 8.0, 4: 3.0}
GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}


def _schedule() -> list[list[tuple[int, int]]]:
    """Circle-method round robin for 6 clubs, twice, venues swapped."""
    teams = list(range(1, N_TEAMS + 1))
    rounds = []
    for r in range(N_TEAMS - 1):
        pairs = [(teams[i], teams[-1 - i]) for i in range(N_TEAMS // 2)]
        rounds.append(pairs)
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return rounds + [[(a, h) for h, a in pairs] for pairs in rounds]


def _points(pos, minutes, goals, assists, cs, conceded, saves, yc, bonus, dc, season) -> int:
    pts = (2 if minutes >= 60 else 1 if minutes > 0 else 0)
    pts += goals * GOAL_PTS[pos] + 3 * assists + cs * CS_PTS[pos] + saves // 3 - yc + bonus
    if pos in (1, 2):
        pts -= conceded // 2
    if season >= "2025-26" and pos in fitted.DEFCON_THRESHOLD and dc >= fitted.DEFCON_THRESHOLD[pos]:
        pts += 2
    return pts


def build_tables(seed: int = 7) -> Tables:
    rng = np.random.default_rng(seed)
    players, stats, fixtures, teams = [], [], [], []
    strength = {t: 0.7 + 0.12 * t for t in range(1, N_TEAMS + 1)}

    # Rosters: codes are stable, element ids are renumbered each season.
    roster: dict[str, dict[int, list[tuple[int, int, str, int]]]] = {}
    for si, season in enumerate((S1, S2)):
        roster[season] = {}
        eid = 1
        for t in range(1, N_TEAMS + 1):
            teams.append({"season": season, "fpl_id": t, "code": 100 + t, "name": f"Club{t}", "short_name": f"C{t}"})
            members = []
            for slot, (pos, role) in enumerate(SQUAD):
                code = 1000 + t * 100 + slot
                members.append((code, eid, pos, role))
                players.append({"season": season, "element_id": eid, "code": code, "element_type": pos,
                                "web_name": f"P{code}", "first_name": "P", "second_name": str(code)})
                eid += 1
            roster[season][t] = members
        # VETERAN: starting forward at club 2 in S1, moves to club 3 for S2.
        vt = 2 if season == S1 else 3
        roster[season][vt].append((VETERAN, eid, 4, "start"))
        players.append({"season": season, "element_id": eid, "code": VETERAN, "element_type": 4,
                        "web_name": "Veteran", "first_name": "V", "second_name": "Veteran"})
        eid += 1
        if season == S2:
            roster[season][1].append((OUTLIER, eid, 3, "outlier"))
            players.append({"season": season, "element_id": eid, "code": OUTLIER, "element_type": 3,
                            "web_name": "Outlier", "first_name": "O", "second_name": "Outlier"})
            eid += 1

        fid = 1 + si * 100
        for gw, pairs in enumerate(_schedule(), start=1):
            for home, away in pairs:
                lines = {}
                for side, opp, is_home in ((home, away, True), (away, home, False)):
                    rows = []
                    for code, element_id, pos, role in roster[season][side]:
                        start = rng.random() < (0.92 if role == "start" else 0.06)
                        minutes = 0
                        if role == "outlier":
                            start, minutes = False, (30 if (season, gw) == (S2, 3) else 0)
                        elif start:
                            minutes = 90 if rng.random() < 0.7 else int(rng.integers(55, 90))
                        elif role == "bench" and pos != 1 and rng.random() < 0.4:
                            minutes = int(rng.integers(5, 35))
                        att = strength[side] / strength[opp] * (1.1 if is_home else 0.9)
                        xg = XG90[pos] * minutes / 90 * att * rng.uniform(0.3, 1.7)
                        xa = XA90[pos] * minutes / 90 * att * rng.uniform(0.3, 1.7)
                        if code == OUTLIER and minutes:
                            xg, xa = 1.5, 0.1
                        goals = int(rng.poisson(xg)) if code != OUTLIER else int(minutes > 0)
                        rows.append(dict(code=code, element_id=element_id, pos=pos, start=int(start and minutes > 0),
                                         minutes=minutes, xg=round(xg, 2), xa=round(xa, 2), goals=goals,
                                         assists=int(rng.poisson(xa * 1.3)),
                                         saves=int(rng.poisson(3 * minutes / 90)) if pos == 1 else 0,
                                         yc=int(rng.random() < 0.08 * minutes / 90),
                                         dc=int(rng.poisson(DC90[pos] * minutes / 90)),
                                         is_home=is_home, opp=opp))
                    lines[side] = rows
                score = {side: sum(r["goals"] for r in rows) for side, rows in lines.items()}
                fixtures.append({"season": season, "fixture_id": fid, "gw": gw, "team_h_fpl_id": home,
                                 "team_a_fpl_id": away, "team_h_difficulty": 3, "team_a_difficulty": 3,
                                 "kickoff_time": None, "started": True, "finished": True,
                                 "finished_provisional": True, "minutes": 90,
                                 "team_h_score": score[home], "team_a_score": score[away]})
                for side, rows in lines.items():
                    conceded = score[away if side == home else home]
                    for r in rows:
                        r["bps"] = 5 * (r["minutes"] > 0) + 12 * r["goals"] + 9 * r["assists"] + r["minutes"] // 10
                    for r in rows:
                        cs = int(conceded == 0 and r["minutes"] >= 60 and r["pos"] != 4)
                        ranked = sorted((x["bps"] for x in rows), reverse=True)
                        bonus = 3 if r["bps"] == ranked[0] and r["minutes"] else 2 if r["bps"] == ranked[1] and r["minutes"] else 0
                        stats.append({
                            "season": season, "element_id": r["element_id"], "gw": gw, "fixture_id": fid,
                            "minutes": r["minutes"],
                            "total_points": _points(r["pos"], r["minutes"], r["goals"], r["assists"], cs,
                                                    conceded if r["minutes"] else 0, r["saves"], r["yc"],
                                                    bonus, r["dc"], season),
                            "starts": r["start"], "goals_scored": r["goals"], "assists": r["assists"],
                            "clean_sheets": cs, "goals_conceded": conceded if r["minutes"] else 0,
                            "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
                            "yellow_cards": r["yc"], "red_cards": 0, "saves": r["saves"], "bonus": bonus,
                            "bps": r["bps"], "defensive_contribution": r["dc"], "expected_goals": r["xg"],
                            "expected_assists": r["xa"], "expected_goal_involvements": r["xg"] + r["xa"],
                            "expected_goals_conceded": 1.3, "influence": 0.0, "creativity": 0.0,
                            "threat": 0.0, "ict_index": 0.0, "was_home": r["is_home"],
                            "opponent_team_fpl_id": r["opp"],
                            "value_tenths": {1: 45, 2: 48, 3: 60, 4: 70}[r["pos"]] + (10 if r["start"] else 0),
                            "selected_by": 1000, "fpl_xp": None, "source": "history",
                        })
                fid += 1

    return Tables(
        stats=pd.DataFrame(stats, columns=list(STAT_COLUMNS)),
        players=pd.DataFrame(players, columns=list(PLAYER_IDENTITY_COLUMNS)),
        fixtures=pd.DataFrame(fixtures, columns=list(FIXTURE_COLUMNS)),
        teams=pd.DataFrame(teams, columns=list(TEAM_IDENTITY_COLUMNS)),
    )


@pytest.fixture(scope="module")
def history() -> History:
    return History(build_tables())


@pytest.fixture(scope="module")
def asof(history):
    return history.asof(S2, 8)


@pytest.fixture(scope="module")
def model(asof) -> FittedXPModel:
    m = FittedXPModel()
    m.fit(asof)
    return m


def _by_id(results):
    return {r.element_id: r for r in results}


def _element(asof, code: int) -> int:
    p = asof.players
    return int(p.loc[p["code"] == code, "element_id"].iloc[0])


def _with_fixtures(asof, fixtures: pd.DataFrame):
    """The same view with the target gameweek's fixtures replaced."""
    f = asof.fixtures
    keep = f[~((f["season"] == asof.season) & (f["gw"] == asof.gw))]
    new = pd.concat([keep, fixtures], ignore_index=True)
    teams = pd.concat([fixtures["team_h_fpl_id"], fixtures["team_a_fpl_id"]]).value_counts()
    players = asof.players.copy()
    players["fixture_count"] = players["team_fpl_id"].map(teams).fillna(0).astype("int64")
    return dataclasses.replace(asof, fixtures=new, players=players)


# --- Structure -----------------------------------------------------------------


def test_blank_is_exactly_zero(asof, model):
    target = asof.target_fixtures()
    blanked_team = int(target["team_h_fpl_id"].iloc[0])
    edited = _with_fixtures(
        asof,
        target[(target["team_h_fpl_id"] != blanked_team) & (target["team_a_fpl_id"] != blanked_team)],
    )
    results = model.predict(edited)
    on_team = set(edited.players.loc[edited.players["team_fpl_id"] == blanked_team, "element_id"])
    assert on_team
    for r in results:
        if r.element_id in on_team:
            assert r.xp == 0.0 and r.fixture_count == 0 and r.xmins == 0.0
            assert all(v == 0.0 for v in r.components.values())
    assert any(r.xp > 0 for r in results if r.element_id not in on_team)


def test_double_is_the_sum_of_its_two_fixtures(asof, model):
    target = asof.target_fixtures()
    first, second = target.iloc[0], target.iloc[1]
    team = int(first["team_h_fpl_id"])
    # Club `team` plays its own fixture plus a second one at home to another side.
    extra = second.copy()
    extra["fixture_id"] = 999
    extra["team_h_fpl_id"] = team
    single_a = model.predict(_with_fixtures(asof, target))
    single_b = model.predict(_with_fixtures(asof, pd.DataFrame([extra])))
    double = model.predict(_with_fixtures(asof, pd.concat([target, pd.DataFrame([extra])])))
    a, b, d = _by_id(single_a), _by_id(single_b), _by_id(double)
    members = asof.players.loc[asof.players["team_fpl_id"] == team, "element_id"]
    for e in members:
        assert d[e].fixture_count == 2
        assert d[e].xp == pytest.approx(a[e].xp + b[e].xp, abs=0.01)
        # Probabilities stay per fixture; minutes add up.
        assert d[e].p_start == pytest.approx(a[e].p_start, abs=1e-4)
        assert d[e].xmins == pytest.approx(a[e].xmins + b[e].xmins, abs=0.02)


def test_components_sum_to_xp(asof, model):
    results = model.predict(asof)
    assert results
    for r in results:
        assert set(r.components) == set(COMPONENT_KEYS)
        assert sum(r.components.values()) == pytest.approx(r.xp, abs=1e-9)


def test_unavailable_player_gets_zero(asof, model):
    results = _by_id(model.predict(asof))
    star = max((r for r in results.values()), key=lambda r: r.xp)
    assert star.xp > 2.0
    out = _by_id(model.predict(asof, availability={star.element_id: 0.0}))[star.element_id]
    assert out.xp == pytest.approx(0.0, abs=1e-9)
    assert out.p_play == 0.0
    doubtful = _by_id(model.predict(asof, availability={star.element_id: 0.5}))[star.element_id]
    assert 0.35 * star.xp < doubtful.xp < 0.65 * star.xp


def test_availability_from_live_flags():
    assert fitted.availability_from_status("a", None) == 1.0
    assert fitted.availability_from_status("i", None) == 0.0
    assert fitted.availability_from_status("d", 75) == 0.75
    assert fitted.availability_from_status("d", None) == 0.5


# --- Fitted behaviour -------------------------------------------------------------


def test_shrinkage_pulls_a_30_minute_outlier_toward_the_prior(asof, model):
    params = model.params
    panel = fitted._panel(asof, [asof.gw], fitted.DEFAULT_CONFIG)
    tp = panel.pgw[panel.pgw["is_target"]].set_index("element_id")
    state = fitted.player_state(tp, params)
    row = tp.loc[_element(asof, OUTLIER)]
    shrunk = float(state.loc[_element(asof, OUTLIER), "xg90"])
    raw = float(row["r_xg_adj"] / row["r_min90"])  # 1.5 xG in a third of a match
    log_price, _ = fitted._log_price(tp.loc[[_element(asof, OUTLIER)]])
    prior = float(params.rate_priors["xg"][3].predict(log_price[:, None])[0])
    assert raw > 3.0
    assert prior < shrunk < raw
    assert abs(shrunk - prior) < abs(raw - shrunk)
    # And the headline follows: a cameo player is not projected like a star.
    xp = _by_id(model.predict(asof))[_element(asof, OUTLIER)].xp
    assert xp < 1.5


def test_last_season_history_alone_gives_a_sensible_prediction(history):
    gw1 = history.asof(S2, 1)
    assert gw1.season_stats().empty  # nothing yet this season
    m = FittedXPModel()
    m.fit(gw1)
    results = _by_id(m.predict(gw1))
    vet = results[_element(gw1, VETERAN)]
    assert vet.fixture_count == 1
    # A regular starter last season: likely to start, a few points, not a haul.
    assert vet.p_start > 0.6
    assert 1.5 < vet.xp < 8.0
    # And the history really was last season's, under this season's element id.
    ph = gw1.player_history
    assert set(ph.loc[ph["element_id"] == vet.element_id, "season"]) == {S1}


def test_fitted_parameters_are_finite_and_plausible(model):
    d = model.params.describe()

    def numbers(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from numbers(v)
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            yield float(x)

    assert all(math.isfinite(v) for v in numbers(d))
    minutes = d["minutes"]
    for p, q in minutes["p60_given_start"].items():
        assert 0.5 < q <= 1.0
        assert 55 < minutes["minutes_given_start"][p] <= 95
    for p in (2, 3, 4):
        assert 0.2 < d["goal_conversion"][p] < 5.0 or d["goal_conversion"][p] == 0.0
        assert 0.2 < d["assist_conversion"][p] < 5.0
    assert 0.5 <= d["clean_sheet_dispersion"] <= 1.5
    assert 0.8 < d["diagnostics"]["home_advantage"] < 1.5
    # Start probability rises with the recent start rate.
    assert d["p_start"]["start_rate_slow"] + d["p_start"]["start_rate_fast"] > 0
    # 2025-26 data exists here, so defensive contribution was fitted, and a
    # higher expected count means a higher chance of the threshold.
    assert d["defensive_contribution"][2]["log_expected_count"] > 0


def test_defensive_contribution_is_off_before_2025_26(history):
    a = history.asof(S1, 9)
    m = FittedXPModel()
    m.fit(a)
    assert m.params.defcon == {}
    assert all(r.components["defensive_contribution"] == 0.0 for r in m.predict(a))


# --- Interfaces -------------------------------------------------------------------


def test_backtest_adapter_runs_through_the_harness(history):
    targets = [t for t in default_targets(history, [S2]) if t.gw >= 7]
    result = run_backtest(history, [FittedModel()], targets, refit_every=2)
    stats = result.stats["fitted"]
    assert stats.blank_violations == 0 and stats.non_finite == 0 and stats.missing == 0
    assert stats.fits == 2


def test_xp_rows_match_the_xpoints_table_shape(asof, model):
    rows = xp_rows(asof, model, [asof.gw])
    assert rows
    assert set(rows[0]) == {
        "season", "element_id", "gw", "model_version", "xp", "xmins", "p_start",
        "p_play", "components", "as_of_gw", "fixture_count",
    }
    assert {r["model_version"] for r in rows} == {MODEL_VERSION}
    assert {r["as_of_gw"] for r in rows} == {asof.gw - 1}


def test_live_asof_projects_future_gameweeks_with_live_availability(history):
    """Production: no stat rows exist at the target, the player list is today's."""
    p = history.players[history.players["season"] == S2]
    club = history.registered(S2, 5).set_index("element_id")["team_fpl_id"]
    live = pd.DataFrame({
        "element_id": p["element_id"],
        "code": p["code"],
        "element_type": p["element_type"],
        "web_name": p["web_name"],
        "first_name": p["first_name"],
        "second_name": p["second_name"],
        "team_fpl_id": p["element_id"].map(club),
        "now_cost_tenths": 55,
        "status": "a",
        "chance_of_playing_next_round": None,
    }).dropna(subset=["team_fpl_id"])
    injured = int(live["element_id"].iloc[0])
    live.loc[live["element_id"] == injured, "status"] = "i"
    # Pretend gameweeks 6+ have not been played: drop their stat rows.
    trimmed = History(dataclasses.replace(
        build_tables(),
        stats=build_tables().stats.query("not (season == @S2 and gw >= 6)"),
    ))
    asof = live_asof(trimmed, S2, 6, live)
    assert asof.stats["gw"][asof.stats["season"] == S2].max() == 5
    m = FittedXPModel()
    m.fit(asof)
    rows = xp_rows(asof, m, [6, 7], live_availability(live))
    assert {r["gw"] for r in rows} == {6, 7}
    assert all(r["as_of_gw"] == 5 for r in rows)
    by = {(r["element_id"], r["gw"]): r for r in rows}
    assert by[(injured, 6)]["xp"] == 0.0
    assert any(r["xp"] > 2 for r in rows)


# --- Numerics ---------------------------------------------------------------------


def test_poisson_floor_mean_matches_simulation():
    rng = np.random.default_rng(0)
    for lam, per in ((0.5, 2), (1.8, 2), (3.2, 3), (6.0, 3)):
        sim = (rng.poisson(lam, 200_000) // per).mean()
        assert poisson_floor_mean(np.array([lam]), per)[0] == pytest.approx(sim, abs=0.01)


def test_glm_recovers_known_coefficients():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(20_000, 2))
    p = 1 / (1 + np.exp(-(-0.5 + 1.2 * x[:, 0] - 0.7 * x[:, 1])))
    glm = fit_glm(x, (rng.random(20_000) < p).astype(float), ("a", "b"), "logit", ridge=0.0)
    coef = glm.raw_coefficients()
    assert coef["intercept"] == pytest.approx(-0.5, abs=0.06)
    assert coef["a"] == pytest.approx(1.2, abs=0.06)
    assert coef["b"] == pytest.approx(-0.7, abs=0.06)
    rate = np.exp(0.3 + 0.8 * x[:, 0])
    glm = fit_glm(x[:, :1], rng.poisson(rate), ("a",), "log", ridge=0.0)
    assert glm.raw_coefficients()["a"] == pytest.approx(0.8, abs=0.03)
