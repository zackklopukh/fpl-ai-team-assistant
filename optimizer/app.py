"""The optimizer service: one endpoint, stateless, no database.

`POST /optimize` takes an OptimizeRequest and returns an OptimizeResponse. That
contract is fixed (see contract.py) -- this module is a thin shell around it:
validate, look up xP, shrink the pool, solve, time it, cache it.

The solver is the MIP in solver.py. The greedy search in greedy.py stays as the
fallback for the one case it exists for: the MIP raising. Every response says
which of the two actually produced it, because the recommendation log is the
only record by which the model is ever evaluated and a mislabelled answer
corrupts it.

Deliberately importable and runnable on its own, with no database credentials:

    .venv/bin/python ingest/publish_xp.py                  # writes the xP artifact
    .venv/bin/uvicorn optimizer.app:app --reload           # from the repo root

Environment:
    OPTIMIZER_XP_URL / OPTIMIZER_XP_PATH   where the xP artifact lives (pool.py)
    OPTIMIZER_SEASON                       season the artifact must be for
    OPTIMIZER_TIME_LIMIT_S                 hard MIP time limit, default 20
    OPTIMIZER_POOL_PER_POSITION            candidate pool cutoff, default 150

modal_app.py wraps this same `app` object for deployment. Nothing in here knows
about Modal, and nothing in here knows about Postgres.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from optimizer import solver
from optimizer.contract import (
    GREEDY_VERSION,
    MIP_VERSION,
    IdealSquadRequest,
    IdealSquadResponse,
    OptimizeRequest,
    OptimizeResponse,
    Plan,
    SquadPlayer,
)
from optimizer.greedy import SquadError, recommend, validate_squad
from optimizer.pool import (
    DEFAULT_POOL_PER_POSITION,
    ArtifactError,
    SeasonMismatchError,
    XPProvider,
    XPSnapshot,
    candidate_pool,
    provider_from_env,
)

log = logging.getLogger("optimizer")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


# ARCHITECTURE.md: "Set a hard solver time limit (~20 seconds) and return the best
# incumbent solution." The web app's own timeout is 25s, so this leaves room for
# the pool build and the response.
SOLVER_TIME_LIMIT_S = _env_float("OPTIMIZER_TIME_LIMIT_S", solver.DEFAULT_TIME_LIMIT)
POOL_PER_POSITION = _env_int("OPTIMIZER_POOL_PER_POSITION", DEFAULT_POOL_PER_POSITION)
EXPECTED_SEASON = (
    os.environ.get("OPTIMIZER_SEASON") or OptimizeRequest.model_fields["season"].default
)

# Which xP to serve: the published artifact if there is one, the synthetic seed
# (loudly) if not. See pool.provider_from_env. Never a database.
provider: XPProvider = provider_from_env(expected_season=EXPECTED_SEASON)

# ARCHITECTURE.md: "Cache on a hash of (squad, bank, free transfers, gameweek,
# model version). Two users with the same template squad get one solve." An
# in-process dict is the right size of solution -- there is no second process to
# share with, and a Redis would be a service to run for a dictionary. It is
# bounded so a long-lived container cannot grow without limit.
CACHE_MAX_ENTRIES = 512
_cache: "OrderedDict[str, OptimizeResponse]" = OrderedDict()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Load the artifact at container start rather than on the first user's
    # request, and say out loud what was loaded. A failure is logged, not fatal:
    # the load is retried on the next request and /health reports it.
    try:
        snap = provider.snapshot("")
        if snap.synthetic:
            log.warning("serving SYNTHETIC SEED xP from %s -- not real FPL players", snap.source)
        else:
            log.info(
                "serving xP %s / %s generated %s from %s",
                snap.season,
                snap.model_version,
                snap.generated_at,
                snap.source,
            )
    except Exception:
        log.exception("xP data failed to load at startup from %s", provider.source)
    yield


app = FastAPI(
    title="FPL optimizer",
    version=MIP_VERSION,
    description="Stateless squad solver. Receives a squad and returns ranked plans.",
    lifespan=_lifespan,
)


@app.exception_handler(RequestValidationError)
async def _one_shape_for_every_422(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Give pydantic's 422 the same shape as the hand-written squad checks.

    FastAPI's default returns `detail` as a list of error objects while every
    `HTTPException` in this module returns it as a sentence. Two shapes for one
    status means every client writes its own parser, and the second shape is the
    one they forget — so the contract has one shape and it is the readable one.
    """
    problems = []
    for err in exc.errors():
        # Drop the leading "body" segment; it names the framework, not the field.
        location = ".".join(str(part) for part in err["loc"][1:]) or "request"
        problems.append(f"{location}: {err['msg']}")

    return JSONResponse(status_code=422, content={"detail": "; ".join(problems)})


