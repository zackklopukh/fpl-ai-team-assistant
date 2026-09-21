"""The weekly grading job. What matters is that the grade cannot be flattered."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_models import graded_frame, paired_vs_live, weekly_scores  # noqa: E402

DEADLINE = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
BEFORE = DEADLINE - timedelta(hours=8)
AFTER = DEADLINE + timedelta(hours=8)


def inputs(**overrides):
    base = {
        "xp": pd.DataFrame(
            [
                # element 1 (club 1) and 2 (club 2), two models, frozen on time.
                {"element_id": 1, "gw": 6, "model_version": "a", "xp": 6.0, "computed_at": BEFORE},
                {"element_id": 2, "gw": 6, "model_version": "a", "xp": 2.0, "computed_at": BEFORE},
                {"element_id": 1, "gw": 6, "model_version": "b", "xp": 3.0, "computed_at": BEFORE},
                {"element_id": 2, "gw": 6, "model_version": "b", "xp": 4.0, "computed_at": BEFORE},
            ]
        ),
        "stats": pd.DataFrame([{"element_id": 1, "gw": 6, "total_points": 8}]),
        "players": pd.DataFrame(
            [
                {"element_id": 1, "element_type": 3, "team_fpl_id": 1},
                {"element_id": 2, "element_type": 3, "team_fpl_id": 2},
                {"element_id": 3, "element_type": 3, "team_fpl_id": 3},
            ]
        ),
        "fixtures": pd.DataFrame([{"gw": 6, "team_h_fpl_id": 1, "team_a_fpl_id": 2}]),
        "deadlines": pd.DataFrame([{"gw": 6, "deadline_time": DEADLINE, "finished": True}]),
    }
    base.update(overrides)
    return base


def test_a_prediction_written_after_the_deadline_is_excluded_and_reported():
    data = inputs()
    data["xp"].loc[data["xp"]["model_version"] == "b", "computed_at"] = AFTER

    graded, late = graded_frame(**data)

    assert set(graded["model_version"]) == {"a"}
    assert late.to_dict("records") == [{"model_version": "b", "gw": 6}]


def test_a_player_whose_club_played_but_he_did_not_is_graded_with_zero():
    graded, _ = graded_frame(**inputs())

    row = graded[(graded["element_id"] == 2) & (graded["model_version"] == "a")].iloc[0]
    assert row["actual"] == 0.0


def test_a_blank_gameweek_player_is_not_graded():
    data = inputs()
    extra = pd.DataFrame(
        [{"element_id": 3, "gw": 6, "model_version": m, "xp": 0.0, "computed_at": BEFORE} for m in "ab"]
    )
    data["xp"] = pd.concat([data["xp"], extra], ignore_index=True)

    graded, _ = graded_frame(**data)

    assert 3 not in set(graded["element_id"])


def test_a_double_gameweek_sums_both_fixtures():
    data = inputs(
        stats=pd.DataFrame(
            [
                {"element_id": 1, "gw": 6, "total_points": 8},
                {"element_id": 1, "gw": 6, "total_points": 5},
            ]
        )
    )
    graded, _ = graded_frame(**data)

    assert graded[graded["element_id"] == 1]["actual"].iloc[0] == 13.0


def test_every_model_is_graded_on_the_same_players():
    # Model b skipped player 2. Grading b on fewer, easier players would flatter
    # it, so player 2 drops out for both.
    data = inputs()
    data["xp"] = data["xp"][
        ~((data["xp"]["model_version"] == "b") & (data["xp"]["element_id"] == 2))
    ]

    graded, _ = graded_frame(**data)

    per_model = graded.groupby("model_version")["element_id"].apply(set).to_dict()
    assert per_model == {"a": {1}, "b": {1}}


def test_an_unfinished_gameweek_is_not_graded():
    data = inputs(deadlines=pd.DataFrame([{"gw": 6, "deadline_time": DEADLINE, "finished": False}]))

    graded, _ = graded_frame(**data)

    assert graded.empty


def test_paired_comparison_reports_the_shadow_minus_the_live_model():
    weekly = pd.DataFrame(
        [
            {"gw": gw, "model_version": m, "squad_pts": pts, "spearman": 0.5, "mae": 1.0, "n": 10}
            for gw, a, b in ((6, 70.0, 75.0), (7, 60.0, 64.0), (8, 80.0, 83.0))
            for m, pts in (("live", a), ("shadow", b))
        ]
    )

    paired = paired_vs_live(weekly, "live", "squad_pts")

    row = paired.iloc[0]
    assert row["model_version"] == "shadow"
    assert row["mean_diff"] == 4.0
    assert row["wins"] == 3
    assert row["ci_low"] > 0  # consistently ahead on every week


def test_weekly_scores_rewards_the_model_that_ranked_correctly():
    # Three players (spearman needs at least three to mean anything).
    # Actual order: 1 (8) > 4 (3) > 2 (0). Model a gets it right, b backwards.
    data = inputs(
        stats=pd.DataFrame(
            [
                {"element_id": 1, "gw": 6, "total_points": 8},
                {"element_id": 4, "gw": 6, "total_points": 3},
            ]
        )
    )
    data["players"] = pd.concat(
        [data["players"], pd.DataFrame([{"element_id": 4, "element_type": 3, "team_fpl_id": 1}])],
        ignore_index=True,
    )
    data["xp"] = pd.DataFrame(
        [
            {"element_id": e, "gw": 6, "model_version": m, "xp": x, "computed_at": BEFORE}
            for m, preds in (("a", {1: 6.0, 4: 3.0, 2: 1.0}), ("b", {1: 1.0, 4: 3.0, 2: 6.0}))
            for e, x in preds.items()
        ]
    )

    weekly = weekly_scores(graded_frame(**data)[0]).set_index("model_version")

    assert weekly.loc["a", "spearman"] > 0.9
    assert weekly.loc["b", "spearman"] < -0.9
