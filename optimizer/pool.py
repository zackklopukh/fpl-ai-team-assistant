"""Where the expected-points matrix comes from, and how it gets shrunk.

The optimizer has no database credentials and never will (CLAUDE.md, invariant 5).
So the xP matrix has to arrive from outside. This module defines the seam:
everything downstream depends on the `XPProvider` protocol, not on a file, a URL
or a table.

Two providers:

* `ArtifactXPProvider` -- the real one. Loads the immutable JSON artifact that
  `ingest/publish_xp.py` writes from Postgres, from a local path or an http(s)
  URL, validates its shape and season, and holds it in memory.
* `FixtureXPProvider` -- the synthetic seed in `tests/seed_xp.json`. Invented
  players with invented ids. It is the tests' fixture and nothing else; a service
  running on it is flagged as SYNTHETIC everywhere a human might look.

`provider_from_env` picks between them; see its docstring for the order.

--------------------------------------------------------------------------------
PRODUCTION SEAM

The nightly xP recompute (ingest/, 05:00 UTC) writes the `xpoints` table. The
optimizer must not read that table. `ingest/publish_xp.py` reads it (it is an
ingest job, so it may) and writes one immutable JSON artifact per publish:
season, model_version, generated_at, as_of_gw, target gameweeks, the ingest run
it came from, and per player the price, position, club, xP and fixture count per
target gameweek. This service loads that artifact once per container start and
holds it for the container's life.

How the artifact physically reaches production (object storage, a release asset,
baked into the image) is deliberately not decided here: set OPTIMIZER_XP_URL or
OPTIMIZER_XP_PATH and nothing else moves. It must NOT mean:
  * giving this service a Postgres URL, or
  * having the web app POST a 500KB xP matrix on every request.

`generated_at` on the snapshot is load-bearing: it is the `data_as_of` stamped on
every recommendation. A recommendation computed at 22:00 can be invalid at 02:00
once prices change, so the answer has to carry the as-of time of the data it used,
never `now()`.
--------------------------------------------------------------------------------

All money is integer tenths.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

log = logging.getLogger(__name__)

# 1 GKP, 2 DEF, 3 MID, 4 FWD -- FPL's element_type, matching db/schema.sql.
GKP, DEF, MID, FWD = 1, 2, 3, 4
POSITION_NAMES = {GKP: "goalkeeper", DEF: "defender", MID: "midfielder", FWD: "forward"}

# ARCHITECTURE.md: "Pre-filter to the top ~150 players by xP per position, plus
# every player currently in the squad. That is the single biggest performance
# lever and it costs almost nothing in solution quality, because the optimal
# transfer target is never the 300th-best midfielder."
DEFAULT_POOL_PER_POSITION = 150

REPO_ROOT = Path(__file__).resolve().parent.parent

# The synthetic seed. Invented players, invented ids -- the solver tests' fixture.
SEED_XP_PATH = Path(__file__).parent / "tests" / "seed_xp.json"

# Where `ingest/publish_xp.py` writes by default. `*.local.json` is git-ignored,
# so a published artifact never lands in a commit by accident.
DEFAULT_ARTIFACT_PATH = REPO_ROOT / "data" / "xp_artifact.local.json"

SYNTHETIC_SOURCE = "SYNTHETIC SEED"

# The artifact format `publish_xp.py` writes. Bumped only on a breaking change.
ARTIFACT_FORMAT = 1

URL_TIMEOUT_S = 30.0


class ArtifactError(ValueError):
    """An xP artifact that cannot be trusted: wrong shape, wrong season, unreadable."""


class SeasonMismatchError(ArtifactError):
    """The request asked about a season the loaded xP is not for."""


@dataclass(frozen=True)
class PlayerXP:
    """One player's row of the xP matrix, plus the few attributes a decision needs.

    Prices here are *buying* prices (today's `now_cost_tenths`). Selling prices
    are different, depend on when the holder bought, and arrive on the request.
    """

    element_id: int
    web_name: str
    team_fpl_id: int
    element_type: int
    price: int  # integer tenths
    ownership: float  # selected_by_percent
    xp_by_gw: dict[int, float]
    fixtures_by_gw: dict[int, int]  # 0 is a blank, 2 a double
    status: str = "a"

    def xp(self, gw: int) -> float:
        """xP for one gameweek. A gameweek with no row is worth nothing, not an error:
        a player outside the model's horizon simply contributes no points."""
        return self.xp_by_gw.get(gw, 0.0)

    def horizon_xp(self, gws: Iterable[int]) -> float:
        return sum(self.xp(gw) for gw in gws)

    def fixtures(self, gw: int) -> int:
        return self.fixtures_by_gw.get(gw, 0)


@dataclass(frozen=True)
class XPSnapshot:
    """An immutable xP matrix as of one point in time."""

    season: str
    model_version: str
    generated_at: str  # ISO 8601, stamped onto every response as data_as_of
    players: dict[int, PlayerXP]
    # Provenance. Optional because the synthetic seed predates the artifact
    # format and carries none of it.
    as_of_gw: int | None = None
    target_gws: tuple[int, ...] = ()
    ingest_run_id: int | None = None
    source: str = ""
    synthetic: bool = False

    @classmethod
    def from_dict(
        cls, doc: dict, *, source: str = "", synthetic: bool = False
    ) -> "XPSnapshot":
        players: dict[int, PlayerXP] = {}
        for row in doc["players"]:
            eid = int(row["element_id"])
            players[eid] = PlayerXP(
                element_id=eid,
                web_name=row["web_name"],
                team_fpl_id=int(row["team_fpl_id"]),
                element_type=int(row["element_type"]),
                price=int(row["now_cost_tenths"]),
                ownership=float(row.get("selected_by_percent") or 0.0),
                # JSON object keys are strings; gameweeks are ints everywhere else.
                xp_by_gw={int(k): float(v) for k, v in row.get("xp", {}).items()},
                fixtures_by_gw={int(k): int(v) for k, v in row.get("n_fixtures", {}).items()},
                status=str(row.get("status") or "a"),
            )
        as_of = doc.get("as_of_gw")
        run_id = doc.get("ingest_run_id")
        return cls(
            season=doc["season"],
            model_version=doc["model_version"],
            generated_at=doc["generated_at"],
            players=players,
            as_of_gw=int(as_of) if as_of is not None else None,
            target_gws=tuple(int(g) for g in doc.get("target_gws", ())),
            ingest_run_id=int(run_id) if run_id is not None else None,
            source=source,
            synthetic=synthetic,
        )

    def get(self, element_id: int) -> PlayerXP | None:
        return self.players.get(element_id)

    def missing(self, element_ids: Iterable[int]) -> list[int]:
        return [e for e in element_ids if e not in self.players]

    def fixtures_matrix(self, element_ids: Iterable[int] | None = None) -> dict[int, dict[int, int]]:
        """element_id -> gw -> fixture count, for the solver's `fixtures=` argument.

        Only players whose counts the snapshot actually carries appear. The solver
        refuses to invent a default of one fixture, and so does this.
        """
        ids = self.players.keys() if element_ids is None else element_ids
        out: dict[int, dict[int, int]] = {}
        for eid in ids:
            p = self.players.get(eid)
            if p is not None and p.fixtures_by_gw:
                out[eid] = dict(p.fixtures_by_gw)
        return out


class XPProvider(Protocol):
    """The seam. Swap the implementation, not the caller."""

    source: str
    synthetic: bool

    def snapshot(self, season: str) -> XPSnapshot: ...


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_PLAYER_INT_FIELDS = ("element_id", "team_fpl_id", "element_type", "now_cost_tenths")


def _is_int(value: Any) -> bool:
    # bool is an int subclass in Python, and True is not a price.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_artifact(doc: Any, *, expected_season: str | None = None) -> list[str]:
    """Every reason `doc` is not a usable xP artifact. Empty means it is.

    Shape only, plus the season. Whether the numbers are *sensible* is the
    publisher's job (publish_xp.py refuses degenerate xP before it writes);
    this side checks that what arrived is the thing that was published, and
    that it is for the season being asked about. Element ids are reassigned
    between seasons, so a wrong-season artifact silently answers about the
    wrong players -- the failure CLAUDE.md invariant 2 exists to prevent.
    """
    problems: list[str] = []
    if not isinstance(doc, Mapping):
        return ["artifact is not a JSON object"]

    for key in ("season", "model_version", "generated_at", "players"):
        if key not in doc:
            problems.append(f"missing top-level field {key!r}")
    if problems:
        return problems

    if expected_season is not None and doc["season"] != expected_season:
        problems.append(
            f"artifact is for season {doc['season']!r}, this service expects "
            f"{expected_season!r}. Element ids are reassigned between seasons."
        )

    fmt = doc.get("format")
    if fmt is not None and fmt != ARTIFACT_FORMAT:
        problems.append(f"artifact format {fmt!r}, this service reads {ARTIFACT_FORMAT}")

    target = doc.get("target_gws")
    if target is not None:
        if not isinstance(target, list) or not target or not all(_is_int(g) for g in target):
            problems.append("target_gws must be a non-empty list of integers")
            target = None

    players = doc["players"]
    if not isinstance(players, list) or not players:
        problems.append("players must be a non-empty list")
        return problems

    seen: set[int] = set()
    for i, row in enumerate(players):
        if len(problems) > 20:
            problems.append("... (further problems suppressed)")
            break
        where = f"players[{i}]"
        if not isinstance(row, Mapping):
            problems.append(f"{where} is not an object")
            continue
        for key in _PLAYER_INT_FIELDS:
            if not _is_int(row.get(key)):
                # A float price is a CLAUDE.md invariant 1 violation, not a rounding
                # detail: 5.5 where 55 was meant is off by a factor of ten.
                problems.append(f"{where}.{key} must be an integer, got {row.get(key)!r}")
        if not isinstance(row.get("web_name"), str) or not row.get("web_name"):
            problems.append(f"{where}.web_name missing")
        if row.get("element_type") not in POSITION_NAMES:
            problems.append(f"{where}.element_type {row.get('element_type')!r} is not 1-4")
        eid = row.get("element_id")
        if eid in seen:
            problems.append(f"{where}: duplicate element_id {eid}")
        seen.add(eid)

        xp = row.get("xp")
        if not isinstance(xp, Mapping):
            problems.append(f"{where}.xp must be an object of gameweek -> points")
            continue
        for gw, value in xp.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                problems.append(f"{where}.xp[{gw}] is not a finite number: {value!r}")
        nfx = row.get("n_fixtures", {})
        if not isinstance(nfx, Mapping) or not all(_is_int(v) and v >= 0 for v in nfx.values()):
            problems.append(f"{where}.n_fixtures must map gameweek -> non-negative int")
        if target is not None:
            want = {str(g) for g in target}
            if set(map(str, xp.keys())) != want:
                problems.append(f"{where}.xp does not cover exactly target_gws {target}")
            if isinstance(nfx, Mapping) and set(map(str, nfx.keys())) != want:
                problems.append(f"{where}.n_fixtures does not cover exactly target_gws {target}")

    return problems


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _read_source(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT_S) as resp:  # noqa: S310 -- scheme checked above
            return resp.read()
    return Path(source).read_bytes()


class ArtifactXPProvider:
    """The real provider: the artifact `ingest/publish_xp.py` wrote.

    `source` is a local path or an http(s) URL. The artifact is fetched once,
    validated, and held for the life of the process -- it is immutable, so there
    is nothing to refresh; a new artifact means a new container (or a restart).
    A failed load is not cached, so a transient network error on the first
    request does not poison the process.
    """

    synthetic = False

    def __init__(self, source: str | Path, *, expected_season: str | None = None):
        self.source = str(source)
        self.expected_season = expected_season
        self._cache: XPSnapshot | None = None
        self._lock = threading.Lock()

    def load(self) -> XPSnapshot:
        with self._lock:
            if self._cache is not None:
                return self._cache
            try:
                raw = _read_source(self.source)
            except Exception as exc:  # network, missing file, permissions
                raise ArtifactError(f"cannot read xP artifact {self.source}: {exc}") from exc
            try:
                doc = json.loads(raw)
            except ValueError as exc:
                raise ArtifactError(f"xP artifact {self.source} is not valid JSON: {exc}") from exc

            problems = validate_artifact(doc, expected_season=self.expected_season)
            if problems:
                raise ArtifactError(
                    f"refusing xP artifact {self.source}: " + "; ".join(problems)
                )

            snap = XPSnapshot.from_dict(doc, source=self.source, synthetic=False)
            log.info(
                "loaded xP artifact %s: season %s, model %s, generated %s, as of GW%s, "
                "targets %s, %d players",
                self.source,
                snap.season,
                snap.model_version,
                snap.generated_at,
                snap.as_of_gw,
                list(snap.target_gws),
                len(snap.players),
            )
            self._cache = snap
            return snap

    def snapshot(self, season: str) -> XPSnapshot:
        snap = self.load()
        if season and season != snap.season:
            raise SeasonMismatchError(
                f"request is for season {season!r} but the loaded xP artifact is for "
                f"{snap.season!r}. Element ids are reassigned between seasons."
            )
        return snap


class FixtureXPProvider:
    """Loads the synthetic seed snapshot from a JSON file on local disk.

    Named for the test fixture, not for football fixtures. This is the test
    provider: it makes the service runnable with no database, no network and no
    credentials. The players in it are invented and their ids match nothing in
    FPL, which is why it is labelled SYNTHETIC wherever it surfaces.
    """

    synthetic = True

    def __init__(self, path: str | Path = SEED_XP_PATH):
        self.path = Path(path)
        self.source = f"{SYNTHETIC_SOURCE} ({self.path})"
        self._cache: XPSnapshot | None = None

    def snapshot(self, season: str) -> XPSnapshot:
        # The file is immutable in practice, so one read per process is enough.
        if self._cache is None:
            self._cache = XPSnapshot.from_dict(
                json.loads(self.path.read_text()), source=self.source, synthetic=True
            )
        return self._cache


def provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    default_artifact: Path = DEFAULT_ARTIFACT_PATH,
    expected_season: str | None = None,
) -> XPProvider:
    """Choose the xP source, in this order:

    1. OPTIMIZER_XP_URL  -- an http(s) URL to a published artifact.
    2. OPTIMIZER_XP_PATH -- a local artifact path. Pointing it at the seed file
       works and is treated as the seed (synthetic), not as an artifact.
    3. The default publish location, if `publish_xp.py` has written one there.
    4. The synthetic seed -- with a loud warning, because a developer seeing
       invented names in the UI is otherwise left to guess why.

    OPTIMIZER_SEASON sets the season an artifact must be for.
    """
    env = os.environ if environ is None else environ
    season = expected_season or env.get("OPTIMIZER_SEASON") or None

    url = (env.get("OPTIMIZER_XP_URL") or "").strip()
    if url:
        if not url.startswith(("http://", "https://")):
            raise ArtifactError(f"OPTIMIZER_XP_URL must be http(s), got {url!r}")
        return ArtifactXPProvider(url, expected_season=season)

    path = (env.get("OPTIMIZER_XP_PATH") or "").strip()
    if path:
        if Path(path).resolve() == SEED_XP_PATH.resolve():
            _warn_synthetic("OPTIMIZER_XP_PATH points at the seed file")
            return FixtureXPProvider(path)
        return ArtifactXPProvider(path, expected_season=season)

    if default_artifact.exists():
        return ArtifactXPProvider(default_artifact, expected_season=season)

    _warn_synthetic(
        f"no OPTIMIZER_XP_URL / OPTIMIZER_XP_PATH and no artifact at {default_artifact}"
    )
    return FixtureXPProvider(SEED_XP_PATH)