def _cache_key(req: OptimizeRequest, snapshot: XPSnapshot) -> str:
    """Everything that can change the answer goes in the key.

    ARCHITECTURE.md names squad, bank, free transfers, gameweek and model version.
    Horizon and max_ownership are in here too because they demonstrably change the
    result; generated_at because a new xP snapshot must invalidate every entry --
    a cached answer computed against 22:00 prices must not be served after 02:00;
    and the solver's configuration because a longer time limit or a wider pool
    can find a better answer.
    """
    payload = {
        "squad": sorted((s.element_id, s.selling_price) for s in req.squad),
        "bank": req.bank,
        "free_transfers": req.free_transfers,
        "current_gw": req.current_gw,
        "horizon": req.horizon,
        "season": req.season,
        "max_ownership": req.max_ownership,
        "xp_season": snapshot.season,
        "model_version": snapshot.model_version,
        "data_as_of": snapshot.generated_at,
        "xp_source": snapshot.source,
        "solver_version": MIP_VERSION,
        "time_limit": SOLVER_TIME_LIMIT_S,
        "pool_per_position": POOL_PER_POSITION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _load_snapshot(season: str) -> XPSnapshot:
    """The xP to answer with, or a named refusal.

    A request for a different season than the loaded data is the caller's
    problem (422); an unreadable artifact is ours (503). Element ids are
    reassigned between seasons, so answering across seasons would be advice
    about the wrong players.
    """
    try:
        snapshot = provider.snapshot(season)
    except SeasonMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArtifactError as exc:
        log.error("xP artifact unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if season and season != snapshot.season:
        raise HTTPException(
            status_code=422,
            detail=(
                f"request is for season {season!r} but the loaded xP is for "
                f"{snapshot.season!r}. Element ids are reassigned between seasons."
            ),
        )
    return snapshot


@app.get("/health")
def health() -> JSONResponse:
    """Liveness plus the one fact worth knowing about a running container: which
    xP data it loaded. A container serving stale or synthetic data is otherwise
    healthy, so both are spelled out here."""
    try:
        snapshot = provider.snapshot("")
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "source": provider.source,
                "error": str(exc),
                "solver_version": MIP_VERSION,
            },
        )

    body = {
        "ok": True,
        "solver_version": MIP_VERSION,
        "fallback_solver_version": GREEDY_VERSION,
        "season": snapshot.season,
        "model_version": snapshot.model_version,
        "generated_at": snapshot.generated_at,
        "data_as_of": snapshot.generated_at,
        "as_of_gw": snapshot.as_of_gw,
        "target_gws": list(snapshot.target_gws),
        "ingest_run_id": snapshot.ingest_run_id,
        "source": snapshot.source,
        "synthetic": snapshot.synthetic,
        "players_loaded": len(snapshot.players),
        "solver_time_limit_s": SOLVER_TIME_LIMIT_S,
        "pool_per_position": POOL_PER_POSITION,
    }
    if snapshot.synthetic:
        body["warning"] = (
            "SYNTHETIC SEED: invented players whose ids match nothing in FPL. "
            "Publish an artifact with ingest/publish_xp.py to serve real data."
        )
    return JSONResponse(content=body)


