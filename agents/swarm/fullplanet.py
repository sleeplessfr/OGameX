"""A planet with no free building fields: how to keep making progress. Pure functions, no network.

Why this module exists. Every building level uses one field and, in this game, so does the terraformer (it gives +5 fields
per level, but its FIRST level still needs a free field, and so do the robot factory levels and the nanite factory that
come before it). A planet that is allowed to fill up completely can therefore never build the terraformer: the way out is
closed from the inside. The agent used to wait for ever ("no free building fields"), with huge stores and no plan.

What a full (or nearly full) planet does instead:

1. Keep a few fields back while the terraformer chain is still reachable (`reserved`): robot factory 10, nanite factory,
   one terraformer level, plus the shipyard 4 / research lab 3 a colony ship needs. Ordinary upgrades may not eat them.
2. When the chain is out of reach (not enough fields left) or the planet is full: found colonies (colonise.py lifts the
   persona's limit), research every useful technology, and turn the surplus into ships and defence.
3. Say what it decided, once an hour (`plan`, logged as `full_planet_plan`).
"""
from __future__ import annotations

# (building, level) needed before the terraformer, in build order: nanite factory also needs computer technology 10.
CHAIN_BUILDINGS = (("robot_factory", 10), ("nano_factory", 1))
TERRAFORMER = "terraformer"
TERRAFORMER_MAX = 30
CHAIN_RESEARCH = (("computer_technology", 10), ("energy_technology", 12))
# What a colony ship needs on the home planet (colonise.LAB_NEEDED / SHIPYARD_NEEDED); kept in reserve so it can always be built.
EXPANSION_BUILDINGS = (("research_lab", 3), ("shipyard", 4))

FULL_PLANET_CAP = 8      # a full planet founds colonies until it owns this many planets (the persona's own limit no longer applies)
FULL_SHARE = 0.4         # share of the spare stock a full planet may turn into ships / defence in one order
FULL_BATCH = 250         # ... and at most this many units per order, so the shipyard is free again soon (colony ship, nanite factory)
PLAN_EVERY = 3600.0      # one full_planet_plan event per planet and hour

# Technologies worth researching on a planet that cannot build anything (weights are relative, like persona.research_weights).
# Graviton technology is left out: it needs an energy amount no planet has.
FULL_RESEARCH = {
    "energy_technology": 1.0, "computer_technology": 1.0, "astrophysics": 1.2, "laser_technology": 0.8, "ion_technology": 0.7,
    "plasma_technology": 0.8, "hyperspace_technology": 0.8, "combustion_drive": 0.6, "impulse_drive": 0.6, "hyperspace_drive": 0.5,
    "espionage_technology": 0.5, "weapon_technology": 0.7, "shielding_technology": 0.7, "armor_technology": 0.7,
    "intergalactic_research_network": 0.4,
}
# Defence a peaceful full planet buys when its personality names none of its own.
DEFENCE_DEFAULTS = {"rocket_launcher": 3.0, "light_laser": 2.0, "heavy_laser": 1.5, "gauss_cannon": 1.0, "ion_cannon": 1.0,
                    "plasma_turret": 0.5, "small_shield_dome": 0.5, "large_shield_dome": 0.5}
COMBAT_UNITS = {"light_fighter", "heavy_fighter", "cruiser", "battleship", "battlecruiser", "bomber", "destroyer", "deathstar",
                "reaper", "pathfinder"} | set(DEFENCE_DEFAULTS)


def levels_of(buildings: list[dict]) -> dict[str, int]:
    return {b["machine_name"]: int(b.get("level") or 0) for b in buildings}


def chain_exists(buildings: list[dict]) -> bool:
    """This game has a nanite factory and a terraformer (older or smaller games may not)."""
    names = {b["machine_name"] for b in buildings}
    return "nano_factory" in names and TERRAFORMER in names


def chain_gaps(buildings: list[dict]) -> dict[str, int]:
    """Fields still needed for the terraformer chain: {building: levels missing}, the next terraformer level included."""
    lv = levels_of(buildings)
    gaps = {name: target - lv.get(name, target) for name, target in CHAIN_BUILDINGS if lv.get(name, target) < target}
    if lv.get(TERRAFORMER, 0) < TERRAFORMER_MAX:
        gaps[TERRAFORMER] = 1
    return gaps


