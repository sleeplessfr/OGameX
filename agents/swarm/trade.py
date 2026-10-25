"""When is it worth calling the merchant? Pure functions: numbers in, a decision out.

Facts of the game (app/Services/MerchantService.php): a call costs 3,500 dark matter and every account starts with 8,000,
so an agent can afford about two trades in its life (unless the owner gives it more). The rates are only shown after
paying, they are random (about 70% to 100% of the fair 3:2:1 value, 90% on average), and the merchant leaves after ONE trade.

So a trade is a rare, planned move: the agent must be stuck waiting for something, own a surplus of a resource it does
not need for that goal, and the surplus must be big enough to cover the whole shortfall. Then it sells the surplus.
"""
from __future__ import annotations

from dataclasses import dataclass

from .brain import RESOURCES, VALUE
from .personas import Persona

CALL_COST = 3500            # dark matter (the game's MerchantService::DARK_MATTER_COST; the API also reports it)
MIN_WAIT_SECONDS = 1200.0   # only worth it when the wait is long (unknown wait = nothing is produced = also worth it)
COOLDOWN_SECONDS = 6 * 3600.0
WORST_RATE = 0.70           # the merchant pays at least this share of the fair value
MIN_SHORTFALL_VALUE = 20000.0   # a trade must cover at least this much (metal-equivalent): dark matter is too scarce for small change
MARGIN = 1.03               # ask for a little more than the shortfall: stores keep filling while we trade


@dataclass
class Deal:
    receive: dict[str, int]   # what to ask for
    give_amount: int          # the most to give
    sell: str


def dark_matter_reserve(persona: Persona) -> int:
    """Patient agents keep one call in reserve; the others spend freely."""
    return CALL_COST if persona.patience >= 0.8 else 0


def shortfall(goal_cost: dict, have: dict) -> dict[str, int]:
    return {r: int(goal_cost.get(r, 0) - have.get(r, 0)) for r in RESOURCES if goal_cost.get(r, 0) > have.get(r, 0)}


def surplus(goal_cost: dict, have: dict) -> dict[str, int]:
    """What can be sold: stock beyond what the goal itself needs (and nothing from a resource we are short of)."""
    return {r: int(have.get(r, 0) - goal_cost.get(r, 0)) for r in RESOURCES if have.get(r, 0) > goal_cost.get(r, 0)}


def _value(amounts: dict) -> float:
    return sum(VALUE[r] * v for r, v in amounts.items())


def choose_sale(persona: Persona, goal_cost: dict | None, have: dict, storage: dict, wait_seconds: float | None,
                dark_matter: int, now: float, cooldown_until: float) -> str | None:
    """Which resource to offer a new merchant, or None for "do not call one"."""
    if not goal_cost or now < cooldown_until:
        return None
    if dark_matter < CALL_COST + dark_matter_reserve(persona):
        return None
    if wait_seconds is not None and wait_seconds < MIN_WAIT_SECONDS:
        return None
    missing = shortfall(goal_cost, have)
    if not missing:
        return None
    if _value(missing) < MIN_SHORTFALL_VALUE:
        return None
    # The shortfall must fit into the stores, or the goal can never be bought anyway.
    if any(goal_cost.get(r, 0) > storage.get(r, 0) > 0 for r in missing):
        return None
    spare = surplus(goal_cost, have)
    spare = {r: v for r, v in spare.items() if r not in missing}
    if not spare:
        return None
    # Even at the worst rate the best surplus must pay for the whole shortfall.
    best = max(spare, key=lambda r: VALUE[r] * spare[r])
    if VALUE[best] * spare[best] * WORST_RATE < _value(missing) * MARGIN:
        return None
    return best


def plan_deal(merchant: dict, goal_cost: dict, have: dict, storage: dict) -> Deal | None:
    """Given a merchant who is already here (type + rates), the trade that buys the whole shortfall, or None."""
    sell = merchant["type"]
    missing = shortfall(goal_cost, have)
    if not missing or any(r == sell or r not in merchant["receive"] for r in missing):
        return None
    receive: dict[str, int] = {}
    cost = 0.0
    for r, need in missing.items():
        want = int(need * MARGIN) + 1
        if storage.get(r) and have.get(r, 0) + want > storage[r]:
            want = int(storage[r] - have.get(r, 0))
            if want < need:
                return None   # does not fit into the store
        per_unit = float(merchant["receive"][r]["per_unit_given"])
        if per_unit <= 0:
            return None
        receive[r] = want
        cost += want / per_unit
    spare = have.get(sell, 0) - goal_cost.get(sell, 0)
    if cost * 1.01 + 1 > spare:
        return None
    return Deal(receive=receive, give_amount=int(cost * 1.01) + 2, sell=sell)
