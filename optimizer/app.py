"""The optimizer service: one endpoint, stateless, no database.

`POST /optimize` takes an OptimizeRequest and returns an OptimizeResponse. That
contract is fixed (see contract.py) -- this module is a thin shell around it:
validate, look up xP, shrink the pool, solve, time it, cache it.

Deliberately importable and runnable on its own:

    .venv/bin/uvicorn optimizer.app:app --reload     # from the repo root

modal_app.py wraps this same `app` object for deployment. Nothing in here knows
about Modal, and nothing in here knows about Postgres.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from optimizer.contract import SOLVER_VERSION, OptimizeRequest, OptimizeResponse
from optimizer.greedy import SquadError, recommend
from optimizer.pool import FixtureXPProvider, XPProvider, candidate_pool

# The development provider reads a local JSON snapshot. Production swaps this one
# line for a provider that pulls the nightly published artifact -- see the
# PRODUCTION SEAM note in pool.py. The optimizer never gets database credentials.
DEFAULT_XP_PATH = Path(__file__).parent / "tests" / "seed_xp.json"
XP_PATH = Path(os.environ.get("OPTIMIZER_XP_PATH", DEFAULT_XP_PATH))

provider: XPProvider = FixtureXPProvider(XP_PATH)

# ARCHITECTURE.md: "Cache on a hash of (squad, bank, free transfers, gameweek,
# model version). Two users with the same template squad get one solve." An
# in-process dict is the right size of solution -- there is no second process to
# share with, and a Redis would be a service to run for a dictionary. It is
# bounded so a long-lived container cannot grow without limit.
CACHE_MAX_ENTRIES = 512
_cache: "OrderedDict[str, OptimizeResponse]" = OrderedDict()

app = FastAPI(
    title="FPL optimizer",
    version=SOLVER_VERSION,
    description="Stateless squad solver. Receives a squad and returns ranked plans.",
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


def _cache_key(req: OptimizeRequest, model_version: str, data_as_of: str) -> str:
    """Everything that can change the answer goes in the key.

    ARCHITECTURE.md names squad, bank, free transfers, gameweek and model version.
    Horizon and max_ownership are in here too because they demonstrably change the
    result, and data_as_of because a new xP snapshot must invalidate every entry --
    a cached answer computed against 22:00 prices must not be served after 02:00.
    """
    payload = {
        "squad": sorted((s.element_id, s.selling_price) for s in req.squad),
        "bank": req.bank,
        "free_transfers": req.free_transfers,
        "current_gw": req.current_gw,
        "horizon": req.horizon,
        "season": req.season,
        "max_ownership": req.max_ownership,
        "model_version": model_version,
        "solver_version": SOLVER_VERSION,
        "data_as_of": data_as_of,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@app.get("/health")
def health() -> dict:
    """Liveness plus the one fact worth knowing about a running container: which
    xP snapshot it loaded. A container serving stale data is otherwise healthy."""
    snapshot = provider.snapshot("")
    return {
        "ok": True,
        "solver_version": SOLVER_VERSION,
        "model_version": snapshot.model_version,
        "data_as_of": snapshot.generated_at,
        "players_loaded": len(snapshot.players),
    }


@app.post("/optimize", response_model=OptimizeResponse)
def optimize(req: OptimizeRequest) -> OptimizeResponse:
    snapshot = provider.snapshot(req.season)

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

    key = _cache_key(req, snapshot.model_version, snapshot.generated_at)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached

    # The horizon runs from the current gameweek forwards. Gameweeks past 38 are
    # dropped rather than rejected: asking for a 5-week horizon in GW36 is a
    # reasonable thing to do, it just has fewer gameweeks in it.
    gws = [gw for gw in range(req.current_gw, req.current_gw + req.horizon) if gw <= 38]

    started = time.perf_counter()
    squad_ids = [s.element_id for s in req.squad]
    pool = candidate_pool(
        snapshot,
        squad_ids,
        gws,
        max_ownership=req.max_ownership,
    )

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
        # 422 with the actual problem named. "Invalid squad" sends a user hunting
        # through fifteen players; "squad has 6 defenders, expected 5" does not.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = OptimizeResponse(
        baseline_xp=baseline_xp,
        plans=plans,
        model_version=snapshot.model_version,
        solver_version=SOLVER_VERSION,
        # The as-of time of the data, never now(). A recommendation computed at
        # 22:00 can be invalid at 02:00 once prices change, so the answer has to
        # carry the timestamp of the prices it was computed from.
        data_as_of=snapshot.generated_at,
        solve_ms=int(round((time.perf_counter() - started) * 1000)),
        truncated=False,  # the greedy search is exhaustive; only the MIP can truncate
    )

    _cache[key] = response
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
    return response
