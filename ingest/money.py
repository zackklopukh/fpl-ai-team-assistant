"""FPL money rules, in integer tenths throughout.

55 means £5.5m. There are no floats in this module and there should never be one:
the selling rule floors a halved difference, and float arithmetic turns that into
an off-by-one squad value that looks almost right and costs you a transfer.

The one rule everything else derives from:

    A player's selling price is the purchase price plus half of any rise,
    rounded down to the nearest £0.1m. A fall is passed on in full.
"""

from __future__ import annotations

from dataclasses import dataclass


def selling_price(purchase_price: int, current_price: int) -> int:
    """What the manager gets back for a player they paid `purchase_price` for.

    Both arguments and the result are integer tenths.

    >>> selling_price(50, 53)   # rose 0.3, keeps half rounded down
    51
    >>> selling_price(50, 54)   # rose 0.4, keeps exactly half
    52
    >>> selling_price(50, 47)   # falls are passed on in full
    47
    >>> selling_price(50, 50)
    50
    """
    if current_price <= purchase_price:
        return current_price

    rise = current_price - purchase_price
    return purchase_price + rise // 2


def start_price(now_cost: int, cost_change_start: int) -> int:
    """A player's price on day one of the season.

    This is the purchase price for anyone still holding a player from their
    original squad, which is most of the squad for most managers.
    """
    return now_cost - cost_change_start


@dataclass(frozen=True)
class Holding:
    """A player currently in a squad, with what they cost and what they fetch."""

    element_id: int
    purchase_price: int
    current_price: int

    @property
    def selling_price(self) -> int:
        return selling_price(self.purchase_price, self.current_price)


def reconstruct_purchase_prices(
    current_squad: list[int],
    transfers: list[dict],
    prices_now: dict[int, int],
    price_changes_start: dict[int, int],
) -> dict[int, int]:
    """Work out what a manager paid for each player they currently hold.

    `transfers` is the raw `/entry/{id}/transfers/` payload — newest first, as the
    API returns it. Each row carries `element_in`, `element_in_cost`,
    `element_out`, `element_out_cost` and `event`, with costs in tenths.

    A player bought more than once — sold in GW4, bought back in GW9 — must resolve
    to the *most recent* purchase, which is why the log is walked oldest-first and
    later buys overwrite earlier ones.

    Anyone in the squad with no purchase in the log came from the original squad,
    so their purchase price is their day-one price.

    Returns element_id -> purchase price in tenths.
    """
    paid: dict[int, int] = {}

    for row in sorted(transfers, key=lambda r: (r["event"], r["time"])):
        paid[row["element_in"]] = row["element_in_cost"]

    purchase_prices: dict[int, int] = {}

    for element_id in current_squad:
        if element_id in paid:
            purchase_prices[element_id] = paid[element_id]
            continue

        if element_id not in prices_now:
            raise KeyError(
                f"element {element_id} is in the squad but not in the price list — "
                "stale bootstrap data, or an element id from another season"
            )

        purchase_prices[element_id] = start_price(
            prices_now[element_id], price_changes_start.get(element_id, 0)
        )

    return purchase_prices


def squad_value(holdings: list[Holding], bank: int) -> int:
    """Total sellable value: what the squad fetches plus what is in the bank."""
    return sum(h.selling_price for h in holdings) + bank
