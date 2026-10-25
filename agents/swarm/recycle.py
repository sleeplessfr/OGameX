"""Recycling debris fields. Pure functions: game data in, a choice out (no network).

Where debris comes from: every fight leaves part of the destroyed ships as metal and crystal at the coordinates of the
target planet. The API does NOT show debris fields in `GET /galaxy/{g}/{s}` (only planets, owners and moons), so an agent
can only learn about them from reports it reads anyway:

  * its own battle reports (`debris`, as attacker and as defender: the field lies at the defender's planet), and
  * spy reports (`debris`: the field at the spied planet right now).

It remembers them (`Memory.debris`), forgets them after a while (others may recycle them first) and, when the haul is big
enough, buys recyclers and sends them: `POST /planets/{id}/fleet` with mission "recycle", type "debris_field". The ships
come back on their own and unload what they picked up, so the agent only has to send them.
"""
from __future__ import annotations

from .brain import RESOURCES
from .military import distance
from .personas import Persona

RECYCLER = "recycler"
DEFAULT_CARGO = 20000       # OGame recycler capacity; the real figure comes from GET /ships (research applied)
DEBRIS_TTL = 24 * 3600.0    # a remembered field this old is forgotten: somebody else has probably taken it
MIN_DEBRIS = 4000           # smaller fields are not worth a trip
BIG_DEBRIS = 60000          # a peaceful agent buys recyclers only for this much remembered debris
MIN_BUY_DEBRIS = 15000      # a fighter buys recyclers only for this much (a recycler costs ~18,000 in total)
MAX_RECYCLERS = 20          # never aims for a bigger fleet than this
BUY_SHARE = 0.4             # at most this share of the stock goes on recyclers in one order
DEUT_VALUE = 3.0            # a unit of deuterium is worth this many metal when weighing the fuel against the haul
WORTH_FACTOR = 2.0          # the haul must be worth at least this many times the fuel burned
MAX_DISTANCE = 300          # systems (military.distance); further than that the trip is not even considered
RESEND_MARGIN = 30.0        # seconds after the return trip before the agent trusts that the ships are home

Coord = tuple[int, int, int]


def fights(persona: Persona) -> bool:
    """Raider / balanced / general-class personalities fight, so debris keeps appearing around them."""
    return persona.character_class == "general" or persona.aggression >= 0.2


def total_of(debris) -> int:
    """Metal plus crystal of a `debris` dict from a report (deuterium cannot be recycled and is not counted)."""
    if not isinstance(debris, dict):
        return 0
    try:
        return sum(max(0, int(debris.get(r) or 0)) for r in ("metal", "crystal"))
    except (TypeError, ValueError):
        return 0


# ---- memory -------------------------------------------------------------------------------------------------------
def remember(fields: dict, coord, amount: int, now: float, *, add: bool) -> None:
    """Note debris at `coord`. A battle report says how much that fight added (add=True); a spy report says how much is
    lying there now (add=False, replaces what was known; zero means the field is empty)."""
    if coord is None or None in coord:
        return
    c = (int(coord[0]), int(coord[1]), int(coord[2]))
    amount = max(0, int(amount))
    old = fields.get(c)
    if add:
        if amount <= 0:
            return
        amount += int(old["amount"]) if old else 0
        sent_until = old.get("sent_until", 0.0) if old else 0.0
    else:
        if amount <= 0:
            fields.pop(c, None)
            return
        sent_until = old.get("sent_until", 0.0) if old else 0.0
    fields[c] = {"at": now, "amount": amount, "sent_until": sent_until}


def forget_old(fields: dict, now: float) -> None:
    for c, info in list(fields.items()):
        if not isinstance(info, dict) or now - float(info.get("at", 0)) > DEBRIS_TTL or int(info.get("amount", 0)) < MIN_DEBRIS:
            del fields[c]


