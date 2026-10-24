"""A small battle estimate, so a raider can ask "would I win this?" before it sends its fleet.

It follows the game's rules in simplified form: up to 6 rounds, every unit shoots once per round at a random enemy
unit, shields soak damage and come back each round, rapid fire can give extra shots, units die when their hull is
gone (a unit below 70% hull may explode). Technology bonuses are +10% per level. It is an estimate (random targeting, no ACS, no moon/planet effects),
run a dozen times to get chances instead of one outcome.

Checked against a real report from the owner's server: 13 light fighters against 14 rocket launchers lose
(all fighters died, about half of the launchers).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

# Combat stats copied from the game source (app/GameObjects/*Objects.php). Used only for the battle estimate.
# name: (metal, crystal, deuterium, hull_points, shield, attack, rapidfire dict)
# hull_points is the game's 'structural integrity' as listed; the battle engine uses a tenth of it (checked against a real report).
STATS = {
    "light_fighter": (3000, 1000, 0, 4000, 10, 50, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "heavy_fighter": (6000, 4000, 0, 10000, 25, 150, {"espionage_probe": 5, "solar_satellite": 5, "small_cargo": 3, "crawler": 5}),
    "cruiser": (20000, 7000, 2000, 27000, 50, 400, {"espionage_probe": 5, "solar_satellite": 5, "light_fighter": 6, "rocket_launcher": 10, "crawler": 5}),
    "battle_ship": (45000, 15000, 0, 60000, 200, 1000, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "battlecruiser": (30000, 40000, 15000, 70000, 400, 700, {"espionage_probe": 5, "solar_satellite": 5, "heavy_fighter": 4, "cruiser": 4, "battle_ship": 7, "small_cargo": 3, "large_cargo": 3, "crawler": 5}),
    "bomber": (50000, 25000, 15000, 75000, 500, 1000, {"espionage_probe": 5, "solar_satellite": 5, "rocket_launcher": 20, "light_laser": 20, "heavy_laser": 10, "ion_cannon": 10, "gauss_cannon": 5, "plasma_turret": 5, "crawler": 5}),
    "destroyer": (60000, 50000, 15000, 110000, 500, 2000, {"espionage_probe": 5, "solar_satellite": 5, "light_laser": 10, "battlecruiser": 2, "crawler": 5}),
    "deathstar": (5000000, 4000000, 1000000, 9000000, 50000, 200000, {"espionage_probe": 250, "solar_satellite": 250, "light_fighter": 200, "heavy_fighter": 100, "cruiser": 33, "battle_ship": 30, "bomber": 25, "destroyer": 5, "small_cargo": 250, "large_cargo": 250, "colony_ship": 250, "recycler": 250, "rocket_launcher": 200, "light_laser": 200, "heavy_laser": 100, "ion_cannon": 100, "gauss_cannon": 50, "battlecruiser": 15, "pathfinder": 30, "reaper": 10, "crawler": 250}),
    "reaper": (85000, 55000, 20000, 140000, 700, 2800, {"espionage_probe": 5, "solar_satellite": 5, "light_fighter": 4, "heavy_fighter": 3, "small_cargo": 3, "large_cargo": 3, "crawler": 5}),
    "small_cargo": (2000, 2000, 0, 4000, 10, 5, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "large_cargo": (6000, 6000, 0, 12000, 25, 5, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "colony_ship": (10000, 20000, 10000, 30000, 100, 50, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "recycler": (10000, 6000, 2000, 16000, 10, 1, {"espionage_probe": 5, "solar_satellite": 5, "crawler": 5}),
    "espionage_probe": (0, 1000, 0, 1000, 0, 0, {}),
    "solar_satellite": (0, 2000, 500, 2000, 1, 0, {}),
    "crawler": (2000, 2000, 1000, 4000, 1, 1, {}),
    "pathfinder": (8000, 15000, 8000, 23000, 100, 200, {"espionage_probe": 5, "solar_satellite": 5, "small_cargo": 3, "crawler": 5}),
    "rocket_launcher": (2000, 0, 0, 2000, 20, 80, {}),
    "light_laser": (1500, 500, 0, 2000, 25, 100, {}),
    "heavy_laser": (6000, 2000, 0, 8000, 100, 250, {}),
    "gauss_cannon": (20000, 15000, 2000, 35000, 200, 1100, {}),
    "ion_cannon": (2000, 6000, 0, 8000, 500, 150, {}),
    "plasma_turret": (50000, 50000, 30000, 100000, 300, 3000, {}),
    "small_shield_dome": (10000, 10000, 0, 20000, 2000, 1, {}),
    "large_shield_dome": (50000, 50000, 0, 100000, 10000, 1, {}),
    "anti_ballistic_missile": (8000, 2000, 0, 8000, 1, 1, {}),
    "interplanetary_missile": (12500, 2500, 10000, 15000, 1, 12000, {}),
}
HULL_DIVISOR = 10   # the battle engine uses a tenth of the listed structural integrity
MAX_UNITS = 2500    # bigger fleets are scaled down (both sides equally) to keep the estimate quick
ROUNDS = 6

Tech = tuple[int, int, int]  # (weapons, shields, armour) levels


@dataclass
class Estimate:
    win_chance: float        # share of runs where every defender died and some attacker survived
    my_loss_value: float     # average resources lost (metal + crystal + deuterium of the dead ships)
    their_loss_value: float  # average resources the defender loses
    my_survival: float       # average share of my fleet that survives


def unit_value(name: str) -> int:
    s = STATS.get(name)
    return s[0] + s[1] + s[2] if s else 0


def tech_from_research(research: dict | None) -> Tech | None:
    """(weapons, shields, armour) from a research-levels dict (my status, or the defender's spy report)."""
    if not research:
        return None
    def level(*names):
        for n in names:
            v = research.get(n)
            if isinstance(v, dict):
                v = v.get("level")
            if v is not None:
                return int(v)
        return 0
    return (level("weapon_technology", "weapons_technology"), level("shielding_technology", "shield_technology"),
            level("armor_technology", "armour_technology"))


def _expand(fleet: dict[str, int], tech: Tech, scale: float) -> list[list]:
    """One entry per unit: [name, attack, shield_max, hull_max, hull, shield]."""
    w, sh, ar = tech
    units = []
    for name, n in fleet.items():
        s = STATS.get(name)
        if not s or n <= 0:
            continue
        attack, shield, hull = s[5] * (1 + 0.1 * w), s[4] * (1 + 0.1 * sh), s[3] / HULL_DIVISOR * (1 + 0.1 * ar)
        for _ in range(max(1, int(round(n * scale)))):
            units.append([name, attack, shield, hull, hull, shield])
    return units


def _fight(att: list[list], dfn: list[list], rng: random.Random) -> None:
    for _ in range(ROUNDS):
        if not att or not dfn:
            return
        for u in att + dfn:
            u[5] = u[2]  # shields come back every round
        for shooters, targets in ((att, dfn), (dfn, att)):
            pool = list(targets)  # units that die in this round still shoot back (simultaneous round)
            for shooter in list(shooters):
                if not pool:
                    break
                while True:
                    target = pool[rng.randrange(len(pool))]
                    dmg = shooter[1]
                    if dmg >= 0.01 * target[2]:  # weaker shots bounce off the shield
                        absorbed = min(target[5], dmg)
                        target[5] -= absorbed
                        target[4] -= dmg - absorbed
                        if 0 < target[4] < 0.7 * target[3] and rng.random() < 1 - target[4] / target[3]:
                            target[4] = 0  # badly damaged units can blow up (the game's explosion rule)
                    rf = STATS[shooter[0]][6].get(target[0], 0)
                    if rf <= 1 or rng.random() >= 1 - 1 / rf:
                        break
        att[:] = [u for u in att if u[4] > 0]
        dfn[:] = [u for u in dfn if u[4] > 0]


def estimate_battle(attackers: dict[str, int], defenders: dict[str, int], rng: random.Random,
                    attacker_tech: Tech = (0, 0, 0), defender_tech: Tech | None = None, runs: int = 12) -> Estimate:
    """`defenders` holds ships and defense together. Unknown defender technology is assumed equal to mine."""
    defender_tech = defender_tech if defender_tech is not None else attacker_tech
    total = sum(max(0, n) for n in attackers.values()) + sum(max(0, n) for n in defenders.values())
    scale = min(1.0, MAX_UNITS / total) if total else 1.0
    wins, my_lost, their_lost, survive = 0, 0.0, 0.0, 0.0
    for _ in range(runs):
        att, dfn = _expand(attackers, attacker_tech, scale), _expand(defenders, defender_tech, scale)
        att_start, dfn_start = list(att), list(dfn)
        _fight(att, dfn, rng)
        if att and not dfn:
            wins += 1
        my_lost += sum(unit_value(u[0]) for u in att_start if u[4] <= 0) / scale
        their_lost += sum(unit_value(u[0]) for u in dfn_start if u[4] <= 0) / scale
        survive += len(att) / max(1, len(att_start))
    return Estimate(wins / runs, my_lost / runs, their_lost / runs, survive / runs)
