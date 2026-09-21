"""Publish the xP matrix as one immutable JSON artifact for the optimizer.

The optimizer has no database credentials (CLAUDE.md invariant 5), so the xP it
plans with has to be handed to it. This job is the hand-off: it reads the latest
`xpoints` computation plus today's prices and fixtures from Postgres (it is an
ingest job, so it may) and writes everything the solver needs into a single
file. `optimizer/pool.py`'s `ArtifactXPProvider` loads that file from a path or
URL and never sees a database.

What goes in, per player: `element_id`, `web_name`, `team_fpl_id`,
`element_type`, `now_cost_tenths`, `selected_by_percent`, `status`, xP per target
gameweek, expected minutes per target gameweek, and fixture count per target
gameweek (0 is a blank, 2 a double). Top level: `season`, `model_version`,
`generated_at`, `as_of_gw`, `target_gws`, and the `ingest_runs` id of the xP
computation the numbers came from.

**It refuses to publish nonsense.** A model run on one gameweek of data once
projected every player at near-identical expected minutes and Haaland at 1.24
points; that matrix is well-formed and a solver will happily optimise against
it. So before writing, the matrix is checked for exactly that shape of failure --
see `validate_xp` -- and the job exits non-zero rather than ship it.

This job does not upload anywhere. Where production reads the artifact from is
the owner's decision; this writes a file.

    python ingest/publish_xp.py                             # latest model_version
    python ingest/publish_xp.py --model-version baseline-0.1 --out /tmp/xp.json

All money is integer tenths.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LIVE_MODEL_VERSION, REPO_ROOT, SEASON, SQUAD_COMPOSITION

log = logging.getLogger(__name__)

XP_JOB = "xpoints"  # compute_xp.JOB -- the ingest_runs row the numbers came from.

# Matches optimizer/pool.py's DEFAULT_ARTIFACT_PATH and ARTIFACT_FORMAT. Kept as
# literals rather than imported: ingest does not depend on the optimizer package.
DEFAULT_OUT = REPO_ROOT / "data" / "xp_artifact.local.json"
ARTIFACT_FORMAT = 1

# --- Degeneracy thresholds --------------------------------------------------
#
# Deliberately loose. These are not quality bars -- a weak model passes them --
# they catch a model that has collapsed to a constant. For scale, baseline-0.1 on
# GW1-4 of 2026-27 has a top xP around 5.2-5.5, a spread among scoring players of
# ~1.3 and ~100 distinct expected-minutes values per gameweek.

# The best player in a gameweek with a fixture projects at least this. A season's
# top scorer projected at 1.24 is the failure this exists for.
MIN_TOP_XP = 3.0
# Spread of xP across players with a positive projection. Near-identical xP
# means the model is not distinguishing anyone, and every plan is noise.
MIN_XP_STDDEV = 0.5
# Distinct expected-minutes values among players expected to play at all. One
# value for everyone is the "every player has identical minutes" failure.
MIN_DISTINCT_XMINS = 5
# Distinct xP values across the whole gameweek.
MIN_DISTINCT_XP = 20
# Share of players with a fixture that project anything at all.
MIN_POSITIVE_SHARE = 0.25
# Share of the season's players the artifact has a complete xP row for. Anything
# less is a half-finished computation.
MIN_COVERAGE = 0.95
# A blank gameweek projects zero. Anything above this is a model bug.
BLANK_XP_TOLERANCE = 0.01
# Per position, how many times the squad quota must have a positive projection
# so a solver has real choices.
POSITION_DEPTH = 2


class PublishError(RuntimeError):
    """The artifact would be wrong. Carries every reason, not just the first."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("refusing to publish xP artifact:\n  - " + "\n  - ".join(self.problems))


# --- Pure transforms ----------------------------------------------------------


