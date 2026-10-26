"""Founding colonies: when to want one, what to research for it, and where to put it.

Pure functions, no network. The agent (agent.py) does the calls. The numbers come from the game's own code
(see expand_planner.py, which prints the same tables for a human).

Why an agent wants a colony: its home planet is running out of building fields (a terraformer is expensive) and it has
the temperament for it (persona.expansion). Where: free positions near home, scored by what the same mines would
produce there and by how many fields the position tends to have; the position number matters (metal bonus at 6-10,
crystal bonus at 1-3, deuterium falls with temperature).
"""
from __future__ import annotations

import math
import random

from .brain import VALUE, fields_left
from .fullplanet import FULL_PLANET_CAP

EXPAND_FIELDS = 70      # start thinking about a colony when the home planet has this few free fields (or fewer)
SEARCH_SYSTEMS = 6      # look this many systems either side of home
LAB_NEEDED = 3          # research lab level for the colony ship's research
SHIPYARD_NEEDED = 4     # the colony ship is built in a shipyard of this level
SHIP_RESEARCH = (("impulse_drive", 3), ("espionage_technology", 4))
MAX_SYSTEM = 499

# (min fields, max fields) at each position: app/Factories/PlanetServiceFactory.php
FIELD_RANGE = {1: (96, 172), 2: (104, 176), 3: (112, 182), 4: (118, 208), 5: (133, 232), 6: (146, 242), 7: (152, 248),
               8: (156, 252), 9: (150, 246), 10: (142, 232), 11: (136, 210), 12: (125, 186), 13: (114, 172),
               14: (100, 168), 15: (90, 164)}
# Production bonus by position (metal, crystal): PlanetService::getProductionForPositionBonuses
POSITION_BONUS = {1: (1, 1.4), 2: (1, 1.3), 3: (1, 1.2), 6: (1.17, 1), 7: (1.23, 1), 8: (1.35, 1), 9: (1.23, 1), 10: (1.17, 1)}
# (min, max) planet temperature at each position; deuterium output is x(1.44 - 0.004 * temperature)
TEMPERATURE = {1: (220, 260), 2: (170, 210), 3: (120, 160), 4: (70, 110), 5: (60, 100), 6: (50, 90), 7: (40, 80), 8: (30, 70),
               9: (20, 60), 10: (10, 50), 11: (0, 40), 12: (-10, 30), 13: (-50, -10), 14: (-90, -50), 15: (-130, -90)}
# Far positions need astrophysics before the game lets you settle them.
ASTRO_FOR_POSITION = {1: 8, 15: 8, 2: 6, 14: 6, 3: 4, 13: 4}


def max_colonies(astro: int) -> int:
    return int(math.floor(astro / 2 + 0.5))   # the game's round(level / 2)


def astro_needed(planets_owned: int) -> int:
    """Astrophysics level that allows one more colony when we own this many planets (home included)."""
    level = 0
    while max_colonies(level) < planets_owned:
        level += 1
    return level


def max_planets(persona) -> int:
    """How many planets this personality is content with."""
    return 1 + int(round(4 * persona.expansion))


def position_income(position: int, levels=(25, 22, 20)) -> float:
    """Hourly production value (power not limiting) of the same mines at this position."""
    mm, cm, dm = levels
    bm, bc = POSITION_BONUS.get(position, (1, 1))
    t = sum(TEMPERATURE[position]) / 2
    metal = (30 + 30 * mm * 1.1 ** mm) * bm
    crystal = (15 + 20 * cm * 1.1 ** cm) * bc
    deut = 10 * dm * 1.1 ** dm * (1.44 - 0.004 * t)
    return metal * VALUE["metal"] + crystal * VALUE["crystal"] + deut * VALUE["deuterium"]


def wants_colony(persona, home: dict, planets_owned: int, rng: random.Random) -> bool:
    """Decided once, when the home planet gets crowded; the agent then keeps the goal until it is done."""
    if persona.expansion <= 0 or planets_owned >= max_planets(persona):
        return False
    left = fields_left(home)
    if left is None or left > EXPAND_FIELDS:
        return False
    return rng.random() < persona.expansion


def wants_colony_full(home: dict, planets_owned: int) -> bool:
    """A home planet with no free field wants colonies whatever the persona's appetite: it has nothing else to grow with.
    No dice and no persona limit (max_planets); only FULL_PLANET_CAP stops it."""
    left = fields_left(home)
    return left is not None and left <= 0 and planets_owned < FULL_PLANET_CAP


def goals(planets_owned: int, research_levels: dict) -> tuple[dict, dict]:
    """(buildings, research) levels the home planet must reach before a colony ship can be built and used."""
    research = {name: level for name, level in SHIP_RESEARCH}
    research["astrophysics"] = astro_needed(planets_owned)
    for name in list(research):
        if int(research_levels.get(name, 0) or 0) >= research[name]:
            del research[name]
    return {"research_lab": LAB_NEEDED, "shipyard": SHIPYARD_NEEDED}, research


def pick_target(persona, rng: random.Random, home: tuple[int, int, int], systems: list[dict], astro: int):
    """Best free position among the given galaxy views. Returns ((g, s, p), explanation) or None."""
    base = position_income(8)
    best = None
    for view in systems:
        g, s = int(view["galaxy"]), int(view["system"])
        for slot in view.get("positions", []):
            pos = int(slot["position"])
            if slot.get("planet") is not None or pos not in FIELD_RANGE or ASTRO_FOR_POSITION.get(pos, 0) > astro:
                continue
            fields = sum(FIELD_RANGE[pos]) / 2
            income = position_income(pos) / base
            near = 1.0 / (1.0 + abs(s - home[1]) / 10.0)       # a close colony is quick to reach and to defend
            score = math.sqrt(income * fields / 200.0) * near * rng.uniform(0.85, 1.15)
            if best is None or score > best[0]:
                best = (score, (g, s, pos), f"position {pos}: about {int(fields)} fields, mines worth {income:.0%} of position 8")
    return None if best is None else (best[1], best[2])


def systems_to_scan(home: tuple[int, int, int], radius: int = SEARCH_SYSTEMS) -> list[tuple[int, int]]:
    g, s, _ = home
    return [(g, x) for x in range(max(1, s - radius), min(MAX_SYSTEM, s + radius) + 1)]
