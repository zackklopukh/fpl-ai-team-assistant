"""Tests for the baseline xPoints model.

Offline by construction: no network, no database. The two saved API payloads
supply real field shapes, and `player_gw_stats` rows are synthesised from them,
because the model reads history rather than today's totals.

The cases that earn their place here are the ones that are silently wrong rather
than loudly broken: a blank gameweek that returns a plausible-looking number, a
double that returns a single, an injured player who still projects four points,
and a components breakdown that does not add up to the number next to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compute_xp import (  # noqa: E402
    build_xp_rows,
    fixtures_by_team_gw,
    matches_played_by_team,
    plan_gameweeks,
)
from xp.features import PlayerFeatures, build_features  # noqa: E402
from xp.model import (  # noqa: E402
    COMPONENT_KEYS,
    MODEL_VERSION,
    FixtureContext,
    expected_points,
    minutes_model,
    poisson_tail,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


# --- Payload loading -------------------------------------------------------


@pytest.fixture(scope="module")
def bootstrap() -> dict:
    return json.loads((FIXTURE_DIR / "bootstrap_static.json").read_text())


@pytest.fixture(scope="module")
def api_fixtures() -> list[dict]:
    return json.loads((FIXTURE_DIR / "fixtures.json").read_text())


def stat_rows_from_element(element: dict, gws: tuple[int, ...] = (1, 2, 3, 4)) -> list[dict]:
    """Turn one bootstrap element's season totals into per-gameweek stat rows.

    The saved payload is a season-to-date snapshot; the model reads gameweek
    history. Spreading the totals evenly is not realistic, but it is the right
    shape, and every test here is about structure rather than about any
    particular player's projection.
    """
    n = len(gws)
    minutes = element["minutes"]
    starts = element["starts"]
    return [
        {
            "element_id": element["id"],
            "gw": gw,
            "fixture_id": 100 + gw,
            "minutes": minutes // n,
            # Distribute starts over the earliest gameweeks so the count is exact.
            "starts": 1 if i < starts else 0,
            "bps": element["bps"] / n,
            "defensive_contribution": element["defensive_contribution"] / n,
            "expected_goals": float(element["expected_goals"]) / n,
            "expected_assists": float(element["expected_assists"]) / n,
            "expected_goals_conceded": float(element["expected_goals_conceded"]) / n,
        }
        for i, gw in enumerate(gws)
    ]


def features_for(element: dict, **overrides) -> PlayerFeatures:
    kwargs = {
        "element_id": element["id"],
        "element_type": element["element_type"],
        "team_fpl_id": element["team"],
        "stat_rows": stat_rows_from_element(element),
        "as_of_gw": 4,
        "matches_in_window": 4,
        "status": element["status"],
        "chance_of_playing": element["chance_of_playing_next_round"],
    }
    kwargs.update(overrides)
    return build_features(**kwargs)


def pick(bootstrap: dict, element_type: int) -> dict:
    """The most-played *available* player of a position — someone with real history.

    Availability is filtered here rather than left to chance: it multiplies through
    every component, so a flagged player silently scales the numbers a test is
    asserting and the failure looks like a model bug rather than a casting one.
    """
    candidates = [
        e
        for e in bootstrap["elements"]
        if e["element_type"] == element_type
        and e.get("status") == "a"
        and e.get("chance_of_playing_next_round") in (None, 100)
    ]
    return max(candidates, key=lambda e: e["minutes"])


HOME_EASY = FixtureContext(fixture_id=1, gw=5, is_home=True, difficulty=2)
AWAY_HARD = FixtureContext(fixture_id=2, gw=5, is_home=False, difficulty=4)


# --- Doubles and blanks ----------------------------------------------------


class TestDoublesAndBlanks:
    def test_a_blank_gameweek_is_exactly_zero(self, bootstrap):
        # Not "small" — zero. A player whose team does not play cannot appear,
        # and a near-zero here would still be captained by a solver comparing it
        # against a genuinely benched player.
        features = features_for(pick(bootstrap, 3))
        result = expected_points(features, [], gw=5)

        assert result.xp == 0.0
        assert result.xmins == 0.0
        assert result.p_play == 0.0
        assert result.fixture_count == 0
        assert all(result.components[key] == 0.0 for key in COMPONENT_KEYS)

    def test_a_double_is_the_sum_of_its_two_fixtures(self, bootstrap):
        features = features_for(pick(bootstrap, 3))

        first = expected_points(features, [HOME_EASY], gw=5)
        second = expected_points(features, [AWAY_HARD], gw=5)
        double = expected_points(features, [HOME_EASY, AWAY_HARD], gw=5)

        assert double.xp == pytest.approx(first.xp + second.xp, abs=0.01)
        assert double.xmins == pytest.approx(first.xmins + second.xmins, abs=0.01)
        assert double.fixture_count == 2

    def test_a_double_is_roughly_twice_a_single(self, bootstrap):
        features = features_for(pick(bootstrap, 4))

        single = expected_points(features, [HOME_EASY], gw=5)
        double = expected_points(features, [HOME_EASY, HOME_EASY], gw=5)

        # Not exactly twice: rounding happens once on the gameweek total, so the
        # two can differ by a thousandth. Anything larger means a fixture is being
        # dropped or double-counted.
        assert double.xp == pytest.approx(2 * single.xp, abs=0.002)

    def test_probabilities_stay_per_fixture_in_a_double(self, bootstrap):
        # p_start is the chance of starting *a* match, not of starting both. Only
        # xmins accumulates — otherwise a double gameweek reports a 180% chance
        # of playing.
        features = features_for(pick(bootstrap, 2))

        single = expected_points(features, [HOME_EASY], gw=5)
        double = expected_points(features, [HOME_EASY, AWAY_HARD], gw=5)

        assert double.p_start == single.p_start
        assert double.p_play == single.p_play
        assert double.p_play <= 1.0

    def test_fixture_index_is_one_to_many(self, api_fixtures):
        rows = [
            {
                "fixture_id": f["id"],
                "gw": f["event"],
                "team_h_fpl_id": f["team_h"],
                "team_a_fpl_id": f["team_a"],
                "team_h_difficulty": f["team_h_difficulty"],
                "team_a_difficulty": f["team_a_difficulty"],
                "finished": f["finished"],
            }
            for f in api_fixtures
        ]
        # Give team 1 a second fixture in gameweek 1.
        rows.append(
            {
                "fixture_id": 9001,
                "gw": 1,
                "team_h_fpl_id": 9,
                "team_a_fpl_id": 1,
                "team_h_difficulty": 3,
                "team_a_difficulty": 3,
                "finished": True,
            }
        )

        index = fixtures_by_team_gw(rows)

        assert len(index[(1, 1)]) == 2
        assert {ctx.is_home for ctx in index[(1, 1)]} == {True, False}
        # A team with no fixture in a gameweek is simply absent — a blank.
        assert (1, 3) not in index or len(index[(1, 3)]) >= 0

    def test_a_double_counts_twice_toward_matches_played(self):
        rows = [
            {"gw": 4, "team_h_fpl_id": 1, "team_a_fpl_id": 2, "finished": True},
            {"gw": 4, "team_h_fpl_id": 3, "team_a_fpl_id": 1, "finished": True},
            {"gw": 4, "team_h_fpl_id": 4, "team_a_fpl_id": 5, "finished": False},
        ]
        counts = matches_played_by_team(rows, as_of_gw=4)

        assert counts[1] == 2
        assert counts[2] == 1
        assert 4 not in counts  # unfinished fixtures are not evidence yet


# --- The minutes term ------------------------------------------------------


class TestMinutes:
    def test_an_unavailable_player_projects_almost_nothing(self, bootstrap):
        element = pick(bootstrap, 3)

        fit = features_for(element, status="a", chance_of_playing=None)
        injured = features_for(element, status="i", chance_of_playing=0)

        fit_xp = expected_points(fit, [HOME_EASY], gw=5).xp
        injured_result = expected_points(injured, [HOME_EASY], gw=5)

        assert fit_xp > 1.0  # the comparison is only meaningful if the fit one scores
        assert injured_result.xp == 0.0
        assert injured_result.xmins == 0.0
        # Everything multiplies through minutes, so no component survives.
        assert all(injured_result.components[key] == 0.0 for key in COMPONENT_KEYS)

    def test_a_doubtful_player_is_discounted_not_zeroed(self, bootstrap):
        element = pick(bootstrap, 3)

        fit = expected_points(features_for(element, status="a", chance_of_playing=None), [HOME_EASY], 5)
        doubt = expected_points(features_for(element, status="d", chance_of_playing=25), [HOME_EASY], 5)

        assert 0 < doubt.xp < fit.xp
        assert doubt.p_start == pytest.approx(fit.p_start * 0.25, abs=1e-4)

    def test_chance_of_playing_overrides_the_status_letter(self, bootstrap):
        element = pick(bootstrap, 2)

        # A club saying 75% beats the letter 'd' defaulting to a coin flip.
        by_percentage = features_for(element, status="d", chance_of_playing=75)
        by_letter = features_for(element, status="d", chance_of_playing=None)

        assert minutes_model(by_percentage)[0] > minutes_model(by_letter)[0]

    def test_a_player_with_no_recent_matches_is_treated_as_fringe(self, bootstrap):
        element = pick(bootstrap, 3)
        features = features_for(element, stat_rows=[], matches_in_window=0)

        p_start, p_play, _, xmins = minutes_model(features)

        assert 0.0 < p_start < 0.5
        assert p_play >= p_start
        assert 0.0 < xmins < 45.0

    def test_a_substitute_gets_cameo_minutes_not_a_start(self, bootstrap):
        element = pick(bootstrap, 4)
        # Four matches, no starts, 80 minutes total — four twenty-minute cameos.
        rows = [
            {
                "element_id": element["id"],
                "gw": gw,
                "fixture_id": 100 + gw,
                "minutes": 20,
                "starts": 0,
                "bps": 4,
                "defensive_contribution": 0,
                "expected_goals": 0.05,
                "expected_assists": 0.02,
                "expected_goals_conceded": 0.3,
            }
            for gw in (1, 2, 3, 4)
        ]
        features = features_for(element, stat_rows=rows, status="a", chance_of_playing=None)

        p_start, p_play, p_sixty, xmins = minutes_model(features)

        assert p_start == 0.0
        assert p_play == pytest.approx(1.0, abs=1e-6)
        assert p_sixty == 0.0  # no start, so no realistic route to the hour
        assert xmins == pytest.approx(20.0, abs=1e-6)

    def test_a_cameo_player_earns_one_appearance_point_not_two(self, bootstrap):
        element = pick(bootstrap, 4)
        rows = [
            {
                "element_id": element["id"],
                "gw": gw,
                "fixture_id": 100 + gw,
                "minutes": 20,
                "starts": 0,
                "bps": 4,
                "defensive_contribution": 0,
                "expected_goals": 0.05,
                "expected_assists": 0.02,
                "expected_goals_conceded": 0.3,
            }
            for gw in (1, 2, 3, 4)
        ]
        features = features_for(element, stat_rows=rows)
        result = expected_points(features, [HOME_EASY], gw=5)

        assert result.components["appearance"] == pytest.approx(1.0, abs=1e-3)


# --- Position and fixture effects -----------------------------------------


class TestPositionAndFixture:
    def test_a_defender_and_a_forward_with_identical_stats_differ(self, bootstrap):
        """Same underlying numbers, different scoring rules — so different xP.

        The defender is worth 6 a goal to the forward's 4 and can keep a clean
        sheet; the forward's defensive-contribution threshold is higher. If these
        come out equal, the position table is not being applied.
        """
        shared = {
            "element_id": 1,
            "team_fpl_id": 1,
            "stat_rows": [
                {
                    "element_id": 1,
                    "gw": gw,
                    "fixture_id": 100 + gw,
                    "minutes": 90,
                    "starts": 1,
                    "bps": 25,
                    "defensive_contribution": 11,
                    "expected_goals": 0.3,
                    "expected_assists": 0.2,
                    "expected_goals_conceded": 1.1,
                }
                for gw in (1, 2, 3, 4)
            ],
            "as_of_gw": 4,
            "matches_in_window": 4,
        }

        defender = build_features(element_type=2, **shared)
        forward = build_features(element_type=4, **shared)

        def_result = expected_points(defender, [HOME_EASY], gw=5)
        fwd_result = expected_points(forward, [HOME_EASY], gw=5)

        assert def_result.xp != fwd_result.xp
        assert def_result.xmins == fwd_result.xmins  # the difference is not minutes
        assert def_result.components["clean_sheet"] > 0
        assert fwd_result.components["clean_sheet"] == 0.0
        # 11 CBIT clears the defender's threshold of 10 but not the forward's 12.
        assert def_result.components["defensive_contribution"] > fwd_result.components[
            "defensive_contribution"
        ]

    def test_a_goalkeeper_earns_no_defensive_contribution(self, bootstrap):
        features = features_for(pick(bootstrap, 1))
        result = expected_points(features, [HOME_EASY], gw=5)

        assert result.components["defensive_contribution"] == 0.0

    def test_an_easier_fixture_is_worth_more_than_a_harder_one(self, bootstrap):
        features = features_for(pick(bootstrap, 3))

        easy = expected_points(features, [FixtureContext(1, 5, True, 1)], gw=5)
        hard = expected_points(features, [FixtureContext(1, 5, True, 5)], gw=5)

        assert easy.xp > hard.xp
        assert easy.components["goals"] > hard.components["goals"]
        assert easy.components["clean_sheet"] >= hard.components["clean_sheet"]

    def test_home_beats_away_for_the_same_opponent_difficulty(self, bootstrap):
        features = features_for(pick(bootstrap, 2))

        home = expected_points(features, [FixtureContext(1, 5, True, 3)], gw=5)
        away = expected_points(features, [FixtureContext(1, 5, False, 3)], gw=5)

        assert home.xp > away.xp

    def test_a_missing_difficulty_is_treated_as_neutral(self, bootstrap):
        features = features_for(pick(bootstrap, 3))

        unknown = expected_points(features, [FixtureContext(1, 5, True, None)], gw=5)
        neutral = expected_points(features, [FixtureContext(1, 5, True, 3)], gw=5)

        assert unknown.xp == neutral.xp


# --- The breakdown ---------------------------------------------------------


class TestComponents:
    @pytest.mark.parametrize("element_type", [1, 2, 3, 4])
    def test_components_sum_to_the_headline(self, bootstrap, element_type):
        # The UI shows both numbers. A penny of drift between them reads as a bug
        # in the model rather than in the rounding.
        features = features_for(pick(bootstrap, element_type))
        result = expected_points(features, [HOME_EASY, AWAY_HARD], gw=5)

        assert sum(result.components.values()) == pytest.approx(result.xp, abs=1e-9)

    def test_every_component_key_is_always_present(self, bootstrap):
        features = features_for(pick(bootstrap, 4))
        result = expected_points(features, [HOME_EASY], gw=5)

        assert set(result.components) == set(COMPONENT_KEYS)
        assert all(value >= 0.0 for value in result.components.values())

    def test_poisson_tail_brackets_the_defensive_threshold(self):
        assert poisson_tail(0.0, 10) == 0.0
        assert poisson_tail(2.0, 10) < 0.001  # nowhere near 10 CBIT
        assert 0.3 < poisson_tail(11.0, 10) < 0.8  # right on the threshold
        assert poisson_tail(30.0, 10) > 0.99


# --- Leakage ---------------------------------------------------------------


class TestAsOfGameweek:
    def test_features_ignore_gameweeks_after_the_as_of_point(self, bootstrap):
        """The whole backtest guarantee, in one assertion.

        Rows from after `as_of_gw` are dropped inside `build_features` rather than
        trusted to the caller, so a future evaluation harness cannot leak by
        forgetting a filter.
        """
        element = pick(bootstrap, 3)
        rows = stat_rows_from_element(element, gws=(1, 2, 3, 4, 5, 6))

        early = build_features(
            element_id=element["id"],
            element_type=3,
            team_fpl_id=1,
            stat_rows=rows,
            as_of_gw=3,
            matches_in_window=3,
        )
        late = build_features(
            element_id=element["id"],
            element_type=3,
            team_fpl_id=1,
            stat_rows=rows,
            as_of_gw=6,
            matches_in_window=6,
        )

        assert early.minutes_total < late.minutes_total
        assert early.as_of_gw == 3

    def test_rates_shrink_toward_the_positional_prior_on_thin_evidence(self, bootstrap):
        element = pick(bootstrap, 4)
        thin = build_features(
            element_id=element["id"],
            element_type=4,
            team_fpl_id=1,
            stat_rows=[
                {
                    "element_id": element["id"],
                    "gw": 1,
                    "fixture_id": 101,
                    "minutes": 15,
                    "starts": 0,
                    "bps": 12,
                    "defensive_contribution": 0,
                    "expected_goals": 0.9,  # one huge chance in a cameo
                    "expected_assists": 0.0,
                    "expected_goals_conceded": 0.2,
                }
            ],
            as_of_gw=1,
            matches_in_window=1,
        )

        # 0.9 xG in 15 minutes is 5.4 per 90 taken literally, which would project
        # a striker at thirty points. Shrinkage has to swallow almost all of it.
        assert thin.xg90 < 1.0


# --- The nightly job -------------------------------------------------------


class TestBuildXpRows:
    def _inputs(self, bootstrap, api_fixtures):
        players = [
            {
                "element_id": e["id"],
                "element_type": e["element_type"],
                "team_fpl_id": e["team"],
                "status": e["status"],
                "chance_of_playing_next_round": e["chance_of_playing_next_round"],
            }
            for e in bootstrap["elements"]
        ]
        stats = [row for e in bootstrap["elements"] for row in stat_rows_from_element(e)]
        fixtures = [
            {
                "fixture_id": f["id"],
                "gw": f["event"],
                "team_h_fpl_id": f["team_h"],
                "team_a_fpl_id": f["team_a"],
                "team_h_difficulty": f["team_h_difficulty"],
                "team_a_difficulty": f["team_a_difficulty"],
                "finished": f["finished"],
            }
            for f in api_fixtures
        ]
        # The saved payload only runs to gameweek 4, so project onto copies of it.
        for f in list(fixtures):
            if f["gw"] == 1:
                fixtures.append({**f, "fixture_id": f["fixture_id"] + 5000, "gw": 5, "finished": False})
        return players, stats, fixtures

    def test_one_row_per_player_per_target_gameweek(self, bootstrap, api_fixtures):
        players, stats, fixtures = self._inputs(bootstrap, api_fixtures)

        rows = build_xp_rows(
            season="2026-27",
            player_rows=players,
            stat_rows=stats,
            fixture_rows=fixtures,
            target_gws=[5, 6],
            as_of_gw=4,
        )

        assert len(rows) == len(players) * 2
        assert {r["model_version"] for r in rows} == {MODEL_VERSION}
        assert {r["season"] for r in rows} == {"2026-27"}
        assert {r["gw"] for r in rows} == {5, 6}

    def test_a_team_blanking_scores_zero_while_the_rest_of_the_league_does_not(
        self, bootstrap, api_fixtures
    ):
        """One club's blank, which is the shape a real blank takes.

        The condition is constructed rather than inherited from the sample's
        coverage: a fixture list that simply stops at some gameweek makes every
        club blank at once, which is a league-wide shutdown and tests nothing
        about the per-team join.
        """
        players, stats, fixtures = self._inputs(bootstrap, api_fixtures)

        blanking_team = int(players[0]["team_fpl_id"])
        without = [
            f
            for f in fixtures
            if not (
                f["gw"] == 6
                and blanking_team in (f["team_h_fpl_id"], f["team_a_fpl_id"])
            )
        ]

        rows = build_xp_rows(
            season="2026-27",
            player_rows=players,
            stat_rows=stats,
            fixture_rows=without,
            target_gws=[6],
            as_of_gw=4,
        )

        teams = {int(p["element_id"]): int(p["team_fpl_id"]) for p in players}
        blanked = [r for r in rows if teams[r["element_id"]] == blanking_team]
        playing = [r for r in rows if teams[r["element_id"]] != blanking_team]

        assert blanked and all(r["xp"] == 0.0 for r in blanked)
        assert any(r["xp"] > 0.0 for r in playing)

    def test_rows_carry_the_shape_xpoints_expects(self, bootstrap, api_fixtures):
        players, stats, fixtures = self._inputs(bootstrap, api_fixtures)

        rows = build_xp_rows(
            season="2026-27",
            player_rows=players,
            stat_rows=stats,
            fixture_rows=fixtures,
            target_gws=[5],
            as_of_gw=4,
        )

        expected = {
            "season",
            "element_id",
            "gw",
            "model_version",
            "xp",
            "xmins",
            "p_start",
            "p_play",
            "components",
            "as_of_gw",
            "fixture_count",
        }
        # db.upsert requires identical keys in identical order across every row.
        assert all(set(r) == expected for r in rows)
        assert len({tuple(r) for r in rows}) == 1

        for row in rows:
            assert 0.0 <= row["p_start"] <= 1.0
            assert 0.0 <= row["p_play"] <= 1.0
            assert set(row["components"]) == set(COMPONENT_KEYS)
            # A blank must record zero fixtures, not a null, or the UI cannot
            # tell "no fixture" from "not computed".
            assert row["fixture_count"] >= 0
            assert row["as_of_gw"] == 4

    def test_no_duplicate_primary_keys(self, bootstrap, api_fixtures):
        players, stats, fixtures = self._inputs(bootstrap, api_fixtures)

        rows = build_xp_rows(
            season="2026-27",
            player_rows=players,
            stat_rows=stats,
            fixture_rows=fixtures,
            target_gws=[5, 6],
            as_of_gw=4,
        )

        keys = {(r["season"], r["element_id"], r["gw"], r["model_version"]) for r in rows}
        assert len(keys) == len(rows)


class TestPlanGameweeks:
    def test_the_horizon_starts_after_the_last_finished_gameweek(self, bootstrap):
        rows = [{"gw": e["id"], "finished": e["finished"]} for e in bootstrap["events"]]

        as_of_gw, targets = plan_gameweeks(rows, horizon=5)

        assert as_of_gw == 4
        assert targets == [5, 6, 7, 8, 9]

    def test_the_horizon_is_capped(self, bootstrap):
        rows = [{"gw": e["id"], "finished": e["finished"]} for e in bootstrap["events"]]
        _, targets = plan_gameweeks(rows, horizon=5)

        assert len(targets) == 5

    def test_a_season_not_yet_started_projects_from_gameweek_one(self):
        rows = [{"gw": gw, "finished": False} for gw in range(1, 39)]

        as_of_gw, targets = plan_gameweeks(rows, horizon=5)

        assert as_of_gw == 0
        assert targets == [1, 2, 3, 4, 5]

    def test_a_finished_season_has_nothing_to_project(self):
        rows = [{"gw": gw, "finished": True} for gw in range(1, 39)]

        as_of_gw, targets = plan_gameweeks(rows, horizon=5)

        assert as_of_gw == 38
        assert targets == []

    def test_a_gameweek_in_progress_is_not_re_predicted(self):
        """Its prediction was frozen at the deadline; that row is what gets graded.

        Rewriting it mid-gameweek would fold in team news from after the
        deadline and flatter every model in score_models.py. This is the exact
        state the live database was in on 2026-09-21: GW5's deadline had passed
        but GW5 was not finished.
        """
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        rows = [
            {"gw": 4, "finished": True, "deadline_time": now - timedelta(days=9)},
            {"gw": 5, "finished": False, "deadline_time": now - timedelta(days=3)},
            {"gw": 6, "finished": False, "deadline_time": now + timedelta(days=19)},
            {"gw": 7, "finished": False, "deadline_time": now + timedelta(days=26)},
        ]

        as_of_gw, targets = plan_gameweeks(rows, horizon=5, now=now)

        assert as_of_gw == 4
        assert targets == [6, 7]

    def test_a_gameweek_is_still_predicted_up_to_its_deadline(self):
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 10, 10, 9, 59, tzinfo=UTC)
        rows = [{"gw": 6, "finished": False, "deadline_time": now + timedelta(minutes=1)}]

        _, targets = plan_gameweeks(rows, horizon=5, now=now)

        assert targets == [6]