def _warn_synthetic(why: str) -> None:
    log.warning(
        "*** SYNTHETIC SEED xP IN USE (%s). Player names and ids are INVENTED and do "
        "not match FPL. Run `python ingest/publish_xp.py` or set OPTIMIZER_XP_PATH / "
        "OPTIMIZER_XP_URL to serve real data. ***",
        why,
    )


# ---------------------------------------------------------------------------
# Pool shrinking
# ---------------------------------------------------------------------------


def candidate_pool(
    snapshot: XPSnapshot,
    squad_ids: Iterable[int],
    gws: Iterable[int],
    *,
    max_ownership: float | None = None,
    per_position: int = DEFAULT_POOL_PER_POSITION,
) -> list[PlayerXP]:
    """The players a solver is allowed to consider.

    Top `per_position` by horizon xP within each position, plus everyone already
    in the squad. The held players are never filtered out -- not by the cutoff and
    not by `max_ownership`. You cannot reason about a squad while pretending some
    of its members do not exist, and differential mode is a request for who to
    *buy*, not an instruction to disown a player you already hold.
    """
    gws = list(gws)
    held = set(squad_ids)

    eligible: dict[int, list[PlayerXP]] = {}
    for player in snapshot.players.values():
        if player.element_id in held:
            continue
        if max_ownership is not None and player.ownership > max_ownership:
            continue
        eligible.setdefault(player.element_type, []).append(player)

    pool: list[PlayerXP] = []
    for players in eligible.values():
        players.sort(key=lambda p: (-p.horizon_xp(gws), p.element_id))
        pool.extend(players[:per_position])

    for eid in held:
        player = snapshot.get(eid)
        if player is not None:
            pool.append(player)

    return pool
