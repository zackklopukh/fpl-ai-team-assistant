"""Where the expected-points matrix comes from, and how it gets shrunk.

The optimizer has no database credentials and never will (CLAUDE.md, invariant 5).
So the xP matrix has to arrive from outside. This module defines the seam:
everything downstream depends on the `XPProvider` protocol, not on a file, a URL
or a table.

The only provider implemented today is `FixtureXPProvider`, which reads a JSON
snapshot from local disk. That is enough to run the whole service standalone.

--------------------------------------------------------------------------------
PRODUCTION SEAM -- read this before wiring the optimizer to real data.

The nightly xP recompute (ingest/, 05:00 UTC) writes the `xpoints` table. The
optimizer must not read that table. The intended production provider fetches the
same nightly job's published artifact -- one immutable JSON blob per
(season, model_version), written to object storage by the cron job and pulled once
per container start, then held in memory for the container's life.

Implementing it means one new class in this file with `snapshot()` doing an HTTP
GET plus `XPSnapshot.from_dict`, and changing the provider constructed in app.py.
Nothing else moves. Specifically it must NOT mean:
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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

# 1 GKP, 2 DEF, 3 MID, 4 FWD -- FPL's element_type, matching db/schema.sql.
GKP, DEF, MID, FWD = 1, 2, 3, 4
POSITION_NAMES = {GKP: "goalkeeper", DEF: "defender", MID: "midfielder", FWD: "forward"}

# ARCHITECTURE.md: "Pre-filter to the top ~150 players by xP per position, plus
# every player currently in the squad. That is the single biggest performance
# lever and it costs almost nothing in solution quality, because the optimal
# transfer target is never the 300th-best midfielder."
DEFAULT_POOL_PER_POSITION = 150


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

    @classmethod
    def from_dict(cls, doc: dict) -> "XPSnapshot":
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
            )
        return cls(
            season=doc["season"],
            model_version=doc["model_version"],
            generated_at=doc["generated_at"],
            players=players,
        )

    def get(self, element_id: int) -> PlayerXP | None:
        return self.players.get(element_id)

    def missing(self, element_ids: Iterable[int]) -> list[int]:
        return [e for e in element_ids if e not in self.players]


class XPProvider(Protocol):
    """The seam. Swap the implementation, not the caller."""

    def snapshot(self, season: str) -> XPSnapshot: ...


class FixtureXPProvider:
    """Loads an xP snapshot from a JSON file on local disk.

    Named for the test fixture, not for football fixtures. This is the
    development and test provider: it makes the service runnable with no
    database, no network and no credentials.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._cache: XPSnapshot | None = None

    def snapshot(self, season: str) -> XPSnapshot:
        # The file is immutable in practice, so one read per process is enough.
        if self._cache is None:
            self._cache = XPSnapshot.from_dict(json.loads(self.path.read_text()))
        return self._cache


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
