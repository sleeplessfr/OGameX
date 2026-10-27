"""Raid simulator, part 1: resolve ONE raid the way the game does, so a fast simulated world can run thousands of them.

Rules copied from the game source (LootService, BattleEngine::calculateDebris, FleetMissionService):
  * loot: at most 50% of the planet's metal/crystal/deuterium, limited by cargo capacity, shared evenly between the three;
  * debris: 30% of the metal+crystal of every destroyed SHIP (both sides), 0% from defence (server defaults);
  * 70% of destroyed defences are rebuilt after the battle;
  * travel: distance by galaxy/system/position, duration = 35000/percent * sqrt(distance*10/speed) + 10, divided by the war fleet speed.
The fight itself is `combat._fight` (6 rounds, rapid fire, shields), so the simulator and the agents' estimate agree.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import combat
from .combat import DEFENSE, DEFENSE_REPAIR, MAX_UNITS, STATS, Tech

LOOT_SHARE = 0.5
DEBRIS_SHIPS = 0.30
DEBRIS_DEFENSE = 0.0
CARGO = {"small_cargo": 5000, "large_cargo": 25000, "light_fighter": 50, "heavy_fighter": 100, "cruiser": 800,
         "battle_ship": 1500, "battlecruiser": 750, "bomber": 500, "destroyer": 2000, "deathstar": 1000000,
         "reaper": 10000, "pathfinder": 10000, "recycler": 20000, "colony_ship": 7500, "espionage_probe": 5}
SPEED = {"small_cargo": 5000, "large_cargo": 7500, "light_fighter": 12500, "heavy_fighter": 10000, "cruiser": 15000,
         "battle_ship": 10000, "battlecruiser": 10000, "bomber": 4000, "destroyer": 5000, "deathstar": 100,
         "reaper": 7000, "pathfinder": 12000, "recycler": 2000, "colony_ship": 2500, "espionage_probe": 100000}
GALAXIES, SYSTEMS = 9, 499


def distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """a, b = (galaxy, system, position). Donut galaxy and donut system, like the game (empty-system discount ignored)."""
    dg, ds, dp = abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])
    if dg:
        return min(dg, abs(dg - GALAXIES)) * 20000
    if ds:
        return max(min(ds, abs(ds - SYSTEMS)), 1) * 95 + 2700
    if dp:
        return dp * 5 + 1000
    return 5


def travel_seconds(a, b, fleet: dict[str, int], percent: float = 100.0, war_speed: float = 1.0) -> int:
    speeds = [SPEED[n] for n, k in fleet.items() if k > 0 and n in SPEED]
    if not speeds:
        return 0
    return max(1, round((35000 / percent * math.sqrt(distance(a, b) * 10 / min(speeds)) + 10) / war_speed))


def capacity(fleet: dict[str, int]) -> int:
    return sum(CARGO.get(n, 0) * k for n, k in fleet.items() if k > 0)


def split_loot(loot: dict[str, float], cap: int) -> dict[str, float]:
    """The game's even sharing of the cargo hold between metal, crystal and deuterium."""
    keys = ("metal", "crystal", "deuterium")
    if cap >= sum(loot[k] for k in keys):
        return {k: float(loot[k]) for k in keys}
    out = {k: min(loot[k], math.floor(cap / 3)) for k in keys}
    for _ in range(3):
        room = cap - sum(out.values())
        open_ = [k for k in keys if loot[k] > out[k]]
        if room <= 0 or not open_:
            break
        for k in open_:
            out[k] = min(loot[k], out[k] + room / len(open_))
    return out


def _price(name: str) -> tuple[int, int, int]:
    s = STATS[name]
    return s[0], s[1], s[2]


@dataclass
class Raid:
    won: bool
    loot: dict[str, float]
    debris: dict[str, float]
    attacker_left: dict[str, int]
    defender_left: dict[str, int]      # ships + defence still standing (defence after the 70% rebuild)
    attacker_lost_value: float
    defender_lost_value: float


def resolve(attackers: dict[str, int], target_res: dict[str, float], defenders: dict[str, int], rng: random.Random,
            attacker_tech: Tech = (0, 0, 0), defender_tech: Tech = (0, 0, 0)) -> Raid:
    """One real fight, with the full result. `defenders` = ships and defence together."""
    total = sum(attackers.values()) + sum(defenders.values())
    scale = min(1.0, MAX_UNITS / total) if total else 1.0
    att = combat._expand(attackers, attacker_tech, scale)
    dfn = combat._expand(defenders, defender_tech, scale)
    att0, dfn0 = list(att), list(dfn)
    if dfn:
        combat._fight(att, dfn, rng)
    won = bool(att) and not dfn
    debris = {"metal": 0.0, "crystal": 0.0, "deuterium": 0.0}
    a_left = {n: 0 for n in attackers}
    d_left = {n: 0 for n in defenders}
    a_lost = d_lost = 0.0
    for u in att0:
        if u[4] <= 0:
            m, c, d = _price(u[0])
            a_lost += (m + c + d) / scale
            debris["metal"] += m * DEBRIS_SHIPS / scale
            debris["crystal"] += c * DEBRIS_SHIPS / scale
    for u in att:
        a_left[u[0]] += 1
    for u in dfn:
        d_left[u[0]] += 1
    for u in dfn0:
        if u[4] <= 0:
            m, c, d = _price(u[0])
            if u[0] in DEFENSE:
                d_lost += (m + c + d) * (1 - DEFENSE_REPAIR) / scale
                debris["metal"] += m * DEBRIS_DEFENSE / scale
                debris["crystal"] += c * DEBRIS_DEFENSE / scale
            else:
                d_lost += (m + c + d) / scale
                debris["metal"] += m * DEBRIS_SHIPS / scale
                debris["crystal"] += c * DEBRIS_SHIPS / scale
    # back to real numbers: survivors scaled up, destroyed defences partly rebuilt
    a_left = {n: min(attackers[n], round(k / scale)) for n, k in a_left.items()}
    d_final: dict[str, int] = {}
    for n, k in defenders.items():
        alive = min(k, round(d_left[n] / scale))
        if n in DEFENSE:
            alive += round((k - alive) * DEFENSE_REPAIR)
        d_final[n] = alive
    loot = {"metal": 0.0, "crystal": 0.0, "deuterium": 0.0}
    if won:
        cap = capacity({n: k for n, k in a_left.items() if n in CARGO})
        loot = split_loot({k: max(0.0, target_res.get(k, 0.0)) * LOOT_SHARE for k in loot}, cap)
    return Raid(won, loot, debris, a_left, d_final, a_lost, d_lost)
