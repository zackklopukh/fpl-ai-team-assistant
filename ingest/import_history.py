"""Import completed past seasons from vaastav/Fantasy-Premier-League.

The FPL API only ever exposes the season in progress, so the xP model's training
and backtest seasons (2023-24, 2024-25, 2025-26) come from the public dataset at
github.com/vaastav/Fantasy-Premier-League. Per season it reads four files —
teams.csv, fixtures.csv, players_raw.csv and gws/merged_gw.csv — and writes
`teams` -> `players` -> `fixtures` -> `player_gw_stats`, in that order because of
the foreign keys. Every write is an upsert on the natural key, so a re-run is safe.

The dataset declares no licence. The raw files are cached under a git-ignored
directory (`data/raw/history/` by default) and are never committed; only the tiny
trimmed samples under tests/fixtures/history_sample/ are.

    python ingest/import_history.py --dry-run
    python ingest/import_history.py --seasons 2025-26

!!! LEAKAGE WARNING — past-season `players` rows are IDENTITY-ONLY. !!!
------------------------------------------------------------------------

players_raw.csv is a snapshot taken at the END of the season: final price, final
total points, final form, final ownership, season-total xG. Using any of those as
a feature when predicting a gameweek of that season is the lookahead leak CLAUDE.md
forbids — it does not crash, it just makes every backtest number fiction.

So for 2023-24, 2024-25 and 2025-26, `players` rows carry identity and nothing else:

* SAFE: `code` (the only stable cross-season player identity — link seasons
  through it, never through element_id), `element_type`, `web_name`,
  `first_name`, `second_name`.
* `team_fpl_id` is the END-OF-SEASON club and is wrong for any gameweek before a
  mid-season transfer. Derive the club at a gameweek from `player_gw_stats`
  instead: join `fixtures` on (season, fixture_id) and take `team_h_fpl_id` when
  `was_home`, else `team_a_fpl_id`.
* `now_cost_tenths` is NOT NULL, so it holds the least-leaky price available: the
  `value` on the player's earliest merged_gw row that season (their price when
  first listed), falling back to players_raw's start cost (now_cost minus
  cost_change_start) for a player with no gameweek rows. It is still not a
  per-gameweek price — read `player_gw_stats.value_tenths` for that.
  `cost_change_start_tenths` is written 0 to match.
* Every season-to-date aggregate (minutes, total_points, form, points_per_game,
  selected_by_percent, expected_*) is left at its default (0 / null) rather than
  filled with the end-of-season figure. `status` is 'a' and news is null, because
  the end-of-season status says nothing about any past gameweek.

NEVER use a past-season `players` column other than the SAFE list as a backtest
feature. Train from `player_gw_stats`, which is per gameweek.

What the transform guarantees about `player_gw_stats`
------------------------------------------------------

* `season` is on every row, and element ids and team ids are never merged across
  seasons. Only the three history seasons are accepted; the live season
  (config.SEASON) is refused outright, so this job can never touch a 2026-27 row.
* `value_tenths` and `selected_by` are that row's own `value` and `selected` —
  the price and ownership during that gameweek. Integers; `value` is already
  tenths.
* `source = 'history'`, `bonus_settled = true` (the season is over).
* One row per player per fixture: a double gameweek gives two rows, a blank none.
* `opponent_team_fpl_id` is `opponent_team`; `was_home` is kept, so the player's
  own club at that gameweek is derivable from `fixtures` (see above). The import
  validates that derivation against every row before writing.

`fpl_xp`, FPL's own expected points (`xP`) — NOT a pre-deadline forecast
------------------------------------------------------------------------

The source captured this after each gameweek was played, and it already reflects
the result: a player who scores 10+ shows an xP about 3 points higher that week
than the week before, with no rise after. So a row's `fpl_xp` must never be used
to predict or score its own gameweek. The previous gameweek's value is the honest
stand-in for what FPL showed before a deadline — the backtest's `fpl_xp_lag`.

Two quirks in the source, both handled here:

1. **Missing gameweeks are recorded as 0, not blank.** Some gameweeks have xP = 0
   on every single row, which is impossible for a real projection (most of
   2025-26 after GW9 is like this). A gameweek whose rows are all exactly zero is
   treated as not captured and written as NULL. A real zero inside a captured
   gameweek is kept as 0.
2. **xP is a gameweek figure, repeated on both rows of a double.** Following the
   convention sync_live.py uses for gameweek-only values, it is written on the
   player's first fixture of the gameweek (by kickoff, then fixture id) and NULL
   on the second, so `sum(fpl_xp)` over a player's gameweek is the real
   projection rather than double it. Compare against FPL at the (element, gw)
   grain with sum() — do NOT filter rows with `fpl_xp is not null`, which would
   drop the second fixture's points.

Other source handling
---------------------

* Exact duplicate rows in merged_gw.csv are dropped (2025-26 has a few). Two rows
  with the same (element, GW, fixture) but different content abort the import.
* Assistant managers (element_type 5, 2024-25 only) are skipped: they are not
  players and the schema's element_type is 1-4.
* `defensive_contribution` only exists from 2025-26, when the scoring rule began.
  Earlier seasons write the column default 0, which means "not scored then".
* Team strengths in teams.csv are the pre-season snapshot. Fixture difficulty in
  fixtures.csv is as last published, which may have drifted during the season.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

JOB = "import_history"

# The only seasons this job will write. The live season is deliberately absent and
# additionally refused by `guard_season`, so a typo cannot overwrite live rows.
HISTORY_SEASONS = ("2023-24", "2024-25", "2025-26")

SOURCE_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/"
    "{season}/{path}"
)

# Local name -> path within the season directory upstream.
FILES = {
    "teams": "teams.csv",
    "fixtures": "fixtures.csv",
    "players": "players_raw.csv",
    "gws": "gws/merged_gw.csv",
}

DEFAULT_CACHE_DIR = config.REPO_ROOT / "data" / "raw" / "history"

# FPL's four playing positions. 5 is the 2024-25 assistant-manager chip.
PLAYER_ELEMENT_TYPES = frozenset({1, 2, 3, 4})

STAT_CONFLICT_KEYS = ("season", "element_id", "gw", "fixture_id")

# Rows per upsert batch; committed after each so a dropped connection loses little.
BATCH_SIZE = 5000

# Counted stats; names match the schema's columns and merged_gw's headers.
_INT_FIELDS = (
    "minutes",
    "total_points",
    "starts",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "defensive_contribution",  # absent before 2025-26 -> 0
)

_DECIMAL_FIELDS = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "influence",
    "creativity",
    "threat",
    "ict_index",
)


# --- Parsing helpers ---------------------------------------------------------


def _int(value: str | None, default: int | None = 0) -> int | None:
    if value is None or value == "":
        return default
    return int(value)


def _required_int(row: dict[str, str], key: str) -> int:
    value = row.get(key)
    if value is None or value == "":
        raise ValueError(f"missing required integer {key!r} in row {row!r}"[:500])
    return int(value)


def _decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


def _bool(value: str | None) -> bool | None:
    if value in (None, ""):
        return None
    if value in ("True", "true", "1"):
        return True
    if value in ("False", "false", "0"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def guard_season(season: str) -> None:
    """Refuse anything that is not a completed history season."""
    if season == config.SEASON:
        raise ValueError(
            f"{season} is the live season; import_history never writes it"
        )
    if season not in HISTORY_SEASONS:
        raise ValueError(
            f"{season} is not a supported history season {HISTORY_SEASONS}"
        )


# --- I/O: fetch and read -----------------------------------------------------


@dataclass
class SeasonFiles:
    """The four source files of one season, as lists of string dicts."""

    season: str
    teams: list[dict[str, str]]
    fixtures: list[dict[str, str]]
    players: list[dict[str, str]]
    gws: list[dict[str, str]]


def read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig tolerates a BOM; merged_gw names include accented characters.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def fetch(season: str, cache_dir: Path, *, download: bool = True) -> SeasonFiles:
    """Read a season's files from the cache, downloading any that are missing."""
    guard_season(season)
    season_dir = cache_dir / season
    loaded: dict[str, list[dict[str, str]]] = {}

    for name, rel in FILES.items():
        path = season_dir / rel
        if not path.exists():
            if not download:
                raise FileNotFoundError(path)
            _download(SOURCE_URL.format(season=season, path=rel), path)
        loaded[name] = read_csv(path)

    return SeasonFiles(season=season, **loaded)


