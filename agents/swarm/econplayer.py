"""A whole player's economy, played offline in seconds (step 5 / milestone 1, part B; see claude/plan-step5-fast-simulator.md).

It is the real agent brain on top of the game's formulas (econsim.py): the home planet and its colonies each build with
`brain.choose_building`, the player researches with `brain.choose_research`, and the colony chain of the agents runs as it
does live (wants a colony when the home planet is crowded, researches astrophysics, impulse drive and espionage, builds
a research lab 3 and a shipyard 4, buys a colony ship, sends it, a new planet appears). Fleets, fighting, trading and
expeditions are NOT simulated: this is the economy and nothing else.

    play(persona, hours=72, speed=100, seed=1) -> Result with an hourly timeline

Ground truth for the formulas is `validate_econsim.py`; this module adds only the player loop.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import colonise, econsim
from .brain import FIELD_ALARM, choose_building, choose_research, fields_left
from .econdata import OBJECTS
from .fullplanet import FULL_PLANET_CAP

NOT_ON_PLANETS = {"lunar_base", "sensor_phalanx", "jump_gate"}           # moon-only buildings
BUILDINGS = [n for n, o in OBJECTS.items() if o["kind"] in ("building", "station") and n not in NOT_ON_PLANETS]
TECHS = [n for n, o in OBJECTS.items() if o["kind"] == "research"]
HOME_FIELDS = 163
HOME_POSITION = 8
COLONY_POSITIONS = (7, 8, 9, 6, 10)       # where the agents usually settle (the report's colonies: 7-11)
COLONY_FLIGHT_SECONDS = 1200.0
START_STOCK = 100_000.0                    # agents start with this much of each resource on a x100 server
MIN_LOOK, MAX_LOOK = 20.0, 600.0


@dataclass
class SimPlanet:
    position: int
    base_fields: int
    temp_avg: float
    res: dict = field(default_factory=lambda: {r: START_STOCK for r in econsim.RESOURCES})
    levels: dict = field(default_factory=dict)
    queue: tuple | None = None          # (finish time, name, level)
    next_look: float = 0.0

    def used(self) -> int:
        return sum(v for k, v in self.levels.items() if k != "terraformer")

    def max_fields(self) -> int:
        return econsim.fields_max(self.base_fields, self.levels.get("terraformer", 0))


@dataclass
class Result:
    timeline: list[dict]
    final: dict
    events: list[tuple]


class Player:
    def __init__(self, persona, speed: float = 100.0, seed: int = 1, start: float = START_STOCK, home_position: int = HOME_POSITION):
        self.persona, self.speed, self.rng = persona, speed, random.Random(seed)
        self.collector = persona.character_class == "collector"
        self.discoverer = persona.character_class == "discoverer"
        home = SimPlanet(home_position, HOME_FIELDS, _temp(home_position))
        home.res = {r: start for r in econsim.RESOURCES}
        self.planets: list[SimPlanet] = [home]
        self.research: dict[str, int] = {}
        self.rq: tuple | None = None
        self.research_look = 0.0
        self.expanding = False
        self.colony_state = "idle"       # idle -> building -> flying
        self.colony_at = 0.0
        self.colony_until = 0.0
        self.events: list[tuple] = []
        self.t = 0.0

    # ---- views the brain expects ---------------------------------------------------------------------
    def _prod(self, p: SimPlanet) -> dict:
        return econsim.production(p.levels, p.position, p.temp_avg, self.speed, self.research.get("energy_technology", 0),
                                  self.research.get("plasma_technology", 0), self.collector)

    def _view(self, p: SimPlanet) -> dict:
        make, need = econsim.energy(p.levels, self.research.get("energy_technology", 0), self.collector)
        return {"production_per_hour": self._prod(p), "energy": int(make - need), "storage": econsim.storage(p.levels),
                "fields": {"used": p.used(), "max": p.max_fields()}, "coordinates": {"galaxy": 1, "system": 1, "position": p.position}}

    def _met(self, requires: dict, p: SimPlanet | None) -> bool:
        for name, lv in requires.items():
            have = self.research.get(name, 0) if OBJECTS[name]["kind"] == "research" else (p.levels.get(name, 0) if p else 0)
            if have < lv:
                return False
        return True

    def _items(self, p: SimPlanet) -> list[dict]:
        make, need = econsim.energy(p.levels, self.research.get("energy_technology", 0), self.collector)
        items = []
        for name in BUILDINGS:
            lvl = p.levels.get(name, 0)
            cost = econsim.price(name, lvl + 1)
            ok_energy = name != "terraformer" or (make - need) >= econsim.energy_price(name, lvl + 1)
            items.append({"machine_name": name, "level": lvl, "next_level": lvl + 1, "cost": cost,
                          "requirements_met": self._met(OBJECTS[name]["requires"], p),
                          "can_afford": all(p.res[r] >= cost[r] for r in cost) and ok_energy})
        return items

    def _tech_items(self, home: SimPlanet) -> list[dict]:
        items = []
        for name in TECHS:
            lvl = self.research.get(name, 0)
            cost = econsim.price(name, lvl + 1)
            items.append({"machine_name": name, "level": lvl, "next_level": lvl + 1, "cost": cost,
                          "requirements_met": self._met(OBJECTS[name]["requires"], home),
                          "can_afford": all(home.res[r] >= cost[r] for r in cost)})
        return items

    # ---- the clock -----------------------------------------------------------------------------------
    def _advance(self, to: float) -> None:
        dt = to - self.t
        if dt <= 0:
            return
        for p in self.planets:
            cap = econsim.storage(p.levels)
            for r, per_hour in self._prod(p).items():
                now = p.res[r] + per_hour * dt / 3600.0
                p.res[r] = max(0.0, now) if now < cap[r] else max(p.res[r], cap[r])   # full stores waste, never destroy stock
        self.t = to

    def _finish_due(self) -> None:
        for p in self.planets:
            if p.queue and self.t >= p.queue[0]:
                p.levels[p.queue[1]] = p.queue[2]
                p.queue = None
                p.next_look = self.t
        if self.rq and self.t >= self.rq[0]:
            self.research[self.rq[1]] = self.rq[2]
            self.rq = None
            self.research_look = self.t

    def _jitter(self, seconds: float) -> float:
        return max(MIN_LOOK, min(MAX_LOOK, seconds)) * self.rng.uniform(0.85, 1.25)

    # ---- decisions -----------------------------------------------------------------------------------
    def _colony_goals(self, home: SimPlanet) -> tuple[dict | None, dict | None]:
        p = self.persona
        n = len(self.planets)
        full = fields_left(self._view(home)) is not None and fields_left(self._view(home)) <= 0 or any(
            fields_left(self._view(q)) is not None and fields_left(self._view(q)) <= 0 for q in self.planets)
        if self.colony_state != "idle" or (p.expansion <= 0 and not full):
            return (None, None) if self.colony_state == "idle" else self._goal_pair(n)
        if not self.expanding:
            if self.t < self.colony_until:
                return None, None
            if full:
                if n >= FULL_PLANET_CAP:
                    return None, None
            elif not colonise.wants_colony(p, self._view(home), n, self.rng):
                return None, None
            self.expanding = True
            self.events.append((self.t, "expand", n))
        return self._goal_pair(n)

    def _goal_pair(self, n: int) -> tuple[dict | None, dict | None]:
        if not self.expanding:
            return None, None
        b, r = colonise.goals(n, self.research)
        return b, r

    def _plan_colony(self, home: SimPlanet, goals_r: dict | None) -> None:
        if not self.expanding or goals_r:
            return
        if self.colony_state == "idle":
            if home.levels.get("research_lab", 0) < colonise.LAB_NEEDED or home.levels.get("shipyard", 0) < colonise.SHIPYARD_NEEDED:
                return
            cost = {"metal": 10_000, "crystal": 20_000, "deuterium": 10_000}
            if all(home.res[r] >= cost[r] for r in cost):
                for r in cost:
                    home.res[r] -= cost[r]
                hours = (cost["metal"] + cost["crystal"]) / (2500 * (1 + home.levels["shipyard"]) * self.speed * 2 ** home.levels.get("nano_factory", 0))
                self.colony_state, self.colony_at = "building", self.t + max(1.0, hours * 3600)
        elif self.colony_state == "building" and self.t >= self.colony_at:
            self.colony_state, self.colony_at = "flying", self.t + COLONY_FLIGHT_SECONDS
            self.events.append((self.t, "colony_sent", len(self.planets)))
        elif self.colony_state == "flying" and self.t >= self.colony_at:
            pos = COLONY_POSITIONS[(len(self.planets) - 1) % len(COLONY_POSITIONS)]
            lo, hi = colonise.FIELD_RANGE[pos]
            self.planets.append(SimPlanet(pos, (lo + hi) // 2, _temp(pos), res={r: 0.0 for r in econsim.RESOURCES}))
            self.expanding, self.colony_state = False, "idle"
            self.colony_until = self.t + 6 * 3600.0
            self.events.append((self.t, "colony_founded", len(self.planets)))

    def _look_planet(self, p: SimPlanet, goals_b: dict | None) -> None:
        if p.queue:
            return
        choice = choose_building(self.persona, self.rng, self._view(p), self._items(p), research_busy=bool(self.rq), units_busy=False,
                                 have=dict(p.res), goals=goals_b if p is self.planets[0] else None)
        if choice.machine_name:
            level = p.levels.get(choice.machine_name, 0) + 1
            cost = econsim.price(choice.machine_name, level)
            for r in cost:
                p.res[r] -= cost[r]
            secs = econsim.build_seconds(choice.machine_name, level, p.levels.get("robot_factory", 0), p.levels.get("nano_factory", 0), self.speed)
            p.queue = (self.t + secs, choice.machine_name, level)
            p.next_look = self.t + secs + 2.0
        else:
            p.next_look = self.t + self._jitter(choice.wait_seconds or 300.0)

    def _look_research(self, goals_r: dict | None) -> None:
        home = self.planets[0]
        if self.rq:
            return
        p = self.persona
        left = fields_left(self._view(home))
        urgent = left is not None and left <= FIELD_ALARM
        if self.rng.random() > p.research_share and not goals_r and not urgent:
            self.research_look = self.t + self._jitter(300.0)
            return
        if not home.levels.get("research_lab") or (home.queue and home.queue[1] == "research_lab"):
            self.research_look = self.t + self._jitter(300.0)
            return
        choice = choose_research(p, self.rng, self._view(home), self._tech_items(home), have=dict(home.res), goals=goals_r)
        if choice.machine_name:
            level = self.research.get(choice.machine_name, 0) + 1
            cost = econsim.price(choice.machine_name, level)
            for r in cost:
                home.res[r] -= cost[r]
            secs = econsim.research_seconds(choice.machine_name, level, home.levels["research_lab"], self.speed, discoverer=self.discoverer)
            self.rq = (self.t + secs, choice.machine_name, level)
            self.research_look = self.t + secs + 2.0
        else:
            self.research_look = self.t + self._jitter(choice.wait_seconds or 300.0)

    # ---- one run -------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        prod = {r: 0.0 for r in econsim.RESOURCES}
        for p in self.planets:
            for r, v in self._prod(p).items():
                prod[r] += v
        return {"hour": round(self.t / 3600.0, 2), "worth": econsim.worth(prod), "planets": len(self.planets),
                "research": sum(self.research.values()), "levels": sum(sum(p.levels.values()) for p in self.planets),
                "stock": sum(econsim.VALUE[r] * p.res[r] for p in self.planets for r in econsim.RESOURCES)}

    def run(self, hours: float) -> Result:
        end = hours * 3600.0
        timeline, next_sample = [], 0.0
        while self.t < end:
            while self.t >= next_sample:
                timeline.append(self.snapshot())
                next_sample += 3600.0
            self._finish_due()
            home = self.planets[0]
            goals_b, goals_r = self._colony_goals(home)
            self._plan_colony(home, goals_r)
            for p in self.planets:
                if self.t >= p.next_look:
                    self._look_planet(p, goals_b)
            if self.t >= self.research_look:
                self._look_research(goals_r)
            wake = [end, next_sample] + [p.next_look for p in self.planets] + [self.research_look]
            wake += [p.queue[0] for p in self.planets if p.queue] + ([self.rq[0]] if self.rq else [])
            if self.colony_state in ("building", "flying"):
                wake.append(self.colony_at)
            self._advance(min(max(self.t + 1.0, min(w for w in wake if w > self.t)), end))
        self._finish_due()
        timeline.append(self.snapshot())
        return Result(timeline, self.snapshot(), self.events)


def _temp(position: int) -> float:
    lo, hi = colonise.TEMPERATURE[position]
    return (lo + hi) / 2 - 20


def play(persona, hours: float = 72.0, speed: float = 100.0, seed: int = 1, start: float = START_STOCK) -> Result:
    return Player(persona, speed, seed, start).run(hours)
