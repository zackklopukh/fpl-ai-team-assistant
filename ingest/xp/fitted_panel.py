"""Point-in-time features for the fitted model, built from an `AsOf` and nothing else.

The fitted model learns its parameters by asking, for every past fixture row,
"what would the model have known before this gameweek's deadline, and what then
happened?". That only means something if the features for a past row are built
from strictly earlier rows, exactly as the features for the target are. So there
is one code path for both: the target is appended to the history as a virtual
player-gameweek with no outcome, and every feature is an *exclusive* running sum
— a row never sees itself, nor anything in its own gameweek (the second fixture
of a double shares the first one's deadline).

Two kinds of state are built here:

* **Team ratings** (`team_ratings`): a multiplicative Poisson model of team xG,
  `lambda = mu * home^(+-1) * attack[team] * defence[opponent]`, fitted as of
  every gameweek from earlier fixtures only, with exponential time decay and
  shrinkage toward a prior. Promoted clubs start from the average rating of the
  clubs they replaced, which is what a promoted side has historically looked
  like rather than a league-average guess.
* **Player sums** (`player_panel`): recency-weighted sums of starts, minutes,
  xG, xA, saves and so on, per player-gameweek, linked across seasons by `code`.
  Previous seasons are down-weighted by a separate factor on top of the decay,
  because a summer changes roles more than a week does.

Everything is vectorised: a decayed exclusive sum is a group cumulative sum of
`x * d^-k` rescaled by `d^k`, so a full refit costs about a second.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Columns summed per player-gameweek. A double gameweek contributes both fixtures.
_SUM_COLUMNS = (
    "minutes",
    "starts",
    "n_fx",
    "goals_scored",
    "assists",
    "expected_goals",
    "expected_assists",
    "xg_adj",
    "xa_adj",
    "min90",
    "saves",
    "saves_adj",
    "gk_min90",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "bps",
    "bonus",
    "dc",
    "dc_min90",
    "start_minutes",
    "start_60",
    "sub_apps",
    "sub_minutes",
    "non_starts",
)


@dataclass(frozen=True)
class DecaySpec:
    """How quickly one family of evidence goes stale.

    `per_gw` multiplies an observation's weight once per player-gameweek that
    follows it; `per_season` multiplies it again for each season boundary between
    it and the moment of prediction.
    """

    per_gw: float
    per_season: float


# --- Helpers -----------------------------------------------------------------


def season_index(seasons: list[str]) -> dict[str, int]:
    return {s: i for i, s in enumerate(sorted(set(seasons)))}


def decayed_exclusive(
    df: pd.DataFrame, group: str, k: str, sidx: str, columns: list[str], decay: DecaySpec
) -> pd.DataFrame:
    """Sum of each column over *earlier* rows of the same group, recency-weighted.

    Row t sees `sum_{i<t} x_i * d^(k_t-k_i) * s^(season_t-season_i)`. Written as a
    cumulative sum of `x * d^-k * s^-season`, so it is one pass however long the
    history. `df` must be sorted by group then time. Exponents stay small (a
    player has at most a few hundred gameweeks in the data), well inside float64.
    """
    d, s = decay.per_gw, decay.per_season
    kk = df[k].to_numpy(dtype=float)
    ss = df[sidx].to_numpy(dtype=float)
    up = np.power(d, -kk) * np.power(s, -ss)
    down = np.power(d, kk) * np.power(s, ss)
    values = df[columns].to_numpy(dtype=float) * up[:, None]
    cum = pd.DataFrame(values, index=df.index).groupby(df[group].to_numpy()).cumsum().to_numpy()
    out = (cum - values) * down[:, None]
    # Clip the tiny negatives that float cancellation can leave where the sum is 0.
    return pd.DataFrame(np.maximum(out, 0.0), index=df.index, columns=columns)


# --- Team ratings --------------------------------------------------------------


@dataclass(frozen=True)
class TeamRatings:
    """Ratings as of each time point: before that gameweek's deadline.

    `ratings` has one row per (time, team_code) with `attack` and `defence`
    (1.0 is league average; defence above 1 means leakier). `league` has one row
    per time with `mu` (mean team xG per match at a neutral venue), `home_adv` (the
    fitted venue multiplier: home sides get mu*home, away sides mu/home) and
    `goals_per_xg` (how many real goals a unit of xG has been worth).
    """

    ratings: pd.DataFrame
    league: pd.DataFrame


def team_fixture_frame(stats: pd.DataFrame, fixtures: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    """One row per (fixture, team) played so far: xG for and against, goals, venue.

    xG comes from summing the player rows, because that is the quantity the
    player model uses; goals come from the fixture score.
    """
    rows = stats[stats["team_code"].notna()]
    per = (
        rows.groupby(["season", "gw", "fixture_id", "team_code"], as_index=False)
        .agg(xg=("expected_goals", "sum"), was_home=("was_home", "first"))
    )
    per["team_code"] = per["team_code"].astype("int64")
    other = per[["season", "fixture_id", "team_code", "xg"]].rename(
        columns={"team_code": "opp_code", "xg": "xga"}
    )
    tf = per.merge(other, on=["season", "fixture_id"], how="inner")
    tf = tf[tf["team_code"] != tf["opp_code"]]

    scores = fixtures[["season", "fixture_id", "team_h_score", "team_a_score"]].copy()
    tf = tf.merge(scores, on=["season", "fixture_id"], how="left")
    home = tf["was_home"].fillna(False).astype(bool)
    hs = pd.to_numeric(tf["team_h_score"], errors="coerce")
    as_ = pd.to_numeric(tf["team_a_score"], errors="coerce")
    tf["goals"] = np.where(home, hs, as_)
    tf["goals_against"] = np.where(home, as_, hs)
    tf["home"] = home
    return tf.drop(columns=["team_h_score", "team_a_score", "was_home"]).reset_index(drop=True)


def team_ratings(
    team_fx: pd.DataFrame,
    times: pd.DataFrame,
    teams: pd.DataFrame,
    decay: DecaySpec,
    prior_matches: float,
    iterations: int = 4,
) -> TeamRatings:
    """Fit attack/defence ratings as of every time point in `times`.

    `times` has columns `season`, `gw`, `t` (a global ordinal). For each, only
    team-fixtures strictly earlier are used. The fit is the weighted Poisson
    maximum-likelihood solution for a multiplicative model, found by iterative
    proportional fitting — each rating is observed xG over what an average team
    would have produced against the same opponents at the same venues — with
    `prior_matches` pseudo-matches pulling it toward its prior. Cheap enough to
    redo from scratch at each time point, which keeps it obviously leak-free.
    """
    seasons = sorted(set(times["season"]) | set(team_fx["season"]))
    sidx = season_index(seasons)
    season_teams = {
        s: set(int(c) for c in teams.loc[teams["season"] == s, "code"]) for s in seasons
    }
    tf = team_fx.copy()
    tf["sidx"] = tf["season"].map(sidx)
    tf = tf.merge(times[["season", "gw", "t"]], on=["season", "gw"], how="left")
    tf = tf[tf["t"].notna()]
    tf_t = tf["t"].to_numpy(dtype=float)
    tf_s = tf["sidx"].to_numpy(dtype=float)
    xg = tf["xg"].to_numpy(dtype=float)
    goals = tf["goals"].to_numpy(dtype=float)
    home = tf["home"].to_numpy(dtype=bool)
    team = tf["team_code"].to_numpy()
    opp = tf["opp_code"].to_numpy()

    all_codes = sorted(set(int(c) for c in teams["code"]) | set(int(c) for c in team))
    pos = {c: i for i, c in enumerate(all_codes)}
    ti = np.array([pos[int(c)] for c in team], dtype=int)
    oi = np.array([pos[int(c)] for c in opp], dtype=int)
    n = len(all_codes)

    rating_rows = []
    league_rows = []
    promoted_prior: dict[int, tuple[float, float]] = {}
    last_by_season: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    for row in times.sort_values("t").itertuples(index=False):
        t, s = float(row.t), sidx[row.season]
        mask = tf_t < t
        att = np.ones(n)
        dfn = np.ones(n)
        mu, h, gpx = 1.35, 1.1, 1.0
        # The prior for each club: 1.0 if it was in the league last season,
        # otherwise the average of the clubs that left (the ones it replaced).
        prior_att = np.ones(n)
        prior_def = np.ones(n)
        prev = s - 1
        if prev in last_by_season and row.season in season_teams:
            prev_season = seasons[prev]
            left = season_teams.get(prev_season, set()) - season_teams[row.season]
            arrived = season_teams[row.season] - season_teams.get(prev_season, set())
            pa, pd_ = last_by_season[prev]
            if left:
                li = [pos[c] for c in left]
                promoted_prior[s] = (float(pa[li].mean()), float(pd_[li].mean()))
            if s in promoted_prior:
                for c in arrived:
                    prior_att[pos[c]], prior_def[pos[c]] = promoted_prior[s]

        if mask.any():
            w = np.power(decay.per_gw, t - tf_t[mask]) * np.power(decay.per_season, s - tf_s[mask])
            x, hm, a_i, o_i = xg[mask], home[mask], ti[mask], oi[mask]
            mu = float((w * x).sum() / w.sum())
            home_rate = (w * x * hm).sum() / max((w * hm).sum(), 1e-9)
            away_rate = (w * x * ~hm).sum() / max((w * ~hm).sum(), 1e-9)
            h = float(np.sqrt(home_rate / away_rate)) if away_rate > 0 else 1.0
            g = goals[mask]
            ok = ~np.isnan(g)
            gpx = float((w[ok] * g[ok]).sum() / max((w[ok] * x[ok]).sum(), 1e-9)) if ok.any() else 1.0
            venue = np.where(hm, h, 1.0 / h)
            k = prior_matches * mu
            for _ in range(iterations):
                exp_for = w * mu * venue * dfn[o_i]
                att = (np.bincount(a_i, w * x, n) + k * prior_att) / (np.bincount(a_i, exp_for, n) + k)
                # Defence of team T is read off the rows where T is the opponent:
                # the xG its opponents produced against it.
                exp_against = w * mu * venue * att[a_i]
                dfn = (np.bincount(o_i, w * x, n) + k * prior_def) / (np.bincount(o_i, exp_against, n) + k)
            # Keep the scale identified: average attack 1 across current clubs.
            current = [pos[c] for c in season_teams.get(row.season, set()) if c in pos] or list(range(n))
            scale = att[current].mean()
            att /= scale
            dfn *= scale
        else:
            att, dfn = prior_att.copy(), prior_def.copy()

        last_by_season[s] = (att.copy(), dfn.copy())
        league_rows.append({"t": t, "mu": mu, "home_adv": h, "goals_per_xg": gpx})
        rating_rows.append(pd.DataFrame({"t": t, "team_code": all_codes, "attack": att, "defence": dfn}))

    ratings = pd.concat(rating_rows, ignore_index=True)
    league = pd.DataFrame(league_rows)
    return TeamRatings(ratings=ratings, league=league)


def fixture_expectations(
    rows: pd.DataFrame, ratings: TeamRatings
) -> pd.DataFrame:
    """Attach expected xG for and against to fixture rows.

    `rows` needs `t`, `team_code`, `opp_code`, `is_home`. Adds `lam_for`, `lam_against`
    (team xG), `att_mult` (venue x opponent defence: how much easier than average
    this fixture is to create chances in), `opp_att_mult` (venue x opponent
    attack, for saves) and `goals_per_xg`.
    """
    r = ratings.ratings
    league = ratings.league
    out = rows.merge(league, on="t", how="left")
    own = r.rename(columns={"attack": "own_att", "defence": "own_def"})
    out = out.merge(own, on=["t", "team_code"], how="left")
    opp = r.rename(columns={"team_code": "opp_code", "attack": "opp_att", "defence": "opp_def"})
    out = out.merge(opp, on=["t", "opp_code"], how="left")
    for c in ("own_att", "own_def", "opp_att", "opp_def"):
        out[c] = out[c].fillna(1.0)
    out["mu"] = out["mu"].fillna(1.35)
    out["home_adv"] = out["home_adv"].fillna(1.1)
    out["goals_per_xg"] = out["goals_per_xg"].fillna(1.0)
    venue = np.where(out["is_home"].astype(bool), out["home_adv"], 1.0 / out["home_adv"])
    out["att_mult"] = venue * out["opp_def"]
    out["opp_att_mult"] = (1.0 / venue) * out["opp_att"]
    out["lam_for"] = out["mu"] * venue * out["own_att"] * out["opp_def"]
    out["lam_against"] = out["mu"] / venue * out["opp_att"] * out["own_def"]
    return out


# --- The panel -------------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    """Everything the fitted model reads, as of one deadline.

    * `pgw` — one row per player-gameweek (historical and the virtual target
      row), with the decayed sums of everything strictly before it. The target
      rows have `is_target` set and no outcomes.
    * `rows` — historical fixture rows (the training set): outcomes, plus the
      fixture's team expectations as they stood before that gameweek, plus a
      `pgw_id` linking to the features.
    * `target` — one row per player per target fixture, with the same team
      expectations as of the deadline and a `pgw_id` to the player's target row.
      A blank contributes no row; a double contributes two.
    * `ratings` — the team ratings the expectations came from, for inspection.
    """

    pgw: pd.DataFrame
    rows: pd.DataFrame
    target: pd.DataFrame
    ratings: TeamRatings
    first_season: str


def _fixture_sides(stats: pd.DataFrame, fixtures: pd.DataFrame, code_of) -> pd.DataFrame:
    """Venue and opponent club code for each stat row, from the fixture itself."""
    sides = fixtures[
        ["season", "fixture_id", "team_h_fpl_id", "team_a_fpl_id", "team_h_score", "team_a_score"]
    ]
    s = stats.merge(sides, on=["season", "fixture_id"], how="left")
    team = pd.to_numeric(s["team_fpl_id"], errors="coerce")
    is_home = (team == s["team_h_fpl_id"]).fillna(False).astype(bool)
    opp_fpl = np.where(is_home, s["team_a_fpl_id"], s["team_h_fpl_id"])
    s["is_home"] = is_home
    s["opp_code"] = [code_of(se, o) for se, o in zip(s["season"], opp_fpl, strict=True)]
    # The result is known for every row here: AsOf only holds rows before the target.
    s["team_goals_against"] = np.where(
        is_home,
        pd.to_numeric(s["team_a_score"], errors="coerce"),
        pd.to_numeric(s["team_h_score"], errors="coerce"),
    )
    return s.drop(columns=["team_h_fpl_id", "team_a_fpl_id", "team_h_score", "team_a_score"])


def build_panel(
    asof,
    horizon_gws: list[int],
    minutes_fast: DecaySpec,
    minutes_slow: DecaySpec,
    rates: DecaySpec,
    team_decay: DecaySpec,
    team_prior_matches: float,
) -> Panel:
    """Build features for every past row and for the target, from `asof` alone."""
    code_map = {
        (str(se), int(f)): int(c)
        for se, f, c in zip(asof.teams["season"], asof.teams["fpl_id"], asof.teams["code"], strict=True)
    }

    def code_of(season, fpl_id):
        if fpl_id is None or pd.isna(fpl_id):
            return np.nan
        return code_map.get((str(season), int(fpl_id)), np.nan)

    seasons = sorted(set(asof.stats["season"]) | {asof.season})
    sidx = season_index(seasons)

    s = asof.stats.copy()
    s = _fixture_sides(s, asof.fixtures, code_of)
    s = s[s["team_code"].notna() & s["opp_code"].notna()].copy()
    s["team_code"] = s["team_code"].astype("int64")
    s["opp_code"] = s["opp_code"].astype("int64")
    s["sidx"] = s["season"].map(sidx)

    # Global time: one tick per (season, gameweek) that has rows, plus the target.
    times = pd.concat(
        [s[["season", "gw"]], pd.DataFrame({"season": [asof.season], "gw": [asof.gw]})]
    ).drop_duplicates()
    times["sidx"] = times["season"].map(sidx)
    times = times.sort_values(["sidx", "gw"]).reset_index(drop=True)
    times["t"] = np.arange(len(times), dtype=float)
    t_target = float(times.loc[(times["season"] == asof.season) & (times["gw"] == asof.gw), "t"].iloc[0])

    tfx = team_fixture_frame(s, asof.fixtures, asof.teams)
    ratings = team_ratings(tfx, times, asof.teams, team_decay, team_prior_matches)

    s = s.merge(times[["season", "gw", "t"]], on=["season", "gw"], how="left")
    s = fixture_expectations(s, ratings)

    gk = (s["element_type"] == 1).to_numpy()
    s["n_fx"] = 1
    s["min90"] = s["minutes"] / 90.0
    s["xg_adj"] = s["expected_goals"].fillna(0.0) / s["att_mult"]
    s["xa_adj"] = s["expected_assists"].fillna(0.0) / s["att_mult"]
    s["saves_adj"] = np.where(gk, s["saves"] / s["opp_att_mult"], 0.0)
    s["gk_min90"] = np.where(gk, s["min90"], 0.0)
    dc_known = s["defensive_contribution"].notna()
    s["dc"] = s["defensive_contribution"].astype("float64").fillna(0.0)
    s["dc_min90"] = np.where(dc_known, s["min90"], 0.0)
    started = s["starts"] > 0
    s["start_minutes"] = np.where(started, s["minutes"], 0)
    s["start_60"] = (started & (s["minutes"] >= 60)).astype(int)
    s["sub_apps"] = ((~started) & (s["minutes"] > 0)).astype(int)
    s["sub_minutes"] = np.where(~started & (s["minutes"] > 0), s["minutes"], 0)
    s["non_starts"] = (~started).astype(int)

    agg = {c: (c, "sum") for c in _SUM_COLUMNS}
    agg.update(
        element_type=("element_type", "last"),
        value_tenths=("value_tenths", "last"),
        sidx=("sidx", "first"),
    )
    pgw = s.groupby(["code", "season", "gw"], as_index=False, sort=False).agg(**agg)
    pgw["is_target"] = False

    players = asof.players
    tgt = pd.DataFrame(
        {
            "code": players["code"].astype("int64"),
            "season": asof.season,
            "gw": asof.gw,
            "element_type": players["element_type"].astype("int64"),
            "value_tenths": players["price_tenths"].astype("Float64"),
            "sidx": sidx[asof.season],
            "is_target": True,
            "element_id": players["element_id"].astype("int64"),
        }
    )
    for c in _SUM_COLUMNS:
        tgt[c] = 0.0
    pgw["element_id"] = -1
    pgw = pd.concat([pgw, tgt], ignore_index=True)
    pgw["value_tenths"] = pd.to_numeric(pgw["value_tenths"], errors="coerce").astype("float64")
    pgw = pgw.sort_values(["code", "sidx", "gw", "is_target"], kind="mergesort").reset_index(drop=True)
    pgw["pgw_id"] = np.arange(len(pgw))
    g = pgw.groupby("code", sort=False)
    pgw["k"] = g.cumcount().astype(float)

    fast_cols = ["starts", "n_fx", "sub_apps", "non_starts"]
    slow_cols = ["starts", "n_fx", "sub_apps", "non_starts", "start_minutes", "start_60", "sub_minutes"]
    rate_cols = [
        "xg_adj",
        "xa_adj",
        "min90",
        "saves_adj",
        "gk_min90",
        "yellow_cards",
        "red_cards",
        "own_goals",
        "penalties_missed",
        "penalties_saved",
        "bps",
        "bonus",
        "dc",
        "dc_min90",
        "expected_goals",
    ]
    for prefix, cols, spec in (
        ("f_", fast_cols, minutes_fast),
        ("s_", slow_cols, minutes_slow),
        ("r_", rate_cols, rates),
    ):
        sums = decayed_exclusive(pgw, "code", "k", "sidx", cols, spec)
        for c in cols:
            pgw[prefix + c] = sums[c].to_numpy()

    # The most recent player-gameweek before this one, whatever season it was in.
    prev = g.shift(1)
    pgw["has_prev"] = prev["n_fx"].notna()
    pgw["last_start"] = (prev["starts"] / prev["n_fx"]).fillna(0.0)
    pgw["last_min90"] = (prev["minutes"] / (90.0 * prev["n_fx"])).fillna(0.0)
    pgw["last_new_season"] = (prev["sidx"] != pgw["sidx"]).fillna(True).astype(float)
    # Point-in-time price: the target row carries AsOf's; any row without one uses
    # the last price the player had (last season's, at a gameweek 1).
    last_price = prev["value_tenths"].groupby(pgw["code"]).ffill()
    pgw["price"] = pgw["value_tenths"].where(pgw["is_target"], np.nan)
    pgw["price"] = pgw["price"].fillna(last_price)
    # Historical rows use their own gameweek's price (point-in-time in the data).
    hist_rows = ~pgw["is_target"]
    pgw.loc[hist_rows, "price"] = pgw.loc[hist_rows, "value_tenths"].fillna(last_price[hist_rows])

    # Consecutive player-gameweeks immediately before this one without minutes,
    # and without a start: a long absence is the injury signal the data does have.
    played = (pgw["minutes"] > 0).to_numpy()
    started_any = (pgw["starts"] > 0).to_numpy()
    pgw["absent_streak"] = _streak(pgw, played)
    pgw["benched_streak"] = _streak(pgw, started_any)
    pgw["season_rows"] = pgw.groupby(["code", "sidx"], sort=False).cumcount().astype(float)

    rows = s.merge(
        pgw.loc[~pgw["is_target"], ["code", "season", "gw", "pgw_id"]],
        on=["code", "season", "gw"],
        how="left",
    )

    target = _target_fixtures(asof, horizon_gws, pgw, t_target, code_of)
    target = fixture_expectations(target, ratings)

    return Panel(pgw=pgw, rows=rows, target=target, ratings=ratings, first_season=seasons[0])


def _streak(pgw: pd.DataFrame, event: np.ndarray) -> np.ndarray:
    """For each row, how many earlier consecutive rows of its player lacked `event`."""
    k = pgw["k"].to_numpy()
    marker = pd.Series(np.where(event, k, np.nan), index=pgw.index)
    last_event = marker.groupby(pgw["code"]).shift(1)
    last_event = last_event.groupby(pgw["code"]).ffill()
    streak = np.where(last_event.isna(), k, k - last_event.to_numpy() - 1)
    return np.asarray(streak, dtype=float)


def _target_fixtures(asof, horizon_gws, pgw, t_target, code_of) -> pd.DataFrame:
    """One row per registered player per fixture in each target gameweek."""
    f = asof.fixtures[(asof.fixtures["season"] == asof.season) & asof.fixtures["gw"].isin(horizon_gws)]
    sides = pd.concat(
        [
            pd.DataFrame(
                {
                    "fixture_id": f["fixture_id"],
                    "gw": f["gw"].astype("int64"),
                    "team_fpl_id": f["team_h_fpl_id"],
                    "opp_fpl_id": f["team_a_fpl_id"],
                    "is_home": True,
                }
            ),
            pd.DataFrame(
                {
                    "fixture_id": f["fixture_id"],
                    "gw": f["gw"].astype("int64"),
                    "team_fpl_id": f["team_a_fpl_id"],
                    "opp_fpl_id": f["team_h_fpl_id"],
                    "is_home": False,
                }
            ),
        ],
        ignore_index=True,
    )
    players = asof.players[asof.players["team_fpl_id"].notna()][
        ["element_id", "code", "element_type", "team_fpl_id"]
    ].copy()
    players["team_fpl_id"] = players["team_fpl_id"].astype("int64")
    out = players.merge(sides, on="team_fpl_id", how="inner")
    out["team_code"] = [code_of(asof.season, t) for t in out["team_fpl_id"]]
    out["opp_code"] = [code_of(asof.season, t) for t in out["opp_fpl_id"]]
    out["t"] = t_target
    ids = pgw.loc[pgw["is_target"], ["element_id", "pgw_id"]]
    out = out.merge(ids, on="element_id", how="left")
    return out.reset_index(drop=True)