def _download(url: str, path: Path) -> None:
    import httpx

    log.info("downloading %s", url)
    path.parent.mkdir(parents=True, exist_ok=True)
    response = httpx.get(
        url,
        headers={"User-Agent": config.USER_AGENT},
        timeout=120,
        follow_redirects=True,
    )
    response.raise_for_status()
    # Write to a temp name first so an interrupted download is never mistaken for
    # a cached file on the next run.
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(response.content)
    tmp.replace(path)


# --- Pure transforms ---------------------------------------------------------


def build_team_rows(season: str, teams: list[dict[str, str]]) -> list[dict[str, Any]]:
    """teams.csv -> `teams` rows. Strengths are the pre-season snapshot."""
    guard_season(season)
    return [
        {
            "season": season,
            "fpl_id": _required_int(team, "id"),
            "code": _required_int(team, "code"),
            "name": team["name"],
            "short_name": team["short_name"],
            "strength": _int(team.get("strength"), None),
            "strength_overall_home": _int(team.get("strength_overall_home"), None),
            "strength_overall_away": _int(team.get("strength_overall_away"), None),
            "strength_attack_home": _int(team.get("strength_attack_home"), None),
            "strength_attack_away": _int(team.get("strength_attack_away"), None),
            "strength_defence_home": _int(team.get("strength_defence_home"), None),
            "strength_defence_away": _int(team.get("strength_defence_away"), None),
        }
        for team in teams
    ]