def _horizon(req: OptimizeRequest, snapshot: XPSnapshot) -> list[int]:
    """The gameweeks to plan over.

    Runs from the current gameweek forwards. Gameweeks past 38 are dropped rather
    than rejected: asking for a 5-week horizon in GW36 is a reasonable thing to do,
    it just has fewer gameweeks in it. Likewise gameweeks the artifact has no xP
    for are dropped -- planning over a week the model never projected would treat
    every player as scoring zero in it. The breakdown shows which weeks were used.
    """
    gws = [gw for gw in range(req.current_gw, req.current_gw + req.horizon) if gw <= 38]
    if snapshot.target_gws:
        covered = set(snapshot.target_gws)
        if req.current_gw not in covered:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"the loaded xP ({snapshot.model_version}, as of GW{snapshot.as_of_gw}) "
                    f"covers GW{min(covered)}-{max(covered)}, not GW{req.current_gw}"
                ),
            )
        gws = [gw for gw in gws if gw in covered]
    return gws


def _settle_money(plan: Plan, squad: Sequence[SquadPlayer], bank: int) -> Plan:
    """Make a plan's money match what the manager actually gets.

    The MIP reports list price on both sides of a transfer. A sale banks the
    *selling* price, which differs from list price for anyone who has held a
    player through a rise (contract.Transfer.price), so transfers out are
    re-priced from the request. bank_after follows from that, for this
    gameweek's moves -- the ones the plan actually commits to.
    """
    selling = {s.element_id: s.selling_price for s in squad}
    outs = [t.model_copy(update={"price": selling[t.element_id]}) for t in plan.transfers_out]
    bank_after = bank + sum(t.price for t in outs) - sum(t.price for t in plan.transfers_in)
    return plan.model_copy(update={"transfers_out": outs, "bank_after": bank_after})


@app.post("/optimize", response_model=OptimizeResponse)
def optimize(req: OptimizeRequest) -> OptimizeResponse:
    snapshot = _load_snapshot(req.season)

    missing = snapshot.missing(s.element_id for s in req.squad)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"no xP data for element_ids {sorted(missing)} in season {req.season}. "
                "Players are keyed by (season, element_id) and ids are reassigned "
                "between seasons -- check the season on the request."
            ),
        )

    # Composition errors are the caller's, and are named here in one sentence
    # before either solver sees the squad. "Invalid squad" sends a user hunting
    # through fifteen players; "squad has 6 defenders, expected 5" does not.
    try:
        validate_squad([snapshot.players[s.element_id] for s in req.squad])
    except SquadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    key = _cache_key(req, snapshot)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached

    gws = _horizon(req, snapshot)

    started = time.perf_counter()
    squad_ids = [s.element_id for s in req.squad]
    pool = candidate_pool(
        snapshot,
        squad_ids,
        gws,
        max_ownership=req.max_ownership,
        per_position=POOL_PER_POSITION,
    )

    solver_version = MIP_VERSION
    truncated = False
    try:
        result = solver.solve(
            req.squad,
            None,  # read xp_by_gw off the PlayerXP pool objects
            pool,
            req.bank,
            req.free_transfers,
            gws[0],
            len(gws),
            fixtures=snapshot.fixtures_matrix(p.element_id for p in pool),
            time_limit=SOLVER_TIME_LIMIT_S,
        )
        baseline_xp = result.baseline_xp
        plans = [_settle_money(p, req.squad, req.bank) for p in result.plans]
        truncated = result.truncated
    except Exception:
        # The fallback exists for this and nothing else. It is logged with the
        # traceback, labelled greedy in the response, and never cached, so the
        # next identical request gets another go at the MIP.
        log.exception(
            "MIP solver failed for GW%s horizon %s; falling back to %s",
            req.current_gw,
            len(gws),
            GREEDY_VERSION,
        )
        solver_version = GREEDY_VERSION
        try:
            baseline_xp, plans = recommend(
                req.squad,
                pool,
                snapshot.players,
                bank=req.bank,
                free_transfers=req.free_transfers,
                gws=gws,
            )
        except SquadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = OptimizeResponse(
        baseline_xp=baseline_xp,
        plans=plans,
        model_version=snapshot.model_version,
        solver_version=solver_version,
        # The as-of time of the data, never now(). A recommendation computed at
        # 22:00 can be invalid at 02:00 once prices change, so the answer has to
        # carry the timestamp of the prices it was computed from.
        data_as_of=snapshot.generated_at,
        solve_ms=int(round((time.perf_counter() - started) * 1000)),
        truncated=truncated,
        season=snapshot.season,
    )

    if solver_version == MIP_VERSION:
        _cache[key] = response
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return response


