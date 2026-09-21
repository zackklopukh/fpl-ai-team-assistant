"""Tests for the walk-forward backtest.

Offline and synthetic: two tiny seasons, three clubs, three players, built so
that each hazard the harness exists to handle happens at a known place.

The test that matters most is the leakage one. Every numeric field of every row
at or after the target — stats, fixture results, and the end-of-season columns
of `players` and `teams` — is planted with a sentinel value, and a model that
inspects every cell it is handed must not find it anywhere. A leaky harness
does not crash; it produces a confident wrong answer about which model to ship,
and this is the only place that would notice.
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

from backtest import metrics  # noqa: E402
from backtest.baselines import (  # noqa: E402
    BaselineModel,
    FplXpLagModel,
    FplXpModel,
    NaiveModel,
)
from backtest.data import AsOf, History, Tables  # noqa: E402
from backtest.harness import Target, default_targets, run_backtest  # noqa: E402
from backtest.report import benchmark_frame, build_results, render  # noqa: E402

S1, S2 = "2024-25", "2025-26"
SENTINEL = 31337

# Alice: element 10 in S1 at club 1, element 77 in S2 at club 3.
# Bob: element 77 in S1 (the id Alice gets next season) at club 2, element 20 in S2.
# Carl: S2 only, element 30, plays for club 1 — but `players` records club 2,
# his end-of-season club after a move.
ALICE, BOB, CARL = 5000, 6000, 7000

PLAYERS = [
    # season, element_id, code, element_type, web_name, end-of-season team
    (S1, 10, ALICE, 3, "Alice", 1),
    (S1, 77, BOB, 4, "Bob", 2),
    (S2, 77, ALICE, 3, "Alice", 3),
    (S2, 20, BOB, 4, "Bob", 2),
    (S2, 30, CARL, 2, "Carl", 2),
]

# Club each player's rows are written for, per season (not the `players` column).
CLUB = {(S1, 10): 1, (S1, 77): 2, (S2, 77): 3, (S2, 20): 2, (S2, 30): 1}

FIXTURES = [
    # season, fixture_id, gw, home, away
    (S1, 1, 1, 1, 2),
    (S1, 2, 2, 2, 3),
    (S1, 3, 3, 1, 3),
    (S2, 11, 1, 1, 3),  # club 2 blanks
    (S2, 12, 2, 3, 1),
    (S2, 13, 2, 2, 3),  # club 3 doubles in gw2
    (S2, 14, 3, 1, 2),  # club 3 blanks in gw3
    (S2, 15, 4, 3, 2),  # club 1 blanks in gw4
    (S2, 16, 5, 1, 3),
]


def points(season: str, element_id: int, gw: int, fixture_id: int) -> int:
    return (element_id + gw * 3 + fixture_id) % 11


def build_tables(sentinel_from: tuple[str, int] | None = None) -> Tables:
    """The synthetic database. Rows at or after `sentinel_from` carry SENTINEL."""

    def planted(season: str, gw: int | None) -> bool:
        if sentinel_from is None:
            return False
        s, g = sentinel_from
        return season > s or (season == s and (gw is None or gw >= g))

    stats = []
    seen: set[tuple[str, int, int]] = set()
    for season, fixture_id, gw, home, away in FIXTURES:
        for (s, element_id), club in CLUB.items():
            if s != season or club not in (home, away):
                continue
            is_home = club == home
            # Carl sits out S2 gw1 entirely: a registered non-appearance.
            minutes = 0 if (season == S2 and element_id == 30 and gw == 1) else 90
            row = {
                "season": season,
                "element_id": element_id,
                "gw": gw,
                "fixture_id": fixture_id,
                "minutes": minutes,
                "total_points": points(season, element_id, gw, fixture_id) if minutes else 0,
                "starts": 1 if minutes else 0,
                "goals_scored": 0,
                "assists": 0,
                "clean_sheets": 0,
                "goals_conceded": 1,
                "own_goals": 0,
                "penalties_saved": 0,
                "penalties_missed": 0,
                "yellow_cards": 0,
                "red_cards": 0,
                "saves": 0,
                "bonus": 0,
                "bps": 20,
                "defensive_contribution": 4,
                "expected_goals": 0.2,
                "expected_assists": 0.1,
                "expected_goal_involvements": 0.3,
                "expected_goals_conceded": 1.1,
                "influence": 10.0,
                "creativity": 10.0,
                "threat": 10.0,
                "ict_index": 3.0,
                "was_home": is_home,
                "opponent_team_fpl_id": away if is_home else home,
                "value_tenths": 50 + gw,
                "selected_by": 1000 + gw,
                # As imported: a whole-gameweek figure on the first fixture's row
                # of a double, null on the second; and not captured at all for
                # S2 gw4.
                "fpl_xp": None if (season, element_id, gw) in seen or (season, gw) == (S2, 4) else 2.5,
                "source": "history",
            }
            seen.add((season, element_id, gw))
            if planted(season, gw):
                for key, value in row.items():
                    if key in ("season", "element_id", "gw", "fixture_id", "was_home", "source"):
                        continue
                    if key == "opponent_team_fpl_id":
                        continue
                    row[key] = SENTINEL
            stats.append(row)

    fixtures = []
    for season, fixture_id, gw, home, away in FIXTURES:
        done = not planted(season, gw)
        fixtures.append(
            {
                "season": season,
                "fixture_id": fixture_id,
                "gw": gw,
                "team_h_fpl_id": home,
                "team_a_fpl_id": away,
                "team_h_difficulty": 3,
                "team_a_difficulty": 3,
                "kickoff_time": None,
                "started": True,
                "finished": True,
                "finished_provisional": True,
                "minutes": 90 if done else SENTINEL,
                "team_h_score": 1 if done else SENTINEL,
                "team_a_score": 0 if done else SENTINEL,
            }
        )

    players = pd.DataFrame(
        [
            {
                "season": s,
                "element_id": e,
                "code": c,
                "element_type": t,
                "web_name": n,
                "first_name": n,
                "second_name": "X",
                "team_fpl_id": team,
                # End-of-season values. Must never reach a model.
                "now_cost_tenths": SENTINEL,
                "total_points": SENTINEL,
                "form": SENTINEL,
                "selected_by_percent": SENTINEL,
            }
            for s, e, c, t, n, team in PLAYERS
        ]
    )
    teams = pd.DataFrame(
        [
            {
                "season": s,
                "fpl_id": f,
                "code": 900 + f,
                "name": f"Club {f}",
                "short_name": f"C{f}",
                "strength": SENTINEL,
            }
            for s in (S1, S2)
            for f in (1, 2, 3)
        ]
    )
    return Tables(
        stats=pd.DataFrame(stats),
        players=players,
        fixtures=pd.DataFrame(fixtures),
        teams=teams,
    )


@pytest.fixture
def history() -> History:
    return History(build_tables())


# --- Leakage ---------------------------------------------------------------


def _every_frame(asof: AsOf) -> dict[str, pd.DataFrame]:
    out = {}
    for f in dataclasses.fields(asof):
        value = getattr(asof, f.name)
        if isinstance(value, pd.DataFrame):
            out[f.name] = value
    return out


def _contains(frame: pd.DataFrame, value: float) -> bool:
    for column in frame.columns:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if (numeric == value).any():
            return True
    return False


def test_a_model_inspecting_everything_finds_nothing_from_the_target_onward():
    target = (S2, 3)
    history = History(build_tables(sentinel_from=target))
    asof = history.asof(*target)

    frames = _every_frame(asof)
    assert set(frames) == {"stats", "player_history", "players", "fixtures", "teams"}
    for name, frame in frames.items():
        assert not _contains(frame, SENTINEL), f"sentinel leaked into AsOf.{name}"

    # No stat row at or after the target, in any frame that carries rows.
    for frame in (asof.stats, asof.player_history):
        at_or_after = (frame["season"] > S2) | ((frame["season"] == S2) & (frame["gw"] >= 3))
        assert not at_or_after.any()

    # The schedule is visible (the target's fixture is listed) but not its result.
    target_fx = asof.target_fixtures()
    assert list(target_fx["fixture_id"]) == [14]
    for column in ("team_h_score", "team_a_score", "finished", "started", "minutes"):
        assert asof.fixtures.loc[
            (asof.fixtures["season"] == S2) & (asof.fixtures["gw"] >= 3), column
        ].isna().all()

    # Including FPL's recorded fpl_xp for the target week, which carries the
    # outcome in the real data and so is withheld like every other result field.
    assert not hasattr(asof, "fpl_projection")


def test_asof_holds_only_plain_values_and_frames():
    """No reference back to History, the unfiltered frames, or a connection."""
    asof = History(build_tables()).asof(S2, 3)
    for f in dataclasses.fields(asof):
        assert isinstance(getattr(asof, f.name), (str, int, pd.DataFrame, pd.Series)), f.name


def test_a_cheating_model_cannot_return_the_targets_actual_points():
    class Cheat:
        name = "cheat"

        def predict(self, asof: AsOf) -> dict[int, float]:
            rows = asof.stats[(asof.stats["season"] == asof.season) & (asof.stats["gw"] == asof.gw)]
            hist = asof.player_history
            rows = pd.concat([rows, hist[(hist["season"] == asof.season) & (hist["gw"] == asof.gw)]])
            return rows.groupby("element_id")["total_points"].sum().to_dict()

    history = History(build_tables())
    result = run_backtest(history, [Cheat()], [Target(S2, 2), Target(S2, 3), Target(S2, 4)])
    assert result.stats["cheat"].predicted == 0
    assert result.predictions["pred"].isna().all()


def test_a_past_target_sees_no_later_season():
    asof = History(build_tables()).asof(S1, 2)
    assert set(asof.stats["season"]) == {S1}
    assert set(asof.stats["gw"]) == {1}
    assert set(asof.fixtures["season"]) == {S1}
    assert set(asof.teams["season"]) == {S1}


def test_players_and_teams_carry_identity_only():
    asof = History(build_tables()).asof(S2, 3)
    for leaked in ("now_cost_tenths", "total_points", "form", "selected_by_percent"):
        assert leaked not in asof.players.columns
    assert "strength" not in asof.teams.columns


# --- Linking and population ------------------------------------------------


def test_history_links_across_seasons_by_code_not_element_id(history):
    asof = history.asof(S2, 2)
    alice = asof.player_history[asof.player_history["element_id"] == 77]

    # Alice's S1 rows arrive under her S2 id; Bob's S1 rows (also id 77) do not.
    assert set(alice["code"]) == {ALICE}
    s1 = alice[alice["season"] == S1]
    assert set(s1["row_element_id"]) == {10}
    assert sorted(s1["gw"]) == [1, 3]
    assert sorted(alice.loc[alice["season"] == S2, "gw"]) == [1]

    bob = asof.player_history[asof.player_history["element_id"] == 20]
    assert set(bob.loc[bob["season"] == S1, "row_element_id"]) == {77}


def test_club_at_target_comes_from_fixtures_not_end_of_season_players(history):
    players = history.asof(S2, 2).players.set_index("element_id")
    assert players.loc[30, "team_fpl_id"] == 1  # `players` says 2
    assert players.loc[77, "team_fpl_id"] == 3


def test_double_gameweek_actual_is_the_sum_of_both_rows(history):
    actual = history.actuals(S2, 2).set_index("element_id")
    expected = points(S2, 77, 2, 12) + points(S2, 77, 2, 13)
    assert actual.loc[77, "actual"] == expected
    assert actual.loc[77, "fixture_count"] == 2
    assert actual.loc[77, "rows"] == 2


def test_blank_is_registered_with_actual_zero(history):
    actual = history.actuals(S2, 3).set_index("element_id")
    assert actual.loc[77, "fixture_count"] == 0
    assert actual.loc[77, "actual"] == 0


def test_not_registered_before_first_row_or_after_last():
    history = History(build_tables())
    # Bob's club blanks S2 gw1 and he has no earlier S2 row: not yet registered.
    assert 20 not in set(history.registered(S2, 1)["element_id"])
    assert 20 in set(history.registered(S2, 2)["element_id"])


def test_point_in_time_price_is_the_last_row_before_the_target(history):
    players = history.asof(S2, 3).players.set_index("element_id")
    # Alice's last row before gw3 is gw2 (value 52). Her target-season gw1 row is
    # older, and there is no gw3 row to peek at in any case.
    assert players.loc[77, "price_tenths"] == 52
    assert players.loc[77, "selected_by"] == 1002

    # Gameweek 1 has nothing before it this season, and last season's closing
    # price is not carried over.
    gw1 = history.asof(S2, 1).players.set_index("element_id")
    assert pd.isna(gw1.loc[77, "price_tenths"])


def test_target_price_row_is_not_used():
    history = History(build_tables(sentinel_from=(S2, 3)))
    players = history.asof(S2, 3).players
    assert not (players["price_tenths"] == SENTINEL).any()


# --- Baselines -------------------------------------------------------------


def test_naive_uses_last_appearances_across_seasons_and_scales_by_fixtures(history):
    asof = history.asof(S2, 2)
    preds = NaiveModel().predict(asof)
    alice_points = [points(S1, 10, 1, 1), points(S1, 10, 3, 3), points(S2, 77, 1, 11)]
    assert preds[77] == pytest.approx(2 * np.mean(alice_points))

    # Carl's only row is a non-appearance: no evidence, predicted zero.
    assert preds[30] == 0.0


def test_baseline_runs_through_asof_and_predicts_zero_for_a_blank(history):
    preds = BaselineModel().predict(history.asof(S2, 3))
    assert preds[77] == 0.0  # blank
    assert preds[30] > 0.0
    assert preds[20] > 0.0


def test_fpl_xp_reference_reads_history_not_asof(history):
    model = FplXpModel()
    assert model.predict_from_history(history, S2, 3) == {30: 2.5, 20: 2.5}
    with pytest.raises(TypeError):
        model.predict(history.asof(S2, 3))


def test_fpl_xp_for_a_double_sums_rows_ignoring_the_null_second_row(history):
    # Alice's gw2 double: 2.5 on the first fixture's row, null on the second.
    assert history.recorded_fpl_xp(S2, 2).loc[77] == pytest.approx(2.5)


def test_fpl_xp_lag_uses_last_weeks_figure_per_fixture(history):
    preds = FplXpLagModel().predict(history.asof(S2, 2))
    assert preds[77] == pytest.approx(5.0)  # 2.5 for one fixture, doubled this week
    assert preds[30] == pytest.approx(2.5)
    assert 20 not in preds  # no gw1 row: missing, not zero

    preds = FplXpLagModel().predict(history.asof(S2, 3))
    assert preds[77] == 0.0  # blank this week
    assert preds[30] == pytest.approx(2.5)


def test_fpl_xp_lag_sees_only_asof_even_with_planted_future():
    history = History(build_tables(sentinel_from=(S2, 3)))
    preds = FplXpLagModel().predict(history.asof(S2, 3))
    assert SENTINEL not in preds.values()


def test_missing_fpl_xp_is_missing_not_zero(history):
    # S2 gw4 was never captured: no projection at all, rather than 0s.
    assert history.recorded_fpl_xp(S2, 4).empty
    result = run_backtest(history, [FplXpModel(), NaiveModel()], [Target(S2, 3), Target(S2, 4)])
    frame = benchmark_frame(result.predictions, ["fpl_xp", "naive"])
    assert set(frame["gw"]) == {3}
    # The double's second-fixture points still count toward the actual.
    assert result.stats["fpl_xp"].missing > 0


def test_defensive_contribution_is_missing_before_it_existed(history):
    stats = history.asof(S2, 3).stats
    assert stats.loc[stats["season"] == S1, "defensive_contribution"].isna().all()
    assert stats.loc[stats["season"] == S2, "defensive_contribution"].notna().all()


# --- Harness ---------------------------------------------------------------


def test_blank_violations_are_counted_and_blanks_left_out_of_metrics(history):
    class AlwaysThree:
        name = "three"

        def predict(self, asof):
            return {int(e): 3.0 for e in asof.players["element_id"]}

    result = run_backtest(history, [AlwaysThree()], [Target(S2, 3)])
    assert result.stats["three"].blank_violations == 1  # Alice, club 3 blanks
    frame = benchmark_frame(result.predictions, ["three"])
    assert 77 not in set(frame["element_id"])


def test_fit_is_called_with_the_target_view_at_the_configured_cadence(history):
    class Recorder:
        name = "rec"

        def __init__(self):
            self.fits: list[tuple[str, int, int]] = []

        def fit(self, asof):
            latest = asof.stats[asof.stats["season"] == asof.season]["gw"].max()
            self.fits.append((asof.season, asof.gw, -1 if pd.isna(latest) else int(latest)))

        def predict(self, asof):
            return {}

    model = Recorder()
    targets = [Target(S1, 2), Target(S1, 3), Target(S2, 1), Target(S2, 2), Target(S2, 3), Target(S2, 4)]
    run_backtest(history, [model], targets, refit_every=2)
    # Refit at the start, every 2 targets, and always at a new season.
    assert [(s, g) for s, g, _ in model.fits] == [(S1, 2), (S2, 1), (S2, 3)]
    # Training data never reaches the target.
    assert all(latest < gw for _, gw, latest in model.fits)


def test_default_targets_start_2026_27_at_gw2():
    history = History(build_tables())
    targets = default_targets(history, [S2], {S2: 2})
    assert targets[0] == Target(S2, 2)


def test_end_to_end_report_renders(history):
    models = [FplXpModel(), FplXpLagModel(), NaiveModel(), BaselineModel()]
    targets = default_targets(history, [S2], {})
    result = run_backtest(history, models, targets)
    results = build_results(result, [m.name for m in models], history.diagnostics())
    text = render(results)
    assert "HEAD-TO-HEAD vs fpl_xp_lag" in text
    assert "HEAD-TO-HEAD vs fpl_xp RECORDED" in text
    assert text.index("vs fpl_xp_lag") < text.index("vs fpl_xp RECORDED")
    assert "FULL SEASON" in text
    assert "Availability caveat" in text
    h2h = results["views"]["vs_fpl_xp"]
    full = results["views"]["full_season"]
    assert h2h["gameweeks_by_season"] == {S2: [1, 2, 3, 5]}  # gw4 has no fpl_xp
    assert full["gameweeks_by_season"] == {S2: [1, 2, 3, 4, 5]}
    assert h2h["results"]["naive"]["overall"]["n"] > 0
    assert results["views"]["vs_fpl_xp_lag"]["results"]["baseline"]["overall"]["n"] > 0
    assert results["runs"]["baseline"]["blank_violations"] == 0


# --- Metrics against hand-computed values ------------------------------------


def _frame(preds, actuals, gw=1, positions=None):
    n = len(preds)
    return pd.DataFrame(
        {
            "season": S1,
            "gw": gw,
            "element_id": range(1, n + 1),
            "element_type": positions or [3] * n,
            "pred": preds,
            "actual": actuals,
        }
    )


def test_point_errors():
    df = _frame([1.0, 2.0, 4.0], [2, 2, 1])
    assert metrics.mae(df) == pytest.approx((1 + 0 + 3) / 3)
    assert metrics.rmse(df) == pytest.approx(math.sqrt((1 + 0 + 9) / 3))
    assert metrics.bias(df) == pytest.approx((-1 + 0 + 3) / 3)


def test_spearman_hand_computed():
    assert metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Ranks (1,2,3,4) vs (1,3,2,4): d^2 sum = 2, rho = 1 - 6*2/(4*15) = 0.8
    assert metrics.spearman([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)
    assert math.isnan(metrics.spearman([1, 1, 1], [1, 2, 3]))


def test_spearman_is_averaged_per_gameweek_not_pooled():
    a = _frame([1, 2, 3], [1, 2, 3], gw=1)
    b = _frame([1, 2, 3], [3, 2, 1], gw=2)
    assert metrics.mean_spearman(pd.concat([a, b])) == pytest.approx(0.0)


def test_topk_hit_rate_hand_computed():
    # Actual top 2: ids 1 (10) and 2 (8). Model's top 2: ids 1 and 3.
    df = _frame([9.0, 1.0, 8.0, 0.0], [10, 8, 2, 0])
    assert metrics.topk_hit_rate(df, 2) == pytest.approx(0.5)


def test_topk_counts_ties_at_the_line_as_hits():
    # Actual top-2 line is 5, shared by ids 2 and 3; picking 3 is not a miss.
    df = _frame([9.0, 0.0, 8.0], [9, 5, 5])
    assert metrics.topk_hit_rate(df, 2) == pytest.approx(1.0)


def test_decision_value_hand_computed():
    positions = [1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4]
    preds = list(range(len(positions), 0, -1))  # model ranks by list order
    actuals = [5, 1, 9] + [2] * 5 + [7] + [3] * 5 + [0] + [6, 6, 6, 0]
    df = _frame([float(p) for p in preds], actuals, positions=positions)
    value = metrics.decision_value(df)
    per = value["per_pick_by_position"]
    assert per["GKP"] == pytest.approx((5 + 1) / 2)
    assert per["DEF"] == pytest.approx(2.0)
    assert per["MID"] == pytest.approx(3.0)
    assert per["FWD"] == pytest.approx(6.0)
    assert value["squad_points_per_gw"] == pytest.approx(6 + 10 + 15 + 18)

    best = metrics.decision_value(df, by="actual")
    assert best["per_pick_by_position"]["GKP"] == pytest.approx((9 + 5) / 2)
    assert best["per_pick_by_position"]["DEF"] == pytest.approx((7 + 2 * 4) / 5)


def test_calibration_buckets():
    df = _frame([0.5, 0.7, 6.5, 9.0], [0, 2, 6, 3])
    rows = {r["bucket"]: r for r in metrics.calibration(df)}
    assert rows["0-1"]["n"] == 2
    assert rows["0-1"]["mean_actual"] == pytest.approx(1.0)
    assert rows["6-8"]["mean_pred"] == pytest.approx(6.5)
    assert rows["8-+"]["mean_actual"] == pytest.approx(3.0)