def build_fixture_rows(
    season: str, fixtures: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """fixtures.csv -> `fixtures` rows. Same mapping as sync_fixtures."""
    guard_season(season)
    return [
        {
            "season": season,
            "fixture_id": _required_int(fixture, "id"),
            "gw": _int(fixture.get("event"), None),
            "team_h_fpl_id": _required_int(fixture, "team_h"),
            "team_a_fpl_id": _required_int(fixture, "team_a"),
            "team_h_difficulty": _int(fixture.get("team_h_difficulty"), None),
            "team_a_difficulty": _int(fixture.get("team_a_difficulty"), None),
            "kickoff_time": _timestamp(fixture.get("kickoff_time")),
            "started": bool(_bool(fixture.get("started"))),
            "finished": bool(_bool(fixture.get("finished"))),
            "finished_provisional": bool(_bool(fixture.get("finished_provisional"))),
            "minutes": _int(fixture.get("minutes")),
            "team_h_score": _int(fixture.get("team_h_score"), None),
            "team_a_score": _int(fixture.get("team_a_score"), None),
        }
        for fixture in fixtures
    ]


def excluded_elements(players: list[dict[str, str]]) -> set[int]:
    """Element ids that are not players (the 2024-25 assistant managers)."""
    return {
        _required_int(p, "id")
        for p in players
        if _required_int(p, "element_type") not in PLAYER_ELEMENT_TYPES
    }


def dedupe_gw_rows(gws: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Drop exact duplicate merged_gw rows; abort on conflicting duplicates.

    Returns (rows, number dropped). Two rows sharing (element, GW, fixture) but
    differing anywhere else cannot be resolved without guessing which is right,
    so that raises rather than picking one.
    """
    seen: dict[tuple[str, str, str], dict[str, str]] = {}
    kept: list[dict[str, str]] = []
    dropped = 0

    for row in gws:
        key = (row["element"], row["GW"], row["fixture"])
        previous = seen.get(key)
        if previous is None:
            seen[key] = row
            kept.append(row)
        elif previous == row:
            dropped += 1
        else:
            raise ValueError(
                f"conflicting merged_gw rows for element={key[0]} GW={key[1]} "
                f"fixture={key[2]}"
            )

    return kept, dropped


def missing_xp_gameweeks(gws: list[dict[str, str]]) -> set[int]:
    """Gameweeks where the source recorded xP = 0 (or blank) on every row.

    A real projection is never zero for every player, so this is the dataset's
    marker for "not captured". Those gameweeks get a NULL fpl_xp, not 0.
    """
    captured: dict[int, bool] = defaultdict(bool)
    for row in gws:
        gw = _required_int(row, "GW")
        xp = _decimal(row.get("xP"))
        captured[gw] = captured[gw] or (xp is not None and xp != 0)
    return {gw for gw, ok in captured.items() if not ok}


def _fixture_order(row: dict[str, str]) -> tuple[str, int]:
    return (row.get("kickoff_time") or "", _required_int(row, "fixture"))


def first_values(gws: list[dict[str, str]]) -> dict[int, int]:
    """element -> `value` on its earliest gameweek row: price when first listed."""
    earliest: dict[int, tuple[tuple[int, str, int], int]] = {}
    for row in gws:
        element = _required_int(row, "element")
        order = (_required_int(row, "GW"), *_fixture_order(row))
        value = _required_int(row, "value")
        if element not in earliest or order < earliest[element][0]:
            earliest[element] = (order, value)
    return {element: value for element, (_, value) in earliest.items()}


def build_player_rows(
    season: str,
    players: list[dict[str, str]],
    first_value: dict[int, int],
) -> list[dict[str, Any]]:
    """players_raw.csv -> identity-only `players` rows. See the module docstring.

    players_raw is end-of-season; nothing that describes performance, price or
    ownership is copied from it. Assistant managers are skipped.
    """
    guard_season(season)
    rows = []

    for player in players:
        element_id = _required_int(player, "id")
        if _required_int(player, "element_type") not in PLAYER_ELEMENT_TYPES:
            continue

        price = first_value.get(element_id)
        if price is None:
            # No gameweek rows at all: fall back to the season's start cost, which
            # is still a pre-season figure rather than the final price.
            price = _required_int(player, "now_cost") - _int(
                player.get("cost_change_start")
            )

        rows.append(
            {
                "season": season,
                "element_id": element_id,
                "code": _required_int(player, "code"),
                # END-OF-SEASON club. Per-gameweek club: fixtures + was_home.
                "team_fpl_id": _required_int(player, "team"),
                "element_type": _required_int(player, "element_type"),
                "web_name": player["web_name"],
                "first_name": player.get("first_name") or None,
                "second_name": player.get("second_name") or None,
                # Least-leaky NOT NULL price: first listed, not final.
                "now_cost_tenths": int(price),
                "cost_change_start_tenths": 0,
                "status": "a",
                "news": None,
                "news_added": None,
                "chance_of_playing_this_round": None,
                "chance_of_playing_next_round": None,
                # Season-to-date aggregates deliberately NOT copied from the
                # end-of-season file.
                "minutes": 0,
                "total_points": 0,
                "form": None,
                "points_per_game": None,
                "selected_by_percent": None,
                "expected_goals": None,
                "expected_assists": None,
                "expected_goal_involvements": None,
                "expected_goals_conceded": None,
            }
        )

    return rows


def build_stat_rows(
    season: str,
    gws: list[dict[str, str]],
    *,
    missing_xp: set[int] | frozenset[int] = frozenset(),
    excluded: set[int] | frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """merged_gw.csv -> `player_gw_stats` rows, one per player per fixture.

    Every value is the row's own: `value_tenths` and `selected_by` are the price
    and ownership during that gameweek, never looked up from `players`.
    `fpl_xp` goes on the first fixture of a double only (see module docstring).
    """
    guard_season(season)

    by_player_gw: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for row in gws:
        element = _required_int(row, "element")
        if element in excluded:
            continue
        by_player_gw[(element, _required_int(row, "GW"))].append(row)

    rows: list[dict[str, Any]] = []

    for (element, gw), entries in sorted(by_player_gw.items()):
        entries.sort(key=_fixture_order)

        for index, entry in enumerate(entries):
            if _required_int(entry, "round") != gw:
                raise ValueError(
                    f"element {element}: GW={gw} but round={entry['round']}"
                )

            out: dict[str, Any] = {
                "season": season,
                "element_id": element,
                "gw": gw,
                "fixture_id": _required_int(entry, "fixture"),
            }
            for name in _INT_FIELDS:
                out[name] = _int(entry.get(name))
            for name in _DECIMAL_FIELDS:
                out[name] = _decimal(entry.get(name))

            out["was_home"] = _bool(entry.get("was_home"))
            out["opponent_team_fpl_id"] = _int(entry.get("opponent_team"), None)

            # The price and ownership *during this gameweek*. This is what keeps
            # the history leak-free — never source them from players_raw.
            out["value_tenths"] = _required_int(entry, "value")
            out["selected_by"] = _int(entry.get("selected"), None)
            out["bonus_settled"] = True

            if gw in missing_xp or index > 0:
                out["fpl_xp"] = None
            else:
                out["fpl_xp"] = _decimal(entry.get("xP"))
            out["source"] = "history"

            rows.append(out)

    return rows


# --- Validation --------------------------------------------------------------


def validate(
    season: str,
    teams: list[dict[str, Any]],
    players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    stats: list[dict[str, Any]],
) -> list[str]:
    """Cross-table checks that would otherwise surface as a silently wrong table.

    Returns a list of problems; empty means safe to write.
    """
    problems: list[str] = []

    for table, rows in (
        ("teams", teams),
        ("players", players),
        ("fixtures", fixtures),
        ("player_gw_stats", stats),
    ):
        bad = sum(1 for r in rows if r["season"] != season)
        if bad:
            problems.append(f"{table}: {bad} rows with a season other than {season}")
    if season == config.SEASON or season not in HISTORY_SEASONS:
        problems.append(f"refusing to write season {season}")

    team_ids = {t["fpl_id"] for t in teams}
    if len(team_ids) != 20:
        problems.append(f"teams: expected 20, got {len(team_ids)}")

    for p in players:
        if type(p["now_cost_tenths"]) is not int:
            problems.append(f"players: non-int price for element {p['element_id']}")
        if p["team_fpl_id"] not in team_ids:
            problems.append(f"players: element {p['element_id']} has unknown team")
    element_ids = {p["element_id"] for p in players}

    sides = {f["fixture_id"]: f for f in fixtures}
    for f in fixtures:
        if f["team_h_fpl_id"] not in team_ids or f["team_a_fpl_id"] not in team_ids:
            problems.append(f"fixtures: {f['fixture_id']} references unknown team")

    keys = Counter((s["element_id"], s["gw"], s["fixture_id"]) for s in stats)
    dupes = sum(1 for n in keys.values() if n > 1)
    if dupes:
        problems.append(f"player_gw_stats: {dupes} duplicate primary keys")

    for s in stats:
        where = f"element {s['element_id']} gw {s['gw']} fixture {s['fixture_id']}"
        if s["element_id"] not in element_ids:
            problems.append(f"player_gw_stats: {where}: element not in players")
            continue
        if type(s["value_tenths"]) is not int:
            problems.append(f"player_gw_stats: {where}: non-int value_tenths")
        if s["source"] != "history":
            problems.append(f"player_gw_stats: {where}: source {s['source']!r}")
        fixture = sides.get(s["fixture_id"])
        if fixture is None:
            problems.append(f"player_gw_stats: {where}: fixture not in fixtures")
            continue
        if fixture["gw"] != s["gw"]:
            problems.append(
                f"player_gw_stats: {where}: fixture is in gw {fixture['gw']}"
            )
        if s["was_home"] is None:
            problems.append(f"player_gw_stats: {where}: was_home missing")
            continue
        # The player's own club must be derivable, and consistent with the
        # opponent the source recorded.
        expected_opponent = (
            fixture["team_a_fpl_id"] if s["was_home"] else fixture["team_h_fpl_id"]
        )
        if s["opponent_team_fpl_id"] != expected_opponent:
            problems.append(
                f"player_gw_stats: {where}: opponent {s['opponent_team_fpl_id']} "
                f"does not match fixture side {expected_opponent}"
            )

    # Cap the report; a systematic mapping error would otherwise print 30k lines.
    if len(problems) > 50:
        problems = problems[:50] + [f"... and {len(problems) - 50} more"]
    return problems


# --- Orchestration -----------------------------------------------------------


@dataclass
class SeasonRows:
    season: str
    teams: list[dict[str, Any]]
    players: list[dict[str, Any]]
    fixtures: list[dict[str, Any]]
    stats: list[dict[str, Any]]
    notes: dict[str, Any] = field(default_factory=dict)


def build_season(files: SeasonFiles) -> SeasonRows:
    """All four tables' rows for one season, validated. Pure."""
    season = files.season
    guard_season(season)

    gws, dropped = dedupe_gw_rows(files.gws)
    excluded = excluded_elements(files.players)
    missing = missing_xp_gameweeks(gws)

    teams = build_team_rows(season, files.teams)
    players = build_player_rows(season, files.players, first_values(gws))
    fixtures = build_fixture_rows(season, files.fixtures)
    stats = build_stat_rows(season, gws, missing_xp=missing, excluded=excluded)

    problems = validate(season, teams, players, fixtures, stats)
    if problems:
        raise ValueError(f"{season}: validation failed:\n  " + "\n  ".join(problems))

    doubles = Counter((s["element_id"], s["gw"]) for s in stats)
    notes = {
        "source_rows": len(files.gws),
        "duplicate_rows_dropped": dropped,
        "non_player_elements_skipped": len(excluded),
        "non_player_rows_skipped": sum(
            1 for r in gws if int(r["element"]) in excluded
        ),
        "gameweeks_missing_xp": sorted(missing),
        "double_gameweek_player_rows": sum(n for n in doubles.values() if n > 1),
        "fpl_xp_null": sum(1 for s in stats if s["fpl_xp"] is None),
    }
    return SeasonRows(season, teams, players, fixtures, stats, notes)


def _write(conn, table: str, rows: list[dict[str, Any]], keys: tuple[str, ...]) -> int:
    import db

    written = 0
    for start in range(0, len(rows), BATCH_SIZE):
        written += db.upsert(conn, table, rows[start : start + BATCH_SIZE], keys)
        conn.commit()
    return written


def write_season(conn, rows: SeasonRows) -> dict[str, int]:
    """Upsert one season in foreign-key order."""
    guard_season(rows.season)
    counts = {
        "teams": _write(conn, "teams", rows.teams, ("season", "fpl_id")),
        "players": _write(conn, "players", rows.players, ("season", "element_id")),
        "fixtures": _write(conn, "fixtures", rows.fixtures, ("season", "fixture_id")),
        "player_gw_stats": _write(
            conn, "player_gw_stats", rows.stats, STAT_CONFLICT_KEYS
        ),
    }
    return counts


def run(seasons: list[str], cache_dir: Path, dry_run: bool) -> dict[str, dict]:
    for season in seasons:
        guard_season(season)

    built = []
    for season in seasons:
        rows = build_season(fetch(season, cache_dir))
        log.info(
            "%s: teams=%d players=%d fixtures=%d player_gw_stats=%d %s",
            season,
            len(rows.teams),
            len(rows.players),
            len(rows.fixtures),
            len(rows.stats),
            rows.notes,
        )
        built.append(rows)

    summary: dict[str, dict] = {}
    if dry_run:
        log.info("dry run: validated %d seasons, wrote nothing", len(built))
        for rows in built:
            summary[rows.season] = {
                "teams": len(rows.teams),
                "players": len(rows.players),
                "fixtures": len(rows.fixtures),
                "player_gw_stats": len(rows.stats),
            }
        return summary

    import db

    with db.connect() as conn:
        for rows in built:
            with db.run_logged(conn, JOB, rows.season) as result:
                counts = write_season(conn, rows)
                result["rows"] = sum(counts.values())
                log.info("%s: wrote %s", rows.season, counts)
                summary[rows.season] = counts

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import completed seasons from vaastav/Fantasy-Premier-League."
    )
    parser.add_argument(
        "--seasons", nargs="+", default=list(HISTORY_SEASONS), choices=HISTORY_SEASONS
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and validate, write nothing"
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="where downloaded CSVs are kept (default: git-ignored data/raw/history)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    run(args.seasons, args.cache_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