def remembered_total(fields: dict, now: float) -> int:
    return sum(int(i.get("amount", 0)) for i in fields.values() if isinstance(i, dict) and now - float(i.get("at", 0)) <= DEBRIS_TTL)


# ---- buying -------------------------------------------------------------------------------------------------------
def recyclers_needed(amount: int, cargo: int = DEFAULT_CARGO) -> int:
    """Recyclers to carry `amount` in one trip (ceiling division, capped)."""
    if amount <= 0 or cargo <= 0:
        return 0
    return min(MAX_RECYCLERS, -(-int(amount) // int(cargo)))


def recyclers_to_buy(persona: Persona, item: dict | None, have: dict, debris_total: int, owned: int,
                     cargo: int = DEFAULT_CARGO) -> tuple[int, str]:
    """How many recyclers to order now, with a reason. `item` is the recycler row of GET /planets/{id}/units."""
    if item is None or not item.get("requirements_met", True) or not item.get("character_class_met", True):
        return 0, "recyclers cannot be built yet"
    if item.get("max_buildable", 0) < 1:
        return 0, "cannot afford a recycler"
    need = recyclers_needed(debris_total, cargo) - owned
    if need <= 0:
        return 0, "enough recyclers"
    big = debris_total >= BIG_DEBRIS
    if not (big or (fights(persona) and debris_total >= MIN_BUY_DEBRIS)):
        return 0, "debris too small for this personality to buy recyclers"
    cost = item.get("cost_per_unit") or {}
    limits = [int(have.get(r, 0) * BUY_SHARE // cost[r]) for r in RESOURCES if cost.get(r, 0) > 0]
    n = min([need, int(item["max_buildable"]), *limits]) if limits else 0
    return (n, f"{debris_total:,} debris remembered") if n >= 1 else (0, "recyclers too expensive for the spare stock")


# ---- sending ------------------------------------------------------------------------------------------------------
def pick_field(fields: dict, now: float, home: Coord, *, max_distance: int = MAX_DISTANCE) -> tuple[Coord, int] | None:
    """The best remembered field to visit: biggest haul per system of distance, ignoring ones already being visited."""
    best = None
    for c, info in fields.items():
        if not isinstance(info, dict) or now - float(info.get("at", 0)) > DEBRIS_TTL or now < float(info.get("sent_until", 0)):
            continue
        amount = int(info.get("amount", 0))
        d = distance(home, c)
        if amount < MIN_DEBRIS or d > max_distance:
            continue
        score = amount / (1 + d)
        if best is None or score > best[0]:
            best = (score, c, amount)
    return (best[1], best[2]) if best else None


def worth_trip(haul: int, deut_cost: int, deut_have: int) -> tuple[bool, str]:
    """Is it worth sending the ships? The fuel must be there and cost clearly less than what comes back."""
    if haul < MIN_DEBRIS:
        return False, f"haul {haul:,} too small"
    if deut_cost > deut_have:
        return False, f"needs {deut_cost:,} deuterium, has {deut_have:,}"
    if haul < WORTH_FACTOR * DEUT_VALUE * deut_cost:
        return False, f"haul {haul:,} not worth {deut_cost:,} deuterium"
    return True, f"haul {haul:,} for {deut_cost:,} deuterium"


def fleet_for(amount: int, owned: int, cargo: int = DEFAULT_CARGO) -> int:
    """How many of the owned recyclers to send: enough to carry the field, not more."""
    return max(0, min(owned, recyclers_needed(amount, cargo)))


def after_sending(fields: dict, coord: Coord, sent_cargo: int, now: float, round_trip_s: float) -> None:
    """Book the trip: what the ships will carry is no longer there, and nobody else is sent meanwhile."""
    info = fields.get(coord)
    if info is None:
        return
    info["amount"] = max(0, int(info["amount"]) - int(sent_cargo))
    info["sent_until"] = now + round_trip_s + RESEND_MARGIN
    if info["amount"] < MIN_DEBRIS:
        del fields[coord]
