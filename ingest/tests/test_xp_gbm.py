"""Tests for the gradient-boosting xP model (`gbm-0.1`).

Offline and synthetic: two short seasons, four clubs, ten players each, with a
double and a blank in the second season. Values are random but seeded.

The test that matters most is the first one. A tree model is superb at finding
the one column that contains the answer, so a feature builder that lets a row
see its own result — or its double-gameweek twin's — produces a backtest that
looks brilliant and a production model that is useless. These tests plant
extreme values in the target gameweek and later and require that no feature and
no prediction moves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data import History, Tables  # noqa: E402
from backtest.model_gbm import GbmModel  # noqa: E402
from xp import gbm_features  # noqa: E402
from xp.gbm import (  # noqa: E402
    MODEL_VERSION,
    GbmConfig,
    GbmXpModel,
    availability_multiplier,
    compute_live,
    live_asof,
    xp_rows,
)
from xp.gbm_features import build_features, feature_columns  # noqa: E402

S1, S2 = "2024-25", "2025-26"
CLUBS = (1, 2, 3, 4)
PLAYERS_PER_CLUB = 10
EXTREME = 9999

# S1: six plain round-robin weeks. S2: club 4 blanks in gw3 and doubles in gw4
# (it plays club 1 twice); club 1 also doubles in gw4 as the other side.
ROUNDS = [((1, 2), (3, 4)), ((1, 3), (2, 4)), ((1, 4), (2, 3))]


def _fixtures() -> list[tuple[str, int, int, int, int]]:
    out = []
    fid = 1
    for season in (S1, S2):
        for gw in range(1, 7):
            pairs = list(ROUNDS[(gw - 1) % 3])
            if season == S2 and gw == 3:
                pairs = [(1, 2)]  # clubs 3 and 4 blank
            if season == S2 and gw == 4:
                pairs = [(1, 4), (4, 1), (2, 3)]  # clubs 1 and 4 double
            for home, away in pairs:
                out.append((season, fid, gw, home, away))
                fid += 1
    return out


FIXTURES = _fixtures()


def _player_ids(season: str) -> list[tuple[int, int, int, int]]:
    """(element_id, code, element_type, club). Element ids shift between seasons."""
    out = []
    for club in CLUBS:
        for k in range(PLAYERS_PER_CLUB):
            code = 1000 + club * 100 + k
            element_type = 1 if k == 0 else 2 if k < 4 else 3 if k < 8 else 4
            element_id = (club - 1) * PLAYERS_PER_CLUB + k + (1 if season == S1 else 101)
            out.append((element_id, code, element_type, club))
    return out


def build_tables(plant_from: tuple[str, int] | None = None, seed: int = 7) -> Tables:
    """Synthetic tables. Rows at or after `plant_from` carry EXTREME in every stat."""
    rng = np.random.default_rng(seed)

    def planted(season: str, gw: int) -> bool:
        if plant_from is None:
            return False
        s, g = plant_from
        return season > s or (season == s and gw >= g)

    stats = []
    for season, fid, gw, home, away in FIXTURES:
        for element_id, code, element_type, club in _player_ids(season):
            if club not in (home, away):
                continue
            k = code % 100
            # A stable role per player so the model has something real to learn.
            p_play = 0.95 if k in (0, 1, 2, 4, 5, 8) else 0.3
            plays = rng.random() < p_play
            minutes = int(rng.choice([90, 75, 20])) if plays else 0
            xg = float(rng.gamma(1.0, 0.1 + 0.1 * (element_type >= 3))) if plays else 0.0
            goals = int(rng.random() < xg)
            pts = (0 if not plays else (2 if minutes >= 60 else 1)) + goals * 5
            row = {
                "season": season,
                "element_id": element_id,
                "gw": gw,
                "fixture_id": fid,
                "minutes": minutes,
                "total_points": pts,
                "starts": int(minutes >= 60),
                "goals_scored": goals,
                "assists": 0,
                "clean_sheets": 0,
                "goals_conceded": 1,
                "own_goals": 0,
                "penalties_saved": 0,
                "penalties_missed": 0,
                "yellow_cards": 0,
                "red_cards": 0,
                "saves": 0,
                "bonus": int(goals > 0),
                "bps": 10 + 10 * goals,
                "defensive_contribution": int(rng.integers(0, 12)) if plays else 0,
                "expected_goals": xg,
                "expected_assists": xg / 2,
                "expected_goal_involvements": 1.5 * xg,
                "expected_goals_conceded": 1.2 if plays else 0.0,
                "influence": 10.0 * plays,
                "creativity": 5.0 * plays,
                "threat": 20.0 * xg,
                "ict_index": 3.0 * plays,
                "was_home": club == home,
                "opponent_team_fpl_id": away if club == home else home,
                "value_tenths": 45 + k,
                "selected_by": 1000 * (k + 1) + gw,
                "fpl_xp": 2.0,
                "source": "history",
            }
            if planted(season, gw):
                for key in row:
                    if key in ("season", "element_id", "gw", "fixture_id", "was_home",
                               "opponent_team_fpl_id", "source"):
                        continue
                    row[key] = EXTREME
            stats.append(row)

    fixtures = []
    for season, fid, gw, home, away in FIXTURES:
        done = not planted(season, gw)
        fixtures.append(
            {
                "season": season, "fixture_id": fid, "gw": gw,
                "team_h_fpl_id": home, "team_a_fpl_id": away,
                "team_h_difficulty": 3, "team_a_difficulty": 3, "kickoff_time": None,
                "started": True, "finished": True, "finished_provisional": True,
                "minutes": 90,
                "team_h_score": (fid % 3) if done else EXTREME,
                "team_a_score": (fid % 2) if done else EXTREME,
            }
        )
    players = pd.DataFrame(
        [
            {"season": s, "element_id": e, "code": c, "element_type": t,
             "web_name": f"P{c}", "first_name": "P", "second_name": str(c)}
            for s in (S1, S2)
            for e, c, t, _ in _player_ids(s)
        ]
    )
    teams = pd.DataFrame(
        [{"season": s, "fpl_id": f, "code": 50 + f, "name": f"Club {f}", "short_name": f"C{f}"}
         for s in (S1, S2) for f in CLUBS]
    )
    return Tables(stats=pd.DataFrame(stats), players=players,
                  fixtures=pd.DataFrame(fixtures), teams=teams)


# Small trees, so the tests fit in well under a second each.
FAST = GbmConfig(clf_max_iter=15, reg_max_iter=15, clf_min_samples_leaf=5,
                 reg_min_samples_leaf=5, clf_max_leaf_nodes=7, reg_max_leaf_nodes=7)


@pytest.fixture(scope="module")
def history() -> History:
    return History(build_tables())


def _features_for(frame: pd.DataFrame, season: str, gw: int) -> pd.DataFrame:
    rows = frame[(frame["season"] == season) & (frame["gw"] == gw)]
    cols = feature_columns(frame)
    return rows.set_index(["code", "fixture_id"])[cols].sort_index()


# --- Leakage: the most important tests --------------------------------------------


@pytest.mark.parametrize("target", [(S2, 4), (S2, 2), (S1, 3)])
def test_features_cannot_see_their_own_gameweek(target):
    """Planting extremes at the target gameweek and after moves no feature at it.

    Both frames are built from *full* stat tables — the target rows included —
    so this checks the builder's own windows, not the AsOf filter. S2 gw4 is a
    double: the second fixture must not see the first fixture's result either.
    """
    season, gw = target
    clean = History(build_tables())
    dirty = History(build_tables(plant_from=target))
    a = build_features(clean.stats, clean.fixtures)
    b = build_features(dirty.stats, dirty.fixtures)
    fa, fb = _features_for(a, season, gw), _features_for(b, season, gw)
    assert len(fa) > 0
    pd.testing.assert_frame_equal(fa, fb)
    # And the builder is not trivially constant: the extremes are visible a
    # gameweek later.
    later = gw + 1
    assert not _features_for(a, season, later).equals(_features_for(b, season, later))


def test_double_gameweek_fixtures_share_the_same_pre_deadline_state():
    h = History(build_tables())
    f = build_features(h.stats, h.fixtures)
    doubles = f[(f["season"] == S2) & (f["gw"] == 4) & (f["gw_fixtures"] == 2)]
    assert len(doubles) > 0
    cols = [c for c in feature_columns(f) if c not in ("home", "opp_gf", "opp_ga", "opp_xgf",
                                                        "opp_xga", "opp_matches", "att_vs_def",
                                                        "def_vs_att", "fx_xg_for", "fx_xg_against",
                                                        "fx_cs_prob")]
    per_player = doubles.groupby("code")[cols].nunique(dropna=False)
    assert (per_player <= 1).all().all()


def test_prediction_features_equal_training_features(history):
    """A target pseudo row gets exactly the features the real row got in training.

    This is what makes the backtest evaluate the production path: the same
    function, the same windows, whether the row's result exists or not.
    """
    season, gw = S2, 4
    asof = history.asof(season, gw)
    model = GbmXpModel(FAST)
    predicting = model.frame(asof)
    targets = predicting[predicting["is_target"]]
    training = build_features(history.stats, history.fixtures)
    cols = feature_columns(training)
    got = targets.set_index(["code", "fixture_id"])[cols].sort_index()
    want = _features_for(training, season, gw)
    assert len(got) == len(want) > 0
    pd.testing.assert_frame_equal(got, want.loc[got.index], check_dtype=False)


def test_fit_before_t_is_unaffected_by_extremes_at_t_and_later():
    target = (S2, 4)
    clean = History(build_tables())
    dirty = History(build_tables(plant_from=target))
    preds = []
    for h in (clean, dirty):
        m = GbmXpModel(FAST)
        asof = h.asof(*target)
        m.fit(asof)
        preds.append(m.predict(asof))
    assert preds[0] == preds[1]
    assert len(preds[0]) > 0


def test_fpl_xp_is_never_a_feature(history):
    f = build_features(history.stats, history.fixtures)
    assert not any("fpl_xp" in c for c in f.columns)


# --- Doubles and blanks ---------------------------------------------------------


def test_blank_is_exactly_zero_and_double_is_the_sum_of_two_fixtures(history):
    asof = history.asof(S2, 4)
    model = GbmXpModel(FAST).fit(asof)
    fixtures = model.predict_fixtures(asof)
    gw = model.predict_gameweek(asof).set_index("element_id")
    counts = asof.players.set_index("element_id")["fixture_count"]

    doubled = counts[counts == 2].index
    assert len(doubled) > 0
    for e in doubled:
        two = fixtures[fixtures["element_id"] == e]["xp"]
        assert len(two) == 2
        assert gw.at[e, "xp"] == pytest.approx(two.sum())
        assert gw.at[e, "fixture_count"] == 2

    blank_asof = history.asof(S2, 3)
    model.fit(blank_asof)
    blank = model.predict_gameweek(blank_asof).set_index("element_id")
    blanked = blank_asof.players.loc[blank_asof.players["fixture_count"] == 0, "element_id"]
    assert len(blanked) > 0
    for e in blanked:
        assert blank.at[e, "xp"] == 0.0
        assert blank.at[e, "p_play"] == 0.0
        assert blank.at[e, "fixture_count"] == 0


# --- Defensive contribution --------------------------------------------------------


def test_defensive_contribution_is_missing_before_2025_26_not_zero(history):
    f = build_features(history.stats, history.fixtures)
    s1 = f[f["season"] == S1]
    assert s1["defcon_r5"].isna().all()
    assert s1["defcon90_a5"].isna().all()
    # Early S2: the window reaches back into S1 rows, which must not count as 0s.
    s2 = f[(f["season"] == S2) & (f["gw"] == 3)]
    stats = history.stats
    for code, feat in s2.groupby("code")["defcon_r10"].first().items():
        rows = stats[(stats["code"] == code) & (stats["season"] == S2) & (stats["gw"] < 3)]
        expected = rows["defensive_contribution"].astype("float64").mean()
        assert feat == pytest.approx(expected)


# --- Availability ------------------------------------------------------------------


def test_unavailable_player_gets_zero(history):
    asof = history.asof(S2, 5)
    model = GbmXpModel(FAST).fit(asof)
    neutral = model.predict_gameweek(asof).set_index("element_id")
    playing = neutral[neutral["fixture_count"] > 0]
    injured, doubtful = int(playing.index[0]), int(playing.index[1])
    avail = {injured: availability_multiplier("i", None), doubtful: availability_multiplier("d", 50)}
    live = model.predict_gameweek(asof, avail).set_index("element_id")
    assert live.at[injured, "xp"] == 0.0
    assert live.at[injured, "p_play"] == 0.0
    assert live.at[injured, "xmins"] == 0.0
    assert live.at[doubtful, "xp"] == pytest.approx(neutral.at[doubtful, "xp"] * 0.5)
    others = [e for e in playing.index if e not in (injured, doubtful)]
    assert live.loc[others, "xp"].equals(neutral.loc[others, "xp"])


def test_availability_multiplier():
    assert availability_multiplier("a", None) == 1.0
    assert availability_multiplier("i", None) == 0.0
    assert availability_multiplier("d", None) == 0.5
    assert availability_multiplier("d", 75) == 0.75
    assert availability_multiplier("a", 100) == 1.0


# --- Output shape and production path ------------------------------------------------


def test_xp_rows_match_the_xpoints_shape(history):
    asof = history.asof(S2, 4)
    model = GbmXpModel(FAST).fit(asof)
    rows = xp_rows(model, asof, as_of_gw=3)
    assert len(rows) == len(asof.players)
    keys = {"season", "element_id", "gw", "model_version", "xp", "xmins", "p_start",
            "p_play", "components", "as_of_gw", "fixture_count"}
    for r in rows:
        assert set(r) == keys
        assert r["model_version"] == MODEL_VERSION
        assert r["as_of_gw"] == 3
        assert r["gw"] == 4
        assert set(r["components"]) == {"appearance", "returns"}
        assert sum(r["components"].values()) == pytest.approx(r["xp"], abs=1e-9)
        assert 0.0 <= r["p_start"] <= r["p_play"] <= 1.0 + 1e-9


def test_live_asof_and_compute_live_project_the_horizon():
    """Production: fit on everything so far, project gameweeks with no rows yet."""
    tables = build_tables()
    stats = tables.stats[(tables.stats["season"] == S1) | (tables.stats["gw"] <= 3)]
    h = History(Tables(stats, tables.players, tables.fixtures, tables.teams))
    live = pd.DataFrame(
        [{"element_id": e, "team_fpl_id": club, "status": "i" if k == 0 else "a",
          "chance_of_playing_next_round": None}
         for e, code, _, club in _player_ids(S2) for k in [code % 100]]
    )
    view = live_asof(h, S2, 5, live)
    assert view.stats["gw"].where(view.stats["season"] == S2).max() == 3
    assert set(view.players["element_id"]) == set(live["element_id"])

    rows = compute_live(h, S2, live, target_gws=[4, 5], as_of_gw=3, config=FAST)
    assert {r["gw"] for r in rows} == {4, 5}
    by = {(r["element_id"], r["gw"]): r for r in rows}
    # Club 4 doubles in gw4; injured goalkeepers (k == 0) project nothing.
    assert all(by[(e, 4)]["fixture_count"] == 2 for e, _, _, club in _player_ids(S2) if club == 4)
    assert all(by[(e, 5)]["xp"] == 0.0 for e, code, _, _ in _player_ids(S2) if code % 100 == 0)
    assert any(r["xp"] > 0 for r in rows)


def test_backtest_adapter_refits_at_season_start_and_on_cadence(history):
    model = GbmModel(FAST, refit_every=2)
    for season, gw in [(S1, 2), (S1, 3), (S1, 4), (S1, 5), (S2, 2), (S2, 3)]:
        asof = history.asof(season, gw)
        model.fit(asof)
        preds = model.predict(asof)
        assert set(preds) == set(int(e) for e in asof.players["element_id"])
    assert [(f["season"], f["gw"]) for f in model.fit_log] == [(S1, 2), (S1, 4), (S2, 2)]


def test_feature_module_has_no_reference_to_fpl_xp():
    source = Path(gbm_features.__file__).read_text()
    code_lines = [ln for ln in source.splitlines() if not ln.strip().startswith(("#", "*", "`"))]
    assert not any('"fpl_xp"' in ln for ln in code_lines)
