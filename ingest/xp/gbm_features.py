"""Leak-free features for the gradient-boosting xP model, one row per fixture.

The whole design rests on one rule: the features on a row describe what was
knowable before that row's gameweek deadline, and nothing from the row itself or
from any other fixture in the same gameweek. Training and prediction go through
the same function — `build_features` is handed the history rows plus, when
predicting, a set of *pseudo rows* for the target gameweek whose stat columns are
empty — so a feature that leaked in training would leak identically in the
backtest and be caught by the tests, rather than silently diverge.

How that is guaranteed rather than hoped for:

* A player's rows are sorted in time and every window is computed from an
  exclusive prefix sum that stops at the **first row of the row's own gameweek**
  — not at the row itself. Stopping at the row would let the second fixture of a
  double see the first one's result, which is exactly the "rolling window that
  accidentally includes the current row" leak, one row removed.
* Team strength is a decayed average over results strictly before the target
  gameweek, joined with `merge_asof(..., allow_exact_matches=False)`.
* Price and ownership come from the player's previous row in the same season,
  which is how `AsOf.players` defines them too.
* `fpl_xp` is never read. It is post-hoc (see db/schema.sql) and a tree would
  latch onto it.
* Defensive contribution is NaN before 2025-26 and stays NaN: its windows divide
  by the number of rows that *have* a value, so a window with none is missing,
  not zero.

Players are followed across seasons by `code`, never by element id, which FPL
reassigns every summer. Clubs are followed by team `code` for the same reason.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

# Stat columns the windows are built from.
WINDOW_STATS = (
    "total_points",
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "bps",
    "bonus",
    "ict_index",
    "influence",
    "creativity",
    "threat",
    "goals_scored",
    "assists",
    "clean_sheets",
    "saves",
    "defensive_contribution",
)

# Short names keep the feature list readable in importance tables.
SHORT = {
    "total_points": "pts",
    "minutes": "mins",
    "starts": "starts",
    "expected_goals": "xg",
    "expected_assists": "xa",
    "expected_goal_involvements": "xgi",
    "expected_goals_conceded": "xgc",
    "bps": "bps",
    "bonus": "bonus",
    "ict_index": "ict",
    "influence": "inf",
    "creativity": "cre",
    "threat": "thr",
    "goals_scored": "goals",
    "assists": "assists",
    "clean_sheets": "cs",
    "saves": "saves",
    "defensive_contribution": "defcon",
}

# Per-fixture means over the player's last N rows. A row exists for every
# registered player whose team plays, so a row window is "his club's last N
# fixtures while he was on the books", minutes or not: it measures role as well
# as output.
ROW_WINDOWS = (1, 3, 5, 10)
ROW_WINDOW_STATS = {
    1: ("total_points", "minutes", "starts", "expected_goal_involvements", "bps"),
    3: ("total_points", "minutes", "starts", "expected_goal_involvements", "bps", "bonus"),
    5: WINDOW_STATS,
    10: WINDOW_STATS,
}

# Per-90 rates over the last N *appearances*: output when he plays, which a row
# window mixes up with how often he plays.
APPEARANCE_WINDOWS = (5, 15)
PER90_STATS = (
    "total_points",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "bps",
    "bonus",
    "threat",
    "creativity",
    "defensive_contribution",
)
# Pseudo-minutes added to every per-90 denominator: a 3-minute cameo with one
# shot must not read as a 2.0 xG/90 player. Shrinks toward zero; the tree sees
# the minutes too and can undo the shrink where the sample is large.
PER90_PSEUDO_MINUTES = 90.0

# Season aggregates: this season to date, and the whole previous season.
SEASON_STATS = (
    "total_points",
    "minutes",
    "starts",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "bps",
    "bonus",
    "defensive_contribution",
)

# Team strength: decayed per-match averages, carried across seasons by club code.
TEAM_HALFLIFE_MATCHES = 10.0

# Premier League expected goals per team per match, roughly stable across
# seasons (~1.3-1.5). Only a scale for the fixture products below.
LEAGUE_XG_PER_TEAM_MATCH = 1.4

# Minutes classes, the first stage of the two-part model.
MIN_CLASS_NONE, MIN_CLASS_SUB, MIN_CLASS_SHORT_START, MIN_CLASS_FULL_START = 0, 1, 2, 3

ROW_KEY = ["code", "season", "gw", "fixture_id"]


def minutes_class(minutes: pd.Series, starts: pd.Series) -> pd.Series:
    """0 did not play, 1 came off the bench, 2 started but <60, 3 started and 60+."""
    m = pd.to_numeric(minutes).fillna(0).to_numpy()
    s = pd.to_numeric(starts).fillna(0).to_numpy()
    cls = np.where(m <= 0, 0, np.where(s <= 0, 1, np.where(m < 60, 2, 3)))
    return pd.Series(cls, index=minutes.index, dtype="int64")


def _season_index(seasons: Sequence[str]) -> dict[str, int]:
    return {s: i for i, s in enumerate(sorted(set(seasons)))}


# --- Team strength -----------------------------------------------------------


def team_strength(fixtures: pd.DataFrame, stats: pd.DataFrame, season_idx: dict[str, int]) -> pd.DataFrame:
    """Decayed goals and xG for/against per match, per club, after each gameweek.

    One row per (team_code, time) where time = season_index * 100 + gw, holding
    the state *after* that gameweek's results. Callers join with a strict
    as-of lookup, so a gameweek never sees its own results.

    Goals come from fixture results (blanked in `AsOf` from the target onward);
    xG is the sum of the club's players' expected goals in that fixture, from
    stat rows, which `AsOf` also cuts at the target.
    """
    f = fixtures[
        fixtures["team_h_score"].notna()
        & fixtures["team_a_score"].notna()
        & fixtures["gw"].notna()
        & fixtures["season"].isin(list(season_idx))
    ]
    if f.empty:
        return pd.DataFrame(columns=["team_code", "t", "gf", "ga", "xgf", "xga", "matches"])

    xg = (
        stats[stats["team_code"].notna()]
        .groupby(["season", "fixture_id", "team_code"])["expected_goals"]
        .sum()
    )
    xg_map = {k: float(v) for k, v in xg.items()}

    records = []
    for r in f.itertuples(index=False):
        if pd.isna(r.team_h_code) or pd.isna(r.team_a_code):
            continue
        t = season_idx[r.season] * 100 + int(r.gw)
        h, a = int(r.team_h_code), int(r.team_a_code)
        xh = xg_map.get((r.season, r.fixture_id, h), np.nan)
        xa = xg_map.get((r.season, r.fixture_id, a), np.nan)
        hs, as_ = float(r.team_h_score), float(r.team_a_score)
        records.append((h, t, hs, as_, xh, xa))
        records.append((a, t, as_, hs, xa, xh))
    m = pd.DataFrame(records, columns=["team_code", "t", "gf", "ga", "xgf", "xga"])
    m["xg_n"] = m["xgf"].notna() & m["xga"].notna()
    m[["xgf", "xga"]] = m[["xgf", "xga"]].fillna(0.0)
    per_gw = (
        m.groupby(["team_code", "t"])
        .agg(gf=("gf", "sum"), ga=("ga", "sum"), xgf=("xgf", "sum"), xga=("xga", "sum"),
             n=("gf", "size"), xn=("xg_n", "sum"))
        .reset_index()
        .sort_values(["team_code", "t"])
    )

    decay = 0.5 ** (1.0 / TEAM_HALFLIFE_MATCHES)
    out = []
    for team, g in per_gw.groupby("team_code", sort=False):
        s = np.zeros(4)
        w = 0.0
        wx = 0.0
        total = 0
        for row in g.itertuples(index=False):
            k = decay ** row.n
            s[:2] = s[:2] * k + (row.gf, row.ga)
            w = w * k + row.n
            s[2:] = s[2:] * (decay ** row.xn) + (row.xgf, row.xga)
            wx = wx * (decay ** row.xn) + row.xn
            total += row.n
            out.append(
                (
                    team,
                    row.t,
                    s[0] / w,
                    s[1] / w,
                    s[2] / wx if wx > 0 else np.nan,
                    s[3] / wx if wx > 0 else np.nan,
                    total,
                )
            )
    return pd.DataFrame(out, columns=["team_code", "t", "gf", "ga", "xgf", "xga", "matches"])


def _attach_team(rows: pd.DataFrame, strength: pd.DataFrame, key: str, prefix: str) -> pd.DataFrame:
    """Join each row's club (or opponent) state strictly before the row's gameweek."""
    cols = ["gf", "ga", "xgf", "xga", "matches"]
    left = rows[[key, "t"]].copy()
    left["_order"] = np.arange(len(left))
    known = left[left[key].notna()].copy()
    known[key] = known[key].astype("int64")
    known = known.sort_values("t")
    if strength.empty or known.empty:
        for c in cols:
            rows[f"{prefix}_{c}"] = np.nan
        return rows
    right = strength.rename(columns={"team_code": key}).copy()
    right[key] = right[key].astype("int64")
    right["t"] = right["t"].astype("int64")
    known["t"] = known["t"].astype("int64")
    right = right.sort_values("t")
    joined = pd.merge_asof(
        known, right, on="t", by=key, allow_exact_matches=False, direction="backward"
    )
    joined = joined.set_index("_order")
    for c in cols:
        values = np.full(len(rows), np.nan)
        values[joined.index.to_numpy()] = joined[c].to_numpy(dtype="float64")
        rows[f"{prefix}_{c}"] = values
    return rows


# --- Player windows ------------------------------------------------------------


def _prefix(values: np.ndarray) -> np.ndarray:
    """Exclusive prefix sum with a leading zero: P[i] = sum(values[:i])."""
    return np.concatenate([[0.0], np.cumsum(values)])


def build_features(
    stats: pd.DataFrame,
    fixtures: pd.DataFrame,
    targets: pd.DataFrame | None = None,
    team_stats: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Features for every history row and every target pseudo row.

    `stats` — `AsOf.stats` (or any frame with the same columns): the rows the
    model may learn from, all strictly before the target.
    `fixtures` — `AsOf.fixtures`, results blanked from the target onward.
    `targets` — optional pseudo rows for the gameweek being predicted, with
    `code, season, gw, fixture_id, element_type, team_code, opp_code, was_home`
    and no stats. Their stat columns are ignored even if present.
    `team_stats` — the rows team xG is summed from, when `stats` has been cut
    down to the players being predicted; defaults to `stats`.

    Returns one row per input row with `ROW_KEY`, `is_target`, the feature
    columns (see `feature_columns`), and for history rows the targets `y`
    (points) and `min_class`.
    """
    hist = stats.copy()
    hist["is_target"] = False
    frames = [hist]
    if targets is not None and len(targets):
        tgt = targets.copy()
        for col in WINDOW_STATS + ("value_tenths", "selected_by"):
            tgt[col] = np.nan
        tgt["is_target"] = True
        frames.append(tgt)
    seasons = pd.concat([f["season"] for f in frames])
    season_idx = _season_index(seasons.tolist())

    # Opponent club code for history rows, from the row's own fixture.
    if "opp_code" not in hist.columns:
        fx = fixtures[["season", "fixture_id", "team_h_code", "team_a_code"]]
        hist = hist.merge(fx, on=["season", "fixture_id"], how="left")
        home = hist["team_code"] == hist["team_h_code"]
        hist["opp_code"] = hist["team_a_code"].where(home, hist["team_h_code"])
        hist = hist.drop(columns=["team_h_code", "team_a_code"])
        frames[0] = hist

    keep = list(
        dict.fromkeys(
            ROW_KEY
            + ["element_type", "team_code", "opp_code", "was_home", "is_target"]
            + list(WINDOW_STATS)
            + ["value_tenths", "selected_by"]
        )
    )
    df = pd.concat([f[[c for c in keep if c in f.columns]] for f in frames], ignore_index=True)
    df["season_i"] = df["season"].map(season_idx).astype("int64")
    df["t"] = df["season_i"] * 100 + df["gw"].astype("int64")
    df = df.sort_values(["code", "t", "fixture_id"], kind="mergesort").reset_index(drop=True)

    n = len(df)
    idx = np.arange(n)
    code = df["code"].to_numpy()
    # First row of the player, of the player-season, and of the player-gameweek.
    player_start = df.assign(_i=idx).groupby("code", sort=False)["_i"].transform("min").to_numpy()
    season_start = (
        df.assign(_i=idx).groupby(["code", "season_i"], sort=False)["_i"].transform("min").to_numpy()
    )
    gw_first = df.assign(_i=idx).groupby(["code", "t"], sort=False)["_i"].transform("min").to_numpy()

    feats: dict[str, np.ndarray] = {}

    # Values used by the windows. Target rows hold NaN, but they sit at the end of
    # each player's sequence and every window stops before its own gameweek, so
    # they are never summed; zeroing them just keeps prefix sums finite.
    vals = {}
    present = {}
    for col in WINDOW_STATS:
        v = pd.to_numeric(df[col], errors="coerce").astype("float64").to_numpy()
        present[col] = (~np.isnan(v)).astype("float64")
        vals[col] = np.nan_to_num(v, nan=0.0)
    minutes = vals["minutes"]
    appeared = (minutes > 0).astype("float64")

    prefixes = {col: _prefix(vals[col]) for col in WINDOW_STATS}
    counts = {col: _prefix(present[col]) for col in WINDOW_STATS}

    def window_mean(col: str, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        total = prefixes[col][hi] - prefixes[col][lo]
        cnt = counts[col][hi] - counts[col][lo]
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, total / np.where(cnt > 0, cnt, 1), np.nan)

    hi = gw_first  # every window ends just before the row's own gameweek
    for w in ROW_WINDOWS:
        lo = np.maximum(hi - w, player_start)
        for col in ROW_WINDOW_STATS[w]:
            feats[f"{SHORT[col]}_r{w}"] = window_mean(col, lo, hi)
        # How many rows the window actually holds: 3 means "3 of 10 known".
        feats[f"rows_r{w}"] = (hi - lo).astype("float64")

    # Appearance windows: the same prefix trick over the appearance-only sequence.
    app_prefix = _prefix(appeared)
    apps_before = app_prefix[hi]  # appearances strictly before the row's gameweek
    apps_at_player_start = app_prefix[player_start]
    app_rows = np.flatnonzero(appeared > 0)
    app_vals = {col: _prefix(vals[col][app_rows]) for col in PER90_STATS + ("minutes",)}
    app_cnt = {col: _prefix(present[col][app_rows]) for col in PER90_STATS}
    app_mins_present = {
        col: _prefix(vals["minutes"][app_rows] * present[col][app_rows]) for col in PER90_STATS
    }
    a_hi = apps_before.astype("int64")
    for w in APPEARANCE_WINDOWS:
        a_lo = np.maximum(a_hi - w, apps_at_player_start.astype("int64"))
        napp = (a_hi - a_lo).astype("float64")
        mins = app_vals["minutes"][a_hi] - app_vals["minutes"][a_lo]
        feats[f"apps_a{w}"] = napp
        with np.errstate(invalid="ignore", divide="ignore"):
            feats[f"mins_per_app_a{w}"] = np.where(napp > 0, mins / np.where(napp > 0, napp, 1), np.nan)
        for col in PER90_STATS:
            total = app_vals[col][a_hi] - app_vals[col][a_lo]
            m_present = app_mins_present[col][a_hi] - app_mins_present[col][a_lo]
            cnt = app_cnt[col][a_hi] - app_cnt[col][a_lo]
            with np.errstate(invalid="ignore", divide="ignore"):
                feats[f"{SHORT[col]}90_a{w}"] = np.where(
                    cnt > 0, total * 90.0 / (m_present + PER90_PSEUDO_MINUTES), np.nan
                )

    # This season to date.
    lo = season_start
    rows_season = (hi - lo).astype("float64")
    feats["rows_season"] = rows_season
    feats["apps_season"] = app_prefix[hi] - app_prefix[lo]
    for col in SEASON_STATS:
        feats[f"{SHORT[col]}_season"] = window_mean(col, lo, hi)

    # Recency of involvement: rows since he last appeared and last started. A
    # player back from injury shows a run of blank rows ending recently.
    last_app = _last_index_before(appeared > 0, code, hi)
    last_start = _last_index_before(vals["starts"] > 0, code, hi)
    feats["rows_since_app"] = np.where(last_app >= 0, hi - 1 - last_app, np.nan).astype("float64")
    feats["rows_since_start"] = np.where(last_start >= 0, hi - 1 - last_start, np.nan).astype("float64")
    feats["rows_career"] = (hi - player_start).astype("float64")

    # The whole previous season, via the code link. Missing if he was not in the
    # league then.
    prev = _previous_season(df, vals, present, appeared)
    key = pd.MultiIndex.from_arrays([df["code"].to_numpy(), df["season_i"].to_numpy() - 1])
    prev = prev.reindex(key)
    for c in prev.columns:
        feats[c] = prev[c].to_numpy(dtype="float64")

    # Point-in-time price and ownership: his previous row, same season. Prices
    # reset every summer, so gameweek 1 has none — the same rule as AsOf.players.
    prev_row = hi - 1
    same_season = (prev_row >= season_start) & (prev_row >= 0)
    price = pd.to_numeric(df["value_tenths"], errors="coerce").astype("float64").to_numpy()
    sel = pd.to_numeric(df["selected_by"], errors="coerce").astype("float64").to_numpy()
    safe_prev = np.clip(prev_row, 0, None)
    feats["price"] = np.where(same_season, price[safe_prev], np.nan)
    start_price = np.where(season_start < hi, price[np.clip(season_start, 0, n - 1)], np.nan)
    feats["price_change"] = feats["price"] - start_price
    ownership = np.where(same_season, sel[safe_prev], np.nan)
    feats["log_owned"] = np.log1p(ownership)

    df_feats = pd.DataFrame(feats, index=df.index)
    out = pd.concat([df[ROW_KEY + ["element_type", "team_code", "opp_code", "was_home", "is_target", "t"]], df_feats], axis=1)

    # Ownership as a percentile within the gameweek: the raw count drifts with the
    # size of the game from season to season, its rank does not.
    out["owned_pct"] = out.groupby("t")["log_owned"].rank(pct=True)

    out["position"] = out["element_type"].astype("float64")
    out["home"] = out["was_home"].astype("float64")
    out["gw_num"] = out["gw"].astype("float64")
    # Rule era, not a result: defensive contribution scores from 2025-26.
    out["defcon_era"] = (out["season"] >= "2025-26").astype("float64")
    # Fixtures his club has in this gameweek: rotation is heavier in a double.
    out["gw_fixtures"] = out.groupby(["code", "t"])["fixture_id"].transform("size").astype("float64")

    strength = team_strength(fixtures, stats if team_stats is None else team_stats, season_idx)
    out = _attach_team(out, strength, "team_code", "team")
    out = _attach_team(out, strength, "opp_code", "opp")
    out["att_vs_def"] = out["team_xgf"] - out["opp_xga"]
    out["def_vs_att"] = out["team_xga"] - out["opp_xgf"]
    # This fixture's expected goals each way, multiplicative in the two sides'
    # strengths (attack x opponent's leakiness / league average) — a product a
    # tree can only approximate with many splits. The clean-sheet chance is the
    # Poisson zero of the goals against, which is how a defender's biggest
    # return is actually priced.
    # The league average is a fixed constant, not a mean over the strength table:
    # that mean would include results after an early training row's gameweek.
    out["fx_xg_for"] = out["team_xgf"] * out["opp_xga"] / LEAGUE_XG_PER_TEAM_MATCH
    out["fx_xg_against"] = out["team_xga"] * out["opp_xgf"] / LEAGUE_XG_PER_TEAM_MATCH
    out["fx_cs_prob"] = np.exp(-out["fx_xg_against"])

    if "total_points" in df:
        target = ~out["is_target"].to_numpy()
        out["y"] = np.where(target, df["total_points"].astype("float64"), np.nan)
        cls = minutes_class(df["minutes"], df["starts"])
        out["min_class"] = np.where(target, cls, -1)
        out["y_minutes"] = np.where(target, df["minutes"].astype("float64"), np.nan)
    return out


