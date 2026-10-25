"""Moving things between an agent's own planets: supplies for a starved colony, and saving the fleet from a raid.

Pure functions, no network: game data in, a decision out (agent.py does the calls).

SUPPLY. The richest planet (usually home) sends part of its stock to a colony that has far less, in the cargo ships the
agent already owns (mission `transport`, the ships fly home alone). It keeps a reserve, and the trip is skipped when the
fuel costs more than MAX_FUEL_SHARE of what is carried.

FLEET SAVING. `GET /fleet` shows a hostile fleet heading for one of our planets (with the target planet). If our ships
are there and the fight cannot be won, they leave at once on a `deployment` to another own planet (the one farthest from
the attacker that is not under attack itself), carrying the planet's resources as far as they fit. After the raid they
are sent back the same way. A deployment has no automatic return trip, so the agent remembers it (Memory.away).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .brain import RESOURCES, VALUE
from .combat import estimate_battle, unit_value
from .military import CARGOS, distance

RESERVE_SHARE = 0.3        # the source keeps at least this share of each resource
STARVED_RATIO = 0.4        # a colony is starved when its stock is worth less than this share of the source's
MIN_SOURCE_VALUE = 30_000  # a source poorer than this (metal-equivalents) has nothing to spare
MIN_SEND_VALUE = 15_000    # not worth a trip below this
MAX_FUEL_SHARE = 0.05      # skip the trip when the deuterium costs more than this share of the cargo's value
SUPPLY_COOLDOWN = 900.0    # server seconds between two supply trips (also waits until the last one has arrived)
BUILD_CARGO_MAX = 5        # a miner / turtle without cargo ships may order this many small cargos for supplies...
BUILD_CARGO_SHARE = 0.1    # ...if they cost at most this share of the stock's value
BUILDERS = ("miner", "turtle", "trader")

MIN_FLEET_VALUE = 40_000   # a fleet worth less than this is not worth a flight (a few probes, one or two fighters)
DANGER_GRACE = 90.0        # seconds after the last landing before the fleet may come home
AWAY_GIVE_UP = 6 * 3600.0  # forget a fleet that never turned up at its refuge
ATTACKER_MARGIN = 1.5      # the attacker is assumed to bring this many times the strength last seen
STAY_IF_ATTACKER_WINS_BELOW = 0.2   # stay only when the simulated attacker wins in at most this share of runs
IMMOBILE = ("solar_satellite", "crawler")
ATTACKS = ("attack", "acs_attack")   # missiles cannot be dodged


# ---- supply ---------------------------------------------------------------------------------------------------
def stock_value(res: dict) -> float:
    return sum(float(res.get(r, 0) or 0) * VALUE[r] for r in RESOURCES)


def pick_supply_route(planets: list[dict]) -> tuple[dict, dict] | None:
    """(source, colony): the richest planet and the poorest one with room to store what we send. None = nothing to do."""
    own = [p for p in planets if p.get("type", "planet") == "planet" and p.get("resources")]
    if len(own) < 2:
        return None
    src = max(own, key=lambda p: stock_value(p["resources"]))
    src_value = stock_value(src["resources"])
    if src_value < MIN_SOURCE_VALUE:
        return None
    best = None
    for p in own:
        if p is src:
            continue
        value = stock_value(p["resources"])
        if value >= STARVED_RATIO * src_value:
            continue
        if sum(_room(p, r) * VALUE[r] for r in RESOURCES) < MIN_SEND_VALUE:
            continue
        if best is None or value < best[0]:
            best = (value, p)
    return (src, best[1]) if best else None


def _room(planet: dict, r: str) -> int:
    store = (planet.get("storage") or {}).get(r)
    if store is None:
        return 10 ** 9
    return max(0, int(store) - int(planet["resources"].get(r, 0) or 0))


def supply_amounts(src: dict, dst: dict) -> dict[str, int]:
    """Half the gap per resource, never more than the source can spare (reserve kept) or the colony can store."""
    out = {}
    for r in RESOURCES:
        have, there = int(src["resources"].get(r, 0) or 0), int(dst["resources"].get(r, 0) or 0)
        spare = int(have * (1 - RESERVE_SHARE))
        out[r] = max(0, min((have - there) // 2, spare, _room(dst, r)))
    return out


def pick_cargo(ships_here: dict, stats: dict, need: int, exclude: tuple = ()) -> tuple[dict[str, int], int]:
    """Cargo ships (largest first) that carry `need` units: ({name: amount}, capacity). Less if there are not enough."""
    kinds = sorted((n for n in CARGOS if n not in exclude and ships_here.get(n, 0) > 0 and cap_of(stats, n) > 0),
                   key=lambda n: -cap_of(stats, n))
    chosen: dict[str, int] = {}
    capacity = 0
    for n in kinds:
        if capacity >= need:
            break
        c = cap_of(stats, n)
        take = min(ships_here[n], -(-(need - capacity) // c))
        chosen[n] = take
        capacity += take * c
    return chosen, capacity


def cap_of(stats: dict, name: str) -> int:
    try:
        return int((stats.get(name) or {}).get("cargo_capacity") or 0)
    except (TypeError, ValueError):
        return 0


def fit_to_capacity(amounts: dict[str, int], capacity: int) -> dict[str, int]:
    """Scale the amounts down together so the sum fits in the hold."""
    total = sum(amounts.values())
    if total <= capacity or total <= 0:
        return dict(amounts)
    k = capacity / total
    return {r: int(v * k) for r, v in amounts.items()}


def fuel_ok(amounts: dict, fuel: int) -> bool:
    """False when the deuterium for the trip costs more than MAX_FUEL_SHARE of the cargo."""
    return fuel * VALUE["deuterium"] <= MAX_FUEL_SHARE * stock_value(amounts)


def choose_cargo_build(persona, units: list[dict], have: dict, ships_here: dict) -> tuple[str, int] | None:
    """A miner or turtle with no cargo ship at all may order a few small cargos so it can feed its colony. Capped."""
    if persona.name not in BUILDERS or any(ships_here.get(n, 0) > 0 for n in CARGOS):
        return None
    item = next((u for u in units if u["machine_name"] == "small_cargo" and u.get("requirements_met")
                 and u.get("character_class_met", True) and u.get("max_buildable", 0) > 0), None)
    if item is None:
        return None
    cost = item["cost_per_unit"]
    unit_cost = sum(cost.get(r, 0) * VALUE[r] for r in RESOURCES)
    if unit_cost <= 0:
        return None
    amount = min(BUILD_CARGO_MAX, item["max_buildable"], int(BUILD_CARGO_SHARE * stock_value(have) // unit_cost))
    return ("small_cargo", amount) if amount >= 1 else None


# ---- fleet saving ---------------------------------------------------------------------------------------------
@dataclass
class Attack:
    id: int
    planet_id: int | None
    coord: tuple[int, int, int]
    origin: tuple[int, int, int]
    arrives_in: float


def incoming_attacks(missions: list[dict]) -> list[Attack]:
    """Foreign attacks on the way, with the planet they are aimed at."""
    found = []
    for m in missions:
        if m.get("is_mine") or m.get("is_return") or m.get("mission") not in ATTACKS:
            continue
        to, src = m.get("to") or {}, m.get("from") or {}
        try:
            found.append(Attack(int(m["id"]), None if to.get("planet_id") is None else int(to["planet_id"]),
                                (int(to.get("galaxy") or 0), int(to.get("system") or 0), int(to.get("position") or 0)),
                                (int(src.get("galaxy") or 0), int(src.get("system") or 0), int(src.get("position") or 0)),
                                float(m.get("seconds_remaining") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def attacks_on(planet: dict, attacks: list[Attack]) -> list[Attack]:
    c = planet.get("coordinates") or {}
    coord = (c.get("galaxy"), c.get("system"), c.get("position"))
    return [a for a in attacks if a.planet_id == planet.get("id") or (a.planet_id is None and a.coord == coord)]


def mobile_ships(units: list[dict]) -> dict[str, int]:
    return {u["machine_name"]: int(u["amount"]) for u in units
            if u.get("category") != "defense" and int(u.get("amount") or 0) > 0 and u["machine_name"] not in IMMOBILE}


def defence_on(units: list[dict]) -> dict[str, int]:
    return {u["machine_name"]: int(u["amount"]) for u in units if u.get("category") == "defense" and int(u.get("amount") or 0) > 0}


def fleet_value(ships: dict) -> int:
    return sum(unit_value(n) * int(k) for n, k in ships.items())


def can_win(ships: dict, defence: dict, attacker_strength: float | None, tech, rng: random.Random) -> tuple[bool, str]:
    """Would the planet (fleet and defence together) hold? The attacker's fleet is not shown, so its strength comes from
    what we last learned about the planet it flies from (a spy report); it is modelled as light fighters of that value."""
    if not attacker_strength:
        return False, "attacker unknown"
    n = max(1, int(attacker_strength * ATTACKER_MARGIN // unit_value("light_fighter")))
    est = estimate_battle({"light_fighter": n}, {**defence, **ships}, rng, tech or (0, 0, 0))
    if est.win_chance <= STAY_IF_ATTACKER_WINS_BELOW:
        return True, f"simulated: attacker wins {est.win_chance:.0%} against {n} light fighters"
    return False, f"simulated: attacker wins {est.win_chance:.0%} against {n} light fighters"


def pick_refuge(origin: dict, planets: list[dict], attacks: list[Attack], attacker: tuple[int, int, int]) -> dict | None:
    """Another own planet that nobody is attacking; the farthest from the attacker, then the nearest to us."""
    def coord(p):
        c = p["coordinates"]
        return (int(c["galaxy"]), int(c["system"]), int(c["position"]))
    options = [p for p in planets if p["id"] != origin["id"] and p.get("type", "planet") == "planet" and not attacks_on(p, attacks)]
    if not options:
        return None
    return max(options, key=lambda p: (distance(coord(p), attacker), -distance(coord(p), coord(origin))))


def flee_cargo(have: dict, capacity: int, fuel: int | None) -> dict[str, int]:
    """Resources to take along: what fits in the hold, deuterium minus the fuel (all of it if the fuel is unknown: none)."""
    stock = {r: int(have.get(r, 0) or 0) for r in RESOURCES}
    stock["deuterium"] = max(0, stock["deuterium"] - (fuel if fuel is not None else stock["deuterium"]))
    return {r: v for r, v in fit_to_capacity(stock, capacity).items() if v > 0}


def danger_until(attacks: list[Attack], now: float) -> float:
    return now + max(a.arrives_in for a in attacks) + DANGER_GRACE


def ready_to_return(entry: dict, now: float, attacks: list[Attack], planets: list[dict]) -> bool:
    """The raid is over (nothing aimed at the home planet or at where the fleet is) and the time has passed."""
    if now < float(entry.get("danger_until", 0)):
        return False
    watched = {entry.get("home_id"), entry.get("at_id")}
    return not any(a.planet_id in watched for a in attacks) and not any(
        attacks_on(p, attacks) for p in planets if p["id"] in watched)


def lost_track(entry: dict, now: float) -> bool:
    return now - float(entry.get("sent_at", now)) > AWAY_GIVE_UP
