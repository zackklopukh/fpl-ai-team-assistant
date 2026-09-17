"""The HTTP contract between the web app and the optimizer.

This file is the boundary described in ARCHITECTURE.md: the web app owns state,
the optimizer owns math. Everything the optimizer knows arrives in an
OptimizeRequest — it has no database credentials and no memory between calls.

Change this file deliberately. Both deployables are written against it, and the
whole point of the split is that the optimizer can be rewritten from scratch
behind a fixed contract.

All money is integer tenths. 55 means £5.5m.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Each solver names itself. These are logged with every recommendation, so a past
# answer stays explainable — which means the label has to say which solver
# actually ran. A single shared constant would quietly stamp MIP answers as
# greedy ones, corrupting the only record by which the model is ever evaluated.
GREEDY_VERSION = "greedy-0.1"
MIP_VERSION = "mip-0.1"

# The service's default when it has not been told otherwise. Prefer passing the
# running solver's own version explicitly.
SOLVER_VERSION = GREEDY_VERSION

MAX_HORIZON = 5  # Beyond five gameweeks the fixture information is too noisy.

Chip = Literal["wildcard", "freehit", "bboost", "3xc"]


class SquadPlayer(BaseModel):
    """A player currently held, with what they would fetch if sold.

    The optimizer does not compute selling price — the caller reconstructs it from
    the transfer log and passes it in. That keeps FPL's pricing rules in one place.
    """

    element_id: int
    selling_price: int = Field(ge=0, description="Integer tenths")
    purchase_price: int | None = Field(
        default=None, ge=0, description="Integer tenths, for display only"
    )


class OptimizeRequest(BaseModel):
    squad: list[SquadPlayer] = Field(min_length=15, max_length=15)
    bank: int = Field(ge=0, description="Integer tenths")
    free_transfers: int = Field(ge=0, le=5)
    current_gw: int = Field(ge=1, le=38)
    horizon: int = Field(default=3, ge=1, le=MAX_HORIZON)
    chips_available: list[Chip] = Field(default_factory=list)
    season: str = "2026-27"

    # Differential mode. A correct optimizer converges on the template squad,
    # because the template is popular precisely because it is close to optimal.
    # This lets a user ask for an answer nobody else has.
    max_ownership: float | None = Field(
        default=None, ge=0, le=100, description="Exclude players owned by more than this %"
    )


class Transfer(BaseModel):
    element_id: int
    web_name: str
    price: int = Field(
        description=(
            "Integer tenths. On a transfer OUT this is what the manager banks — the "
            "reconstructed selling price from the request, not the player's list "
            "price, because those differ and only one of them is money. On a "
            "transfer IN it is the list price paid."
        )
    )


class GameweekBreakdown(BaseModel):
    gw: int
    xp: float
    captain_element_id: int
    n_fixtures: dict[int, int] = Field(
        default_factory=dict,
        description="element_id -> fixtures that gameweek; 0 is a blank, 2 a double",
    )


class Plan(BaseModel):
    """One ranked course of action.

    'Hold your transfer' is frequently optimal, and a tool that never says it does
    not get believed — so a plan with no transfers is a first-class result.
    """

    label: str = Field(description="e.g. 'Hold', 'One transfer', 'Take a -4'")
    transfers_in: list[Transfer] = Field(default_factory=list)
    transfers_out: list[Transfer] = Field(default_factory=list)
    hit_cost: int = Field(default=0, description="Points, 4 per transfer beyond free")
    xi: list[int] = Field(description="Starting eleven, element ids")
    bench_order: list[int] = Field(description="Four bench slots, in autosub order")
    captain: int
    vice_captain: int
    delta_xp: float = Field(description="Expected points gained over the baseline, after hits")
    bank_after: int | None = Field(
        default=None,
        description="Integer tenths left in the bank if this plan is taken, so the UI need not parse it out of prose",
    )
    per_gw_breakdown: list[GameweekBreakdown] = Field(default_factory=list)
    reasoning: str = Field(
        default="", description="Why this plan — the explanation is more the product than the verdict"
    )


class OptimizeResponse(BaseModel):
    baseline_xp: float = Field(description="Expected points from holding, over the horizon")
    plans: list[Plan] = Field(description="Ranked best first; always includes a hold")
    model_version: str
    solver_version: str = SOLVER_VERSION
    data_as_of: str = Field(description="ISO timestamp of the price and xP data used")
    solve_ms: int
    truncated: bool = Field(
        default=False, description="True when the solver hit its time limit and returned an incumbent"
    )
