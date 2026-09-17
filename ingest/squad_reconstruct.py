"""Rebuild a public manager's squad, with purchase and selling prices.

This is what the money module was for. Given nothing but a team id, the public API
gives you the squad (`entry/{id}/event/{gw}/picks/`), every price ever paid
(`entry/{id}/transfers/`) and today's prices (`bootstrap-static/`). Walk the
transfer log forward from the original squad and you know the purchase price of
every player held; apply FPL's selling rule and you have the selling price — the
number that actually constrains a transfer — without a screenshot and without a
logged-in session.

`reconstruct_squad` is deliberately pure: it takes payloads that someone else
fetched. That is what the web app will call, and it is what can be tested offline
against a recorded fixture.

**Caching, for any caller in a request path.** `entry/{id}/` and
`entry/{id}/transfers/` are the *only* two exceptions to the rule that no
user-facing request touches the FPL API (CLAUDE.md invariant 3). A caller on a
request path must cache each response for at least an hour — a transfer log
changes weekly at most — and rate-limit per team id, so one person holding down
refresh cannot turn into a hundred upstream calls from a shared Vercel IP.

Two caveats worth knowing before trusting a number:

* Picks are private until that gameweek's deadline passes. A manager planning
  transfers on a Friday has a squad this cannot see; `picks=None` is an ordinary
  outcome here, not an error.
* Selling prices are computed against *today's* prices. Reconstructing a squad
  days after the deadline it was set at will not match the squad value FPL showed
  at that deadline, because prices moved in between. That gap is price movement,
  not a bug.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from config import POSITIONS, SEASON
from fpl_client import FPLClient
from money import Holding, reconstruct_purchase_prices, squad_value

log = logging.getLogger(__name__)

# Free transfers accumulate to a cap. Kept here rather than inlined because it is
# a rule FPL has changed before and will change again.
MAX_FREE_TRANSFERS = 5

# Chips that make a gameweek's transfers cost nothing.
UNLIMITED_TRANSFER_CHIPS = {"wildcard", "freehit"}


def price_tables(
    bootstrap: dict[str, Any],
) -> tuple[dict[int, int], dict[int, int], dict[int, dict[str, Any]]]:
    """(current price, season price change, element metadata), all keyed by id.

    Prices stay integer tenths the whole way through — `now_cost` is already in
    tenths and is never divided here.
    """
    prices_now: dict[int, int] = {}
    changes_start: dict[int, int] = {}
    meta: dict[int, dict[str, Any]] = {}

    for element in bootstrap.get("elements", []):
        element_id = int(element["id"])
        prices_now[element_id] = int(element["now_cost"])
        changes_start[element_id] = int(element.get("cost_change_start") or 0)
        meta[element_id] = element

    return prices_now, changes_start, meta


def transfers_by_gameweek(transfers: list[dict[str, Any]]) -> dict[int, int]:
    """How many transfers were made in each gameweek."""
    counts: dict[int, int] = {}
    for row in transfers:
        event = int(row["event"])
        counts[event] = counts.get(event, 0) + 1
    return counts


def free_transfers(
    transfers: list[dict[str, Any]],
    up_to_gw: int,
    chip_gws: dict[int, str] | None = None,
) -> int:
    """Free transfers available going into the gameweek after `up_to_gw`.

    An estimate, and labelled as one in the output. The transfer log is public but
    the chip log is not — `entry/{id}/history/` carries it, but a caller holding
    only one gameweek's picks knows about one chip. Pass `chip_gws` when you have
    more; transfers made under a wildcard or free hit cost nothing.

    The rule modelled: one free transfer per gameweek, unused ones carry over to a
    cap of five, and you can never bank fewer than none.
    """
    chip_gws = chip_gws or {}
    used = transfers_by_gameweek(transfers)

    available = 1
    for gw in range(1, up_to_gw + 1):
        if gw > 1:
            available = min(MAX_FREE_TRANSFERS, available + 1)
        if chip_gws.get(gw) in UNLIMITED_TRANSFER_CHIPS:
            continue
        available = max(0, available - used.get(gw, 0))

    # The next gameweek's allowance.
    return min(MAX_FREE_TRANSFERS, available + 1)


def reconstruct_squad(
    team_id: int,
    bootstrap: dict[str, Any],
    transfers: list[dict[str, Any]],
    picks: dict[str, Any] | None,
    entry: dict[str, Any] | None = None,
    season: str = SEASON,
) -> dict[str, Any]:
    """Build a squad with purchase and selling prices from already-fetched payloads.

    Pure: no network, no database. All money in integer tenths.

    `picks` may be None — the picks endpoint 404s until that gameweek's deadline
    has passed. The result then carries `picks_available: False` and an empty
    squad, with whatever the entry payload could tell us still filled in, rather
    than raising.

    See the module docstring for the caching obligation on any caller that sits in
    a user-facing request path.
    """
    entry = entry or {}
    prices_now, changes_start, meta = price_tables(bootstrap)

    result: dict[str, Any] = {
        "team_id": team_id,
        "season": season,
        "picks_available": picks is not None,
        "manager_name": " ".join(
            part
            for part in (entry.get("player_first_name"), entry.get("player_last_name"))
            if part
        )
        or None,
        "team_name": entry.get("name"),
    }

    if picks is None:
        # Pre-deadline. The entry summary still has last deadline's bank, which is
        # better than nothing for a caller that just wants to show something.
        result.update(
            {
                "gw": entry.get("current_event"),
                "active_chip": None,
                "bank_tenths": entry.get("last_deadline_bank"),
                "players": [],
                "selling_total_tenths": None,
                "squad_value_tenths": entry.get("last_deadline_value"),
                "free_transfers": None,
                "free_transfers_is_estimate": True,
                "unpriced_elements": [],
            }
        )
        return result

    history = picks.get("entry_history") or {}
    gw = int(history.get("event") or entry.get("current_event") or 0)
    active_chip = picks.get("active_chip")

    squad = [int(p["element"]) for p in picks.get("picks", [])]
    purchase_prices = reconstruct_purchase_prices(
        squad, transfers, prices_now, changes_start
    )

    # A squad member absent from bootstrap means stale reference data, not a
    # missing player. Reported rather than raised so one bad id does not deny the
    # user the other fourteen; their selling price falls back to what they cost,
    # which is the only defensible guess.
    unpriced = [e for e in squad if e not in prices_now]

    holdings = [
        Holding(
            element_id=element_id,
            purchase_price=purchase_prices[element_id],
            current_price=prices_now.get(element_id, purchase_prices[element_id]),
        )
        for element_id in squad
    ]
    by_id = {h.element_id: h for h in holdings}

    players: list[dict[str, Any]] = []
    for pick in picks.get("picks", []):
        element_id = int(pick["element"])
        holding = by_id[element_id]
        element = meta.get(element_id, {})
        players.append(
            {
                "element_id": element_id,
                "web_name": element.get("web_name"),
                "element_type": element.get("element_type") or pick.get("element_type"),
                "team_fpl_id": element.get("team"),
                "squad_position": pick.get("position"),
                "on_bench": (pick.get("position") or 0) > 11,
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "multiplier": pick.get("multiplier"),
                "purchase_price_tenths": holding.purchase_price,
                "current_price_tenths": holding.current_price,
                "selling_price_tenths": holding.selling_price,
            }
        )

    bank = history.get("bank")
    if bank is None:
        bank = entry.get("last_deadline_bank") or 0
    bank = int(bank)

    chip_gws = {gw: active_chip} if active_chip else {}

    result.update(
        {
            "gw": gw,
            "active_chip": active_chip,
            "bank_tenths": bank,
            "players": players,
            "selling_total_tenths": sum(h.selling_price for h in holdings),
            "squad_value_tenths": squad_value(holdings, bank),
            "free_transfers": free_transfers(transfers, gw, chip_gws),
            "free_transfers_is_estimate": True,
            "unpriced_elements": unpriced,
        }
    )
    return result


def latest_available_gw(bootstrap: dict[str, Any]) -> int | None:
    """The most recent gameweek whose deadline has passed, so picks are public."""
    played = [
        int(event["id"])
        for event in bootstrap.get("events", [])
        if event.get("finished") or event.get("is_current")
    ]
    return max(played) if played else None


def fetch_and_reconstruct(team_id: int, season: str = SEASON) -> dict[str, Any]:
    """Thin I/O wrapper: fetch the three payloads, then hand them to the pure part.

    Not for a request path — see the module docstring. This exists so the CLI and
    an offline experiment have one way in.
    """
    with FPLClient() as client:
        bootstrap = client.bootstrap_static()
        entry = client.entry(team_id)
        transfers = client.entry_transfers(team_id)

        gw = entry.get("current_event") or latest_available_gw(bootstrap)
        picks = None
        if gw:
            try:
                picks = client.entry_picks(team_id, int(gw))
            except Exception as exc:  # noqa: BLE001 — a 404 here is expected pre-deadline
                log.info("picks for team %s gw %s not available yet: %s", team_id, gw, exc)

    return reconstruct_squad(team_id, bootstrap, transfers, picks, entry, season)


def _money(tenths: int | None) -> str:
    """Render integer tenths for humans. Display only — never feed this back in."""
    if tenths is None:
        return "—"
    return f"{tenths // 10}.{tenths % 10}"


def format_squad(squad: dict[str, Any]) -> str:
    lines: list[str] = []
    header = f"Team {squad['team_id']}"
    if squad.get("team_name"):
        header += f" — {squad['team_name']}"
    if squad.get("manager_name"):
        header += f" ({squad['manager_name']})"
    lines.append(header)

    if not squad["picks_available"]:
        lines.append(
            "Picks are not public yet for this gameweek — they unlock at the deadline."
        )
        lines.append(f"Last deadline bank: {_money(squad.get('bank_tenths'))}")
        return "\n".join(lines)

    lines.append(f"Gameweek {squad['gw']}" + (f"  chip: {squad['active_chip']}" if squad["active_chip"] else ""))
    lines.append("")
    lines.append(f"{'#':>2}  {'pos':<3} {'player':<18} {'bought':>7} {'now':>6} {'sells':>6}")
    lines.append("-" * 50)

    for player in squad["players"]:
        marker = "C" if player["is_captain"] else ("V" if player["is_vice_captain"] else "")
        name = f"{player['web_name'] or player['element_id']} {marker}".strip()
        if player["on_bench"]:
            name = f"({name})"
        lines.append(
            f"{player['squad_position']:>2}  "
            f"{POSITIONS.get(player['element_type'], '?'):<3} "
            f"{name:<18} "
            f"{_money(player['purchase_price_tenths']):>7} "
            f"{_money(player['current_price_tenths']):>6} "
            f"{_money(player['selling_price_tenths']):>6}"
        )

    lines.append("-" * 50)
    lines.append(f"Selling total  {_money(squad['selling_total_tenths']):>7}")
    lines.append(f"Bank           {_money(squad['bank_tenths']):>7}")
    lines.append(f"Squad value    {_money(squad['squad_value_tenths']):>7}")
    lines.append(f"Free transfers {squad['free_transfers']:>7}  (estimated from the transfer log)")

    if squad["unpriced_elements"]:
        lines.append(
            f"Warning: no current price for elements {squad['unpriced_elements']} — "
            "stale bootstrap data; their selling price fell back to what was paid."
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct a public FPL squad.")
    parser.add_argument("team_id", type=int, help="the public entry id from the FPL URL")
    parser.add_argument("--season", default=SEASON)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(format_squad(fetch_and_reconstruct(args.team_id, args.season)))


if __name__ == "__main__":
    main()
