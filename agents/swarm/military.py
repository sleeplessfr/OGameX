"""Military and social decisions of an agent. Pure functions: game data in, a choice out (no network).

The loop an agent follows: find a neighbour -> spy on it -> read the report -> attack if it looks worth it.
Every threshold comes from the persona, so a raider and a turtle act differently on the same facts.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .brain import RESOURCES
from .personas import Persona

FIGHTERS = ("light_fighter", "heavy_fighter", "cruiser", "battle_ship", "battlecruiser", "bomber", "destroyer")
CARGOS = ("small_cargo", "large_cargo")
SYSTEMS_PER_GALAXY = 500  # distance weight of one galaxy jump, in "systems"
LOOT_SHARE = 0.5          # an attack can take at most half of the resources on a planet
SCORE_SLACK = 200         # new players have ~0 points: allow some room around the own score


Coord = tuple[int, int, int]


@dataclass
class Memory:
    """What an agent remembers about others. Times are server timestamps (seconds)."""
    spied: dict[Coord, float] = field(default_factory=dict)      # when we last sent probes there
    attacked: dict[Coord, float] = field(default_factory=dict)   # when we last attacked there
    pending: dict[Coord, float] = field(default_factory=dict)    # probes sent, report not read yet
    intel: dict[Coord, dict] = field(default_factory=dict)       # read reports: {'at', 'loot', 'defenders', 'owner_id'}
    avoid_until: dict[Coord, float] = field(default_factory=dict)  # targets the game refused (protection, vacation, ...)


@dataclass
class Intel:
    loot: int                 # what an attack could take
    defenders: int | None     # ships + defense units seen; None = the report did not show them
    owner_id: int | None


def coord_of(galaxy: int, system: int, position: int) -> Coord:
    return (int(galaxy), int(system), int(position))


def distance(a: Coord, b: Coord) -> int:
    """Rough travel distance between two coordinates, in systems (a galaxy jump counts as a lot)."""
    return abs(a[0] - b[0]) * SYSTEMS_PER_GALAXY + abs(a[1] - b[1]) + (0 if a[:2] != b[:2] else abs(a[2] - b[2]) // 5)


# ---- targets --------------------------------------------------------------------------------------------------
def pick_target(
    persona: Persona, rng: random.Random, me: Coord, my_score: int, players: list[dict], memory: Memory, now: float,
    *, allowed_ids: set[int] | None, cooldown: float, exclude_user_id: int | None = None,
) -> dict | None:
    """Choose a neighbour to spy on. `players` are rows of GET /highscore. `allowed_ids` None = anyone."""
    options = []
    for p in players:
        if p.get("is_me") or p["user_id"] == exclude_user_id:
            continue
        if allowed_ids is not None and p["user_id"] not in allowed_ids:
            continue
        if p["score"] > max(my_score, 1) * persona.max_target_ratio + SCORE_SLACK:
            continue
        c = coord_of(p["main_planet"]["galaxy"], p["main_planet"]["system"], p["main_planet"]["position"])
        if now < memory.avoid_until.get(c, 0) or now - memory.spied.get(c, -1e12) < cooldown:
            continue
        d = distance(me, c)
        if d > persona.target_range:
            continue
        options.append((d, p, c))
    if not options:
        return None
    options.sort(key=lambda t: t[0])
    # Among the few nearest, pick one at random so agents do not all hammer the same neighbour.
    d, p, c = rng.choice(options[:3])
    return {**p, "coord": c, "distance": d}


# ---- reading a spy report -------------------------------------------------------------------------------------
def summarize_report(report: dict) -> Intel:
    """Boil an espionage_report (see docs/API.md) down to what matters for an attack decision."""
    res = report.get("resources") or {}
    total = sum(int(res.get(r, 0) or 0) for r in RESOURCES)
    ships, defense = report.get("ships"), report.get("defense")
    defenders = None
    if ships is not None or defense is not None:
        defenders = sum(int(v or 0) for v in (ships or {}).values()) + sum(int(v or 0) for v in (defense or {}).values())
    return Intel(loot=int(total * LOOT_SHARE), defenders=defenders, owner_id=(report.get("target") or {}).get("owner_id"))


def assess_attack(persona: Persona, rng: random.Random, intel: Intel, my_fighters: int) -> tuple[bool, str]:
    """Decide whether this target is worth attacking with `my_fighters` fighters. Returns (go, reason)."""
    if my_fighters < 1:
        return False, "no fighters"
    if intel.loot < persona.min_loot:
        return False, f"loot {intel.loot} below minimum {persona.min_loot}"
    if intel.defenders is None:
        if rng.random() < persona.gamble:
            return True, "defenses unknown, gambling"
        return False, "defenses unknown"
    if intel.defenders > my_fighters * persona.risk_ratio:
        return False, f"{intel.defenders} defenders vs {my_fighters} fighters is too risky"
    if rng.random() >= persona.aggression:
        return False, "not in the mood"
    return True, f"loot {intel.loot}, {intel.defenders} defenders vs {my_fighters} fighters"


def build_strike(persona: Persona, ships_here: dict[str, int], ship_stats: dict[str, dict], loot: int) -> dict[str, int] | None:
    """Fighters to send plus enough cargo ships to carry `loot`. None if there is nothing to fight with."""
    fleet = {}
    for name in FIGHTERS:
        n = int(ships_here.get(name, 0) * persona.strike_fraction)
        if n > 0:
            fleet[name] = n
    if not fleet:
        return None

    def capacity(name: str) -> int:
        return int(ship_stats.get(name, {}).get("cargo_capacity", 0))

    carried = sum(n * capacity(name) for name, n in fleet.items())
    for name in CARGOS:
        if carried >= loot:
            break
        cap = capacity(name)
        have = int(ships_here.get(name, 0))
        if cap > 0 and have > 0:
            n = min(have, -(-(loot - carried) // cap))  # ceiling division
            fleet[name] = n
            carried += n * cap
    return fleet


def count_fighters(ships_here: dict[str, int]) -> int:
    return sum(int(ships_here.get(n, 0)) for n in FIGHTERS)


def pick_attack(persona: Persona, rng: random.Random, memory: Memory, now: float, ships_here: dict[str, int]) -> tuple[Coord, Intel, str] | None:
    """Look through fresh spy reports for a target worth attacking now."""
    fighters = count_fighters(ships_here)
    best = None
    for c, info in memory.intel.items():
        if now - info["at"] > 2 * 3600 or now - memory.attacked.get(c, -1e12) < persona.attack_cooldown_s:
            continue
        intel = Intel(info["loot"], info["defenders"], info.get("owner_id"))
        go, reason = assess_attack(persona, rng, intel, fighters)
        if go and (best is None or intel.loot > best[1].loot):
            best = (c, intel, reason)
    return best


# ---- what to build --------------------------------------------------------------------------------------------
def choose_units(
    persona: Persona, rng: random.Random, units: list[dict], have: dict, ships_here: dict[str, int],
) -> tuple[str, int] | None:
    """Pick one ship or defense type to order and how many, spending at most `army_share` of what is spare."""
    if persona.army_share <= 0 and persona.probes_wanted <= 0:
        return None
    usable = {u["machine_name"]: u for u in units if u["requirements_met"] and u["character_class_met"] and u["max_buildable"] > 0}

    # Keep a small stock of probes if the persona spies (they are cheap).
    probes = ships_here.get("espionage_probe", 0)
    if probes < persona.probes_wanted and "espionage_probe" in usable:
        return "espionage_probe", min(persona.probes_wanted - probes, usable["espionage_probe"]["max_buildable"])

    if persona.army_share <= 0:
        return None
    weighted = [(persona.unit_weights.get(n, 0) * (1 + persona.randomness * (rng.random() * 2 - 1)), n) for n in usable]
    weighted = [(w, n) for w, n in weighted if w > 0 and n != "espionage_probe"]
    if not weighted:
        return None
    weighted.sort(reverse=True)
    name = weighted[0][1]
    cost = usable[name]["cost_per_unit"]
    limits = [int(have.get(r, 0) * persona.army_share // cost[r]) for r in RESOURCES if cost.get(r, 0) > 0]
    amount = min([usable[name]["max_buildable"], *limits]) if limits else 0
    return (name, amount) if amount >= 1 else None