def expansion_gaps(buildings: list[dict]) -> dict[str, int]:
    lv = levels_of(buildings)
    return {name: target - lv[name] for name, target in EXPANSION_BUILDINGS if name in lv and lv[name] < target}


def reserve(buildings: list[dict], left: int | None, alarm: int) -> dict[str, int] | None:
    """The fields to keep back, {building: levels}, or None when nothing is reserved: no field information, plenty of fields
    left, no terraformer chain in this game, or too few fields left for the chain (then the last fields are simply used)."""
    if left is None or left > alarm or left <= 0 or not chain_exists(buildings):
        return None
    chain = chain_gaps(buildings)
    if left < sum(chain.values()):
        return None
    return {**chain, **expansion_gaps(buildings)}


def reachable(buildings: list[dict], left: int | None) -> bool:
    """Can the terraformer chain still be completed with the fields that are free?"""
    return left is not None and left > 0 and chain_exists(buildings) and left >= sum(chain_gaps(buildings).values())


def research_weights(persona_weights: dict[str, float]) -> dict[str, float]:
    """The persona's own taste first; every other useful technology gets a modest base weight."""
    return {**FULL_RESEARCH, **{k: v for k, v in persona_weights.items() if v > 0}}


def spending_weights(unit_weights: dict[str, float]) -> dict[str, float]:
    """Unit weights of a full planet: the persona's own, plus defence when it names no warship or defence at all."""
    if any(w > 0 and n in COMBAT_UNITS for n, w in unit_weights.items()):
        return dict(unit_weights)
    return {**unit_weights, **DEFENCE_DEFAULTS}


def plan(*, left: int | None, buildings: list[dict], research_levels: dict, planets_owned: int, expanding: bool,
         spending: bool, persona_name: str = "") -> dict:
    """What a planet that is full (or close to it) is doing about it. Keys: decision (terraformer path / expanding /
    spending / nothing), reachable, need (fields the chain still needs), left, why."""
    lv = levels_of(buildings)
    gaps = chain_gaps(buildings) if chain_exists(buildings) else {}
    need = sum(gaps.values())
    rl = {k: int(v or 0) for k, v in (research_levels or {}).items()}
    tech = ", ".join(f"{n} {rl.get(n, 0)}/{t}" for n, t in CHAIN_RESEARCH)
    ok = reachable(buildings, left)
    base = {"left": left, "need": need, "reachable": ok}
    if ok:
        steps = ", ".join(f"{n} +{g}" for n, g in gaps.items())
        return {**base, "decision": "terraformer path",
                "why": f"{left} free fields, the chain needs {need} ({steps}); research {tech}; other upgrades may not touch the reserve"}
    if not chain_exists(buildings):
        way = "this game offers no terraformer chain"
    elif left is not None and left > 0:
        way = f"terraformer unreachable: {left} free fields but the chain needs {need}"
    else:
        way = f"terraformer unreachable: no free field (the chain needs {need})"
    blocked = expansion_gaps(buildings)
    can_ship = not blocked and "shipyard" in lv
    parts = []
    if planets_owned < FULL_PLANET_CAP:
        if blocked:
            parts.append("colony blocked: " + ", ".join(f"{n} {lv[n]}/{dict(EXPANSION_BUILDINGS)[n]}" for n in blocked)
                         + " and no field to raise it")
        else:
            parts.append(f"founding colony number {planets_owned} of up to {FULL_PLANET_CAP - 1}" + (" (colony ship under way)" if expanding else ""))
    else:
        parts.append(f"already {planets_owned} planets (cap {FULL_PLANET_CAP})")
    if spending:
        parts.append("surplus goes to research of every useful technology, ships and defence")
    expand_ok = planets_owned < FULL_PLANET_CAP and not blocked
    if expand_ok:
        decision = "expanding"
    elif spending and (can_ship or lv.get("research_lab", 0) > 0):
        decision = "spending"
    else:
        decision = "nothing"
    return {**base, "decision": decision, "why": f"{way}; " + "; ".join(p for p in parts if p)}