def _last_index_before(flag: np.ndarray, code: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Global index of the player's last flagged row strictly before `hi`, or -1."""
    n = len(flag)
    marked = np.where(flag, np.arange(n), -1)
    # Running max, reset at each player boundary so one player never sees another.
    starts = np.r_[True, code[1:] != code[:-1]]
    group = np.cumsum(starts) - 1
    running = pd.Series(marked).groupby(group).cummax().to_numpy()
    prev = hi - 1
    ok = prev >= 0
    result = np.full(n, -1)
    result[ok] = running[prev[ok]]
    # A value from a different player (prev crossed a boundary) is not his.
    crossed = ok & (group[np.clip(prev, 0, None)] != group)
    result[crossed] = -1
    return result


def _previous_season(
    df: pd.DataFrame, vals: dict[str, np.ndarray], present: dict[str, np.ndarray], appeared: np.ndarray
) -> pd.DataFrame:
    """Per (code, season_i): whole-season aggregates, to be read by season_i + 1."""
    hist = ~df["is_target"].to_numpy()
    frame = pd.DataFrame(
        {
            "code": df["code"].to_numpy()[hist],
            "season_i": df["season_i"].to_numpy()[hist],
            "app": appeared[hist],
            **{c: vals[c][hist] for c in SEASON_STATS},
            **{f"_n_{c}": present[c][hist] for c in SEASON_STATS},
        }
    )
    g = frame.groupby(["code", "season_i"])
    sums = g.sum()
    rows = g.size()
    out = pd.DataFrame(index=sums.index)
    out["prev_rows"] = rows
    out["prev_apps"] = sums["app"]
    mins = sums["minutes"]
    for c in SEASON_STATS:
        n = sums[f"_n_{c}"]
        if c == "minutes":
            out["prev_mins_per_row"] = mins / rows
            continue
        if c == "starts":
            out["prev_start_rate"] = sums[c] / rows
            continue
        per90 = sums[c] * 90.0 / (mins + PER90_PSEUDO_MINUTES)
        out[f"prev_{SHORT[c]}90"] = per90.where(n > 0)
    out["prev_pts_per_row"] = sums["total_points"] / rows
    return out


# Identity and bookkeeping columns, never fed to the model.
NON_FEATURES = frozenset(
    ROW_KEY
    + [
        "element_type",
        "team_code",
        "opp_code",
        "was_home",
        "is_target",
        "t",
        "y",
        "min_class",
        "y_minutes",
        "log_owned",
    ]
)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if c not in NON_FEATURES]