def latest_computation(xp_rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Only the rows written by the most recent run of this model version.

    `xpoints` keeps every gameweek a version ever projected. After a second
    nightly run, GW5's rows from the first run are still there beside GW6-10 from
    the second -- mixing them would publish a projection made with less data than
    its neighbours. compute_xp stamps every row of one run with the same
    `computed_at`, so the latest stamp is the latest run.
    """
    if not xp_rows:
        return []
    latest = max(r["computed_at"] for r in xp_rows)
    return [r for r in xp_rows if r["computed_at"] == latest]


def fixture_counts(
    fixture_rows: Sequence[Mapping[str, Any]], target_gws: Sequence[int]
) -> dict[tuple[int, int], int]:
    """(team, gw) -> fixtures that gameweek. One-to-many: absent means a blank."""
    targets = set(target_gws)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for row in fixture_rows:
        gw = row.get("gw")
        if gw is None or int(gw) not in targets:
            continue
        counts[(int(row["team_h_fpl_id"]), int(gw))] += 1
        counts[(int(row["team_a_fpl_id"]), int(gw))] += 1
    return dict(counts)


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def build_artifact(
    *,
    season: str,
    model_version: str,
    xp_rows: Sequence[Mapping[str, Any]],
    player_rows: Sequence[Mapping[str, Any]],
    fixture_rows: Sequence[Mapping[str, Any]],
    ingest_run_id: int | None,
    generated_at: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Assemble the artifact from already-fetched rows. Returns (doc, problems).

    Pure: no database, no clock, no file. `xp_rows` must already be one
    computation (see `latest_computation`). Problems found while assembling --
    inconsistent as_of_gw, fixture counts that disagree with the model's, players
    with gaps -- are returned rather than raised so `validate_xp` can add its own
    and the operator sees the whole list at once.
    """
    problems: list[str] = []
    if not xp_rows:
        return {}, [f"no xpoints rows for {season} / {model_version}"]

    target_gws = sorted({int(r["gw"]) for r in xp_rows})
    as_of = {r.get("as_of_gw") for r in xp_rows}
    if len(as_of) != 1 or None in as_of:
        problems.append(f"xpoints rows disagree on as_of_gw: {sorted(map(str, as_of))}")
    as_of_gw = next((int(a) for a in as_of if a is not None), None)

    counts = fixture_counts(fixture_rows, target_gws)

    xp_by_player: dict[int, dict[int, Mapping[str, Any]]] = defaultdict(dict)
    for r in xp_rows:
        xp_by_player[int(r["element_id"])][int(r["gw"])] = r

    players_by_id = {int(p["element_id"]): p for p in player_rows}
    unknown = sorted(set(xp_by_player) - set(players_by_id))
    if unknown:
        problems.append(f"xpoints has players not in players: {unknown[:10]}")

    out_players: list[dict[str, Any]] = []
    incomplete: list[int] = []
    fixture_mismatch: list[str] = []
    for eid in sorted(players_by_id):
        rows = xp_by_player.get(eid)
        if not rows:
            continue  # not projected; the coverage check decides if that matters
        if set(rows) != set(target_gws):
            incomplete.append(eid)
            continue
        p = players_by_id[eid]
        team = int(p["team_fpl_id"])

        n_fixtures: dict[str, int] = {}
        for gw in target_gws:
            n = counts.get((team, gw), 0)
            model_n = rows[gw].get("fixture_count")
            if model_n is not None and int(model_n) != n:
                # The fixture list moved after the model ran: a postponement
                # turned a single into a blank, say. The xP no longer describes
                # the gameweek it is labelled with.
                fixture_mismatch.append(f"{eid}@GW{gw}: model {model_n}, fixtures {n}")
            n_fixtures[str(gw)] = n

        sel = p.get("selected_by_percent")
        out_players.append(
            {
                "element_id": eid,
                "web_name": p["web_name"],
                "team_fpl_id": team,
                "element_type": int(p["element_type"]),
                # int(), not float(): money is integer tenths (CLAUDE.md invariant 1).
                "now_cost_tenths": int(p["now_cost_tenths"]),
                "selected_by_percent": float(sel) if sel is not None else 0.0,
                "status": p.get("status") or "a",
                "xp": {str(gw): round(float(rows[gw]["xp"]), 3) for gw in target_gws},
                "xmins": {str(gw): _num(rows[gw].get("xmins")) for gw in target_gws},
                "n_fixtures": n_fixtures,
            }
        )

    if incomplete:
        problems.append(
            f"{len(incomplete)} players have xP for only some of GW{target_gws}: {incomplete[:10]}"
        )
    if fixture_mismatch:
        problems.append(
            f"{len(fixture_mismatch)} fixture counts changed since xP was computed "
            f"(re-run compute_xp): {fixture_mismatch[:5]}"
        )

    covered = len(out_players) / len(players_by_id) if players_by_id else 0.0
    if covered < MIN_COVERAGE:
        problems.append(
            f"xP covers {len(out_players)}/{len(players_by_id)} players ({covered:.0%}), "
            f"need {MIN_COVERAGE:.0%}"
        )

    doc = {
        "format": ARTIFACT_FORMAT,
        "season": season,
        "model_version": model_version,
        "generated_at": generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_gw": as_of_gw,
        "target_gws": target_gws,
        "ingest_run_id": ingest_run_id,
        "xp_computed_at": _iso(max(r["computed_at"] for r in xp_rows)),
        "players": out_players,
    }
    return doc, problems


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def validate_xp(doc: Mapping[str, Any]) -> list[str]:
    """Every reason this matrix is too degenerate to plan with. Empty means ship it.

    The checks target a collapsed model, not a weak one: nobody projects above a
    few points, everybody projects the same, everybody has the same minutes, a
    blank projects points. Each is computed per target gameweek, over players
    who actually have a fixture that week.
    """
    problems: list[str] = []
    players = doc.get("players") or []
    if not players:
        return ["artifact has no players"]

    for p in players:
        price = p.get("now_cost_tenths")
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            problems.append(f"player {p.get('element_id')}: now_cost_tenths {price!r} is not positive integer tenths")
            break

    for gw in doc.get("target_gws", []):
        key = str(gw)
        playing = [p for p in players if p["n_fixtures"].get(key, 0) > 0]
        if not playing:
            problems.append(f"GW{gw}: no player has a fixture")
            continue

        values = [p["xp"][key] for p in playing]
        bad = [v for v in values if not math.isfinite(v) or v < -1.0]
        if bad:
            problems.append(f"GW{gw}: {len(bad)} non-finite or implausibly negative xP values")
            continue

        top = max(values)
        if top < MIN_TOP_XP:
            best = max(playing, key=lambda p: p["xp"][key])
            problems.append(
                f"GW{gw}: top projection is {best['web_name']} at {top:.2f} "
                f"(need >= {MIN_TOP_XP}) -- the model is not separating anyone"
            )

        positive = [v for v in values if v > 0]
        if len(positive) / len(playing) < MIN_POSITIVE_SHARE:
            problems.append(
                f"GW{gw}: only {len(positive)}/{len(playing)} players with a fixture project any points"
            )
        if len(positive) >= 2 and statistics.pstdev(positive) < MIN_XP_STDDEV:
            problems.append(
                f"GW{gw}: xP spread is {statistics.pstdev(positive):.3f} "
                f"(need >= {MIN_XP_STDDEV}) -- near-identical projections"
            )
        if len({round(v, 3) for v in values}) < MIN_DISTINCT_XP:
            problems.append(f"GW{gw}: only {len({round(v, 3) for v in values})} distinct xP values")

        xmins = [
            p["xmins"][key]
            for p in playing
            if p.get("xmins") and p["xmins"].get(key) is not None and p["xmins"][key] > 0
        ]
        if xmins and len(set(xmins)) < MIN_DISTINCT_XMINS:
            common = Counter(xmins).most_common(1)[0]
            problems.append(
                f"GW{gw}: only {len(set(xmins))} distinct expected-minutes values "
                f"({common[1]} players at {common[0]}) -- identical minutes for everyone"
            )

        blanks = [
            p for p in players
            if p["n_fixtures"].get(key, 0) == 0 and p["xp"][key] > BLANK_XP_TOLERANCE
        ]
        if blanks:
            names = ", ".join(f"{p['web_name']} {p['xp'][key]:.2f}" for p in blanks[:5])
            problems.append(f"GW{gw}: {len(blanks)} players with a blank project points: {names}")

    gws = [str(g) for g in doc.get("target_gws", [])]
    for etype, quota in SQUAD_COMPOSITION.items():
        depth = sum(
            1 for p in players
            if p["element_type"] == etype and sum(p["xp"][g] for g in gws) > 0
        )
        if depth < quota * POSITION_DEPTH:
            problems.append(
                f"only {depth} players at element_type {etype} project any points; "
                f"need {quota * POSITION_DEPTH}"
            )

    return problems


def write_atomic(doc: Mapping[str, Any], out: Path) -> None:
    """Write via a temp file and rename, so a reader never sees half an artifact."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        # mkstemp creates 0600; the artifact is meant to be read by other processes.
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- Database reads -------------------------------------------------------------


def latest_model_version(conn, season: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "select model_version from xpoints where season = %s "
            "group by model_version order by max(computed_at) desc limit 1",
            (season,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def load_xp_rows(conn, season: str, model_version: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select element_id, gw, xp, xmins, as_of_gw, fixture_count, computed_at "
            "from xpoints where season = %s and model_version = %s",
            (season, model_version),
        )
        return cur.fetchall()


def load_players(conn, season: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select element_id, web_name, team_fpl_id, element_type, now_cost_tenths, "
            "selected_by_percent, status from players where season = %s",
            (season,),
        )
        return cur.fetchall()


def load_fixtures(conn, season: str, gws: Sequence[int]) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select fixture_id, gw, team_h_fpl_id, team_a_fpl_id "
            "from fixtures where season = %s and gw = any(%s)",
            (season, list(gws)),
        )
        return cur.fetchall()


def find_xp_run(conn, season: str, computed_at: datetime) -> int | None:
    """The `ingest_runs` id of the compute_xp run that wrote these rows.

    `xpoints` carries no run id, but compute_xp stamps `computed_at` inside its
    logged run, so the run whose window contains that stamp is the one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select id from ingest_runs where job = %s and season = %s and ok "
            "and started_at <= %s and coalesce(finished_at, now()) >= %s "
            "order by id desc limit 1",
            (XP_JOB, season, computed_at, computed_at),
        )
        row = cur.fetchone()
    return row[0] if row else None


