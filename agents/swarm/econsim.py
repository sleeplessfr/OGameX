"""Economy simulator, step 5 / milestone 1 (see claude/plan-step5-fast-simulator.md).

The game's own formulas in plain Python, so a whole player (many planets, research, colonies) can be played in milliseconds:
prices, construction and research times, mine output with energy, planet-position bonus, plasma technology and the
collector class, storage. Price tables come from the game's PHP source (econdata.py, made by gen_econdata.py); the few
production formulas below are copied from app/GameObjects/BuildingObjects.php, GameObjectProduction.php and
PlanetService.php and are checked by validate_econsim.py against what the real server reported.

Left out on purpose (they are not used by the agents): officers, items, crawlers, the intergalactic research network,
vacation mode. A character class changes mines (collector +25%), energy (collector +10%) and research time (discoverer -25%).
"""
from __future__ import annotations

import math

from .econdata import OBJECTS

POSITION_BONUS = {   # PlanetService::getProductionForPositionBonuses: (metal, crystal, deuterium) multipliers by planet position
    1: (1, 1.4, 1), 2: (1, 1.3, 1), 3: (1, 1.2, 1),
    6: (1.17, 1, 1), 7: (1.23, 1, 1), 8: (1.35, 1, 1), 9: (1.23, 1, 1), 10: (1.17, 1, 1),
}
BASIC_INCOME = (30.0, 15.0, 0.0)   # per hour, times the economy speed and the position bonus (SettingsService basic income)
MINES = ("metal_mine", "crystal_mine", "deuterium_synthesizer")
RESOURCES = ("metal", "crystal", "deuterium")
VALUE = {"metal": 1.0, "crystal": 1.5, "deuterium": 3.0}   # the agents' yardstick for the worth of an hour of production


def price(name: str, level: int) -> dict[str, int]:
    """Cost of building/researching `name` UP TO `level` (so the cost of the upgrade from level-1)."""
    o = OBJECTS[name]
    if level <= 0:
        return {r: 0 for r in RESOURCES}
    out = {}
    for r in RESOURCES:
        v = math.floor(o[r] * o["factor"] ** (level - 1))
        if o["round100"]:
            v = round(v / 100) * 100
        out[r] = int(v)
    return out


def energy_price(name: str, level: int) -> int:
    o = OBJECTS[name]
    return int(math.floor(o["energy"] * o["factor"] ** (level - 1))) if level > 0 else 0


def build_seconds(name: str, next_level: int, robot: int, nano: int, speed: float) -> int:
    """PlanetService::getBuildingConstructionTime."""
    p = price(name, next_level)
    base = p["metal"] + p["crystal"]
    if name == "nano_factory":
        hours = base / (2500 * (1 + robot) * speed * 2 ** nano)
    else:
        hours = base / (2500 * max(4 - next_level / 2, 1) * (1 + robot) * speed * 2 ** nano)
    return max(1, int(hours * 3600))


def research_seconds(name: str, next_level: int, lab: int, speed: float, research_speed: float = 1.0, discoverer: bool = False) -> int:
    """PlanetService::getTechnologyResearchTime (without the intergalactic research network)."""
    p = price(name, next_level)
    hours = (p["metal"] + p["crystal"]) / (1000 * (1 + lab) * speed * research_speed)
    seconds = int(hours * 3600)
    if discoverer:
        seconds = int(seconds * 0.75)
    return max(1, seconds)


def storage_cap(level: int) -> int:
    return int(5000 * math.floor(2.5 * math.exp(20 * level / 33)))


def storage(levels: dict[str, int]) -> dict[str, int]:
    return {"metal": storage_cap(levels.get("metal_store", 0)), "crystal": storage_cap(levels.get("crystal_store", 0)),
            "deuterium": storage_cap(levels.get("deuterium_store", 0))}


def energy(levels: dict[str, int], energy_tech: int, collector: bool = False) -> tuple[float, float]:
    """(energy made, energy needed). The fusion plant's energy counts only while it has deuterium (see production)."""
    s = levels.get("solar_plant", 0)
    make = math.floor(20 * s * 1.1 ** s)
    f = levels.get("fusion_plant", 0)
    make += math.floor(30 * f * (1.05 + energy_tech * 0.01) ** f)
    if collector:
        make = math.floor(make * 1.10)
    m, c, d = (levels.get(k, 0) for k in MINES)
    need = math.floor(10 * m * 1.1 ** m) + math.floor(10 * c * 1.1 ** c) + math.floor(20 * d * 1.1 ** d)
    return float(make), float(need)


def production_factor(make: float, need: float) -> float:
    if need <= 0:
        return 1.0
    return min(100, max(0, math.floor(make / need * 100))) / 100.0


def production(levels: dict[str, int], position: int, temp_avg: float, speed: float, energy_tech: int = 0, plasma: int = 0,
               collector: bool = False) -> dict[str, float]:
    """Hourly output of one planet, the way PlanetService::updateResourceProductionStatsInner adds it up."""
    make, need = energy(levels, energy_tech, collector)
    factor = production_factor(make, need)
    bonus = POSITION_BONUS.get(position, (1, 1, 1))
    basic = [BASIC_INCOME[i] * speed * bonus[i] for i in range(3)]
    m, c, d = (levels.get(k, 0) for k in MINES)
    f = levels.get("fusion_plant", 0)
    raw = [30 * m * 1.1 ** m, 20 * c * 1.1 ** c, 10 * d * 1.1 ** d * (1.44 - 0.004 * temp_avg)]
    out = []
    for i in range(3):
        mine = raw[i] * speed                      # (building percentage is always 100 % for the agents)
        slot = math.floor(mine * (bonus[i] - 1))
        plasma_pct = (0.01, 0.0066, 0.0033)[i] * plasma
        plasma_bonus = math.floor((mine + slot) * plasma_pct) if mine > 0 else 0
        cls = math.floor((mine + slot) * 0.25) if collector and mine > 0 else 0
        total = mine + slot + plasma_bonus + cls
        out.append(total * factor)
    out[2] -= 10 * f * 1.1 ** f * speed * factor   # the fusion plant burns deuterium (cut by a power shortage like the mines)
    return {"metal": basic[0] + out[0], "crystal": basic[1] + out[1], "deuterium": basic[2] + out[2]}


def worth(prod: dict[str, float]) -> float:
    return sum(VALUE[r] * prod.get(r, 0.0) for r in RESOURCES)


def fields_max(base_fields: int, terraformer: int, lunar_base: int = 0) -> int:
    """PlanetService::getPlanetFieldMax: terraformer +5 per level and +1 per two levels."""
    return base_fields + terraformer * 5 + terraformer // 2 + lunar_base * 3