# --- The ideal squad ---------------------------------------------------------


def _ideal_cache_key(req: IdealSquadRequest, snapshot: XPSnapshot) -> str:
    """Everything that changes the answer. A from-scratch request is the same for
    every visitor, so after the first solve it is a cache hit for all of them."""
    payload = {
        "kind": "ideal",
        "squad": sorted((s.element_id, s.selling_price) for s in req.squad) if req.squad else None,
        "bank": req.bank if req.squad else None,
        "budget": None if req.squad else req.budget,
        "gw": req.current_gw,
        "horizon": req.horizon,
        "max_ownership": req.max_ownership,
        "season": req.season,
        "model_version": snapshot.model_version,
        "data_as_of": snapshot.generated_at,
        "xp_source": snapshot.source,
        "time_limit": SOLVER_TIME_LIMIT_S,
        "pool": POOL_PER_POSITION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@app.post("/squad/ideal", response_model=IdealSquadResponse)
def ideal_squad(req: IdealSquadRequest) -> IdealSquadResponse:
    """The best fifteen for the horizon: from scratch, or as a wildcard.

    No greedy fallback here. Greedy improves a squad one swap at a time; it
    cannot build one from nothing, so a failure is reported, not papered over.
    """
    snapshot = _load_snapshot(req.season)

    squad_ids: list[int] = []
    if req.squad is not None:
        if len(req.squad) != 15:
            raise HTTPException(
                status_code=422,
                detail=f"a wildcard needs the 15 players held, got {len(req.squad)}",
            )
        squad_ids = [s.element_id for s in req.squad]
        missing = snapshot.missing(squad_ids)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"no xP data for element_ids {sorted(missing)} in season {req.season}. "
                    "Ids are reassigned between seasons -- check the season on the request."
                ),
            )
        try:
            validate_squad([snapshot.players[e] for e in squad_ids])
        except SquadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    key = _ideal_cache_key(req, snapshot)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached

    gws = _horizon(req, snapshot)
    started = time.perf_counter()
    pool = candidate_pool(
        snapshot,
        squad_ids,
        gws,
        max_ownership=req.max_ownership,
        per_position=POOL_PER_POSITION,
    )

    try:
        result = solver.solve_squad(
            req.squad,
            None,  # read xp_by_gw off the PlayerXP pool objects
            pool,
            gws[0],
            len(gws),
            bank=req.bank,
            budget=req.budget,
            fixtures=snapshot.fixtures_matrix(p.element_id for p in pool),
            time_limit=SOLVER_TIME_LIMIT_S,
        )
    except solver.SolverError as exc:
        # An impossible budget is the caller's to fix, and says so plainly.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result["solve_ms"] = int(round((time.perf_counter() - started) * 1000))
    response = IdealSquadResponse(
        **result,
        model_version=snapshot.model_version,
        solver_version=MIP_VERSION,
        data_as_of=snapshot.generated_at,
        season=snapshot.season,
    )
    _cache[key] = response
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
    return response