# --- Entry point ----------------------------------------------------------------


def publish(
    *, season: str, model_version: str | None, out: Path, now: datetime | None = None
) -> dict[str, Any]:
    import db

    with db.connect() as conn:
        # The live model by name, never "whichever ran last": the nightly job
        # computes several models side by side, and their finish order is not
        # a decision about which one the site should use.
        version = model_version or LIVE_MODEL_VERSION
        if version is None:
            raise PublishError([f"no xpoints rows at all for season {season}"])

        xp_rows = latest_computation(load_xp_rows(conn, season, version))
        if not xp_rows:
            raise PublishError([f"no xpoints rows for {season} / {version}"])
        target_gws = sorted({int(r["gw"]) for r in xp_rows})
        computed_at = xp_rows[0]["computed_at"]

        run_id = find_xp_run(conn, season, computed_at)
        if run_id is None:
            log.warning(
                "no successful %r ingest run brackets computed_at %s; "
                "publishing with ingest_run_id null",
                XP_JOB,
                computed_at,
            )

        doc, problems = build_artifact(
            season=season,
            model_version=version,
            xp_rows=xp_rows,
            player_rows=load_players(conn, season),
            fixture_rows=load_fixtures(conn, season, target_gws),
            ingest_run_id=run_id,
            generated_at=now or datetime.now(UTC),
        )

    problems += validate_xp(doc)
    if problems:
        raise PublishError(problems)

    write_atomic(doc, out)
    return doc


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", default=SEASON)
    parser.add_argument(
        "--model-version",
        default=None,
        help=f"default: config.LIVE_MODEL_VERSION ({LIVE_MODEL_VERSION})",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    try:
        doc = publish(season=args.season, model_version=args.model_version, out=args.out)
    except PublishError as exc:
        log.error("%s", exc)
        return 1

    log.info(
        "published %s: %s / %s, as of GW%s, GW%s, %d players, run %s, generated %s",
        args.out,
        doc["season"],
        doc["model_version"],
        doc["as_of_gw"],
        doc["target_gws"],
        len(doc["players"]),
        doc["ingest_run_id"],
        doc["generated_at"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
