#!/usr/bin/env python3
"""Offline economy tuner: plays thousands of planets in a few minutes, without a server, to find better personality weights.

    python tune_economy.py --persona miner                     (search, then print the best weights found)
    python tune_economy.py --persona miner --check             (only measure the current weights)
    python tune_economy.py --persona balanced --hours 24 --speed 100 --generations 20

How it works: the planet below uses the game's own formulas (costs, production, energy, storage, build times; taken from
app/GameObjects/BuildingObjects.php and PlanetService::getBuildingConstructionTime) and the agent's REAL decision
function (swarm.brain.choose_building). The score is the value of one hour of production (metal 1, crystal 1.5,
deuterium 3) after the given number of game hours, averaged over several random seeds. A simple evolution strategy then
nudges the weights and keeps what scores better.

It only touches the means of the economy: the weights of the mines, solar plant and robot factory (never further than
3 times away from the original) and the spare power it keeps. The stores, balance, patience and store alarm (a turtle's
character, for example) are left alone unless you add --with-stores or --with-traits. Research, ships and fighting are left alone, so the result is a
suggestion for those few numbers, not a whole new character. Nothing is written to your files: copy the numbers you like.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import replace

from swarm.brain import VALUE, choose_building
from swarm.personas import PRESETS, Persona

# name: (metal, crystal, deuterium, growth per level)   (app/GameObjects/BuildingObjects.php)
PRICE = {
    "metal_mine": (60, 15, 0, 1.5), "crystal_mine": (48, 24, 0, 1.6), "deuterium_synthesizer": (225, 75, 0, 1.5),
    "solar_plant": (75, 30, 0, 1.5), "robot_factory": (400, 120, 200, 2.0),
    "metal_store": (1000, 0, 0, 2.0), "crystal_store": (1000, 500, 0, 2.0), "deuterium_store": (1000, 1000, 0, 2.0),
}
TUNED = tuple(PRICE)
# What a personality is FOR (how big its stores are, ...) is left alone; only the means of getting there are tuned.
MEANS = ("metal_mine", "crystal_mine", "deuterium_synthesizer", "solar_plant", "robot_factory")
STRETCH = 3.0   # a weight never moves further than this factor away from the original, up or down
TEMPERATURE = 30.0   # average planet temperature; only changes the deuterium output a little


def price(name: str, level: int) -> dict:
    m, c, d, f = PRICE[name]
    return {"metal": int(m * f ** level), "crystal": int(c * f ** level), "deuterium": int(d * f ** level)}


class Planet:
    def __init__(self, speed: float, start: float):
        self.speed = speed
        self.levels = {k: 0 for k in PRICE}
        self.res = {"metal": start, "crystal": start, "deuterium": start}

    def storage(self) -> dict:
        def cap(level):
            return 5000 * math.floor(2.5 * math.exp(20 * level / 33))
        return {"metal": cap(self.levels["metal_store"]), "crystal": cap(self.levels["crystal_store"]),
                "deuterium": cap(self.levels["deuterium_store"])}

    def energy(self) -> tuple[float, float]:
        s = self.levels["solar_plant"]
        make = 20 * s * 1.1 ** s
        m, c, d = (self.levels[k] for k in ("metal_mine", "crystal_mine", "deuterium_synthesizer"))
        need = 10 * m * 1.1 ** m + 10 * c * 1.1 ** c + 20 * d * 1.1 ** d
        return make, need

    def production(self) -> dict:
        make, need = self.energy()
        f = 1.0 if need <= 0 else min(1.0, make / need)
        m, c, d = (self.levels[k] for k in ("metal_mine", "crystal_mine", "deuterium_synthesizer"))
        return {"metal": (30 + 30 * m * 1.1 ** m * f) * self.speed,
                "crystal": (15 + 20 * c * 1.1 ** c * f) * self.speed,
                "deuterium": 10 * d * 1.1 ** d * (1.44 - 0.004 * TEMPERATURE) * f * self.speed}

    def build_seconds(self, name: str) -> float:
        p = price(name, self.levels[name])
        nxt = self.levels[name] + 1
        hours = (p["metal"] + p["crystal"]) / (2500 * max(4 - nxt / 2, 1) * (1 + self.levels["robot_factory"]) * self.speed)
        return max(1.0, int(hours * 3600))

    def view(self) -> tuple[dict, list[dict]]:
        make, need = self.energy()
        planet = {"production_per_hour": self.production(), "energy": int(make - need), "storage": self.storage()}
        items = []
        for name in TUNED:
            cost = price(name, self.levels[name])
            items.append({"machine_name": name, "level": self.levels[name], "cost": cost, "requirements_met": True,
                          "can_afford": all(self.res[r] >= cost[r] for r in cost)})
        return planet, items

    def advance(self, seconds: float) -> None:
        cap = self.storage()
        for r, per_hour in self.production().items():
            self.res[r] = max(self.res[r], min(cap[r], self.res[r] + per_hour * seconds / 3600))   # full stores waste, never destroy stock


def income_value(planet: Planet) -> float:
    return sum(VALUE[r] * v for r, v in planet.production().items())


def play(persona: Persona, seed: int, hours: float, speed: float, start: float) -> float:
    """Average of the production value at half time and at the end (so a late sprint does not win by itself)."""
    rng = random.Random(seed)
    planet = Planet(speed, start)
    t, end, queue = 0.0, hours * 3600.0, None   # queue: (finish time, name)
    half_seen, half_value = False, 0.0
    while t < end:
        if queue and t >= queue[0]:
            planet.levels[queue[1]] += 1
            queue = None
        if not half_seen and t >= end / 2:
            half_seen, half_value = True, income_value(planet)
        if queue:
            dt = max(20.0, queue[0] - t + 2.0)
        else:
            view, items = planet.view()
            choice = choose_building(persona, rng, view, items, research_busy=False, units_busy=False, have=dict(planet.res))
            if choice.machine_name:
                cost = price(choice.machine_name, planet.levels[choice.machine_name])
                for r in cost:
                    planet.res[r] -= cost[r]
                dur = planet.build_seconds(choice.machine_name)
                queue = (t + dur, choice.machine_name)
                dt = max(20.0, dur + 2.0)
            else:
                dt = max(20.0, min(600.0, (choice.wait_seconds or 300.0) + 2.0))
        dt *= rng.uniform(0.85, 1.25)   # the real agents jitter their pauses the same way
        planet.advance(dt)
        t += dt
    if queue and t >= queue[0]:
        planet.levels[queue[1]] += 1
    if not half_seen:
        half_value = income_value(planet)
    return (half_value + income_value(planet)) / 2


def score(persona: Persona, seeds: range, hours: float, speed: float, start: float) -> float:
    return sum(play(persona, s, hours, speed, start) for s in seeds) / len(seeds)


# ---- the search -----------------------------------------------------------------------------------------
def mutate(base: Persona, rng: random.Random, sigma: float, original: Persona, names: tuple[str, ...] = MEANS,
           traits: bool = False) -> Persona:
    weights = dict(base.building_weights)
    for name in names:
        w = weights.get(name, 1.0)
        if w <= 0:
            continue
        home = original.building_weights.get(name, w)
        weights[name] = round(min(home * STRETCH, max(home / STRETCH, w * math.exp(rng.gauss(0, sigma)))), 3)
    out = replace(base, building_weights=weights,
                  energy_buffer=int(min(300, max(0, base.energy_buffer + rng.gauss(0, 15 * sigma / 0.3)))))
    if traits:   # balance, patience and the store alarm are part of the character: only with --with-traits
        out = replace(out,
                      balance=round(min(2.0, max(0.0, base.balance + rng.gauss(0, 0.15 * sigma / 0.3))), 3),
                      patience=round(min(1.0, max(0.0, base.patience + rng.gauss(0, 0.1 * sigma / 0.3))), 3),
                      store_alarm=round(min(0.99, max(0.5, base.store_alarm + rng.gauss(0, 0.04 * sigma / 0.3))), 3))
    return out


def describe(p: Persona) -> str:
    weights = ", ".join(f"{n}: {p.building_weights.get(n, 0):g}" for n in TUNED)
    return (f"building_weights (economy part): {{{weights}}}\n"
            f"balance={p.balance:g}  patience={p.patience:g}  energy_buffer={p.energy_buffer}  store_alarm={p.store_alarm:g}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", default="miner", choices=sorted(PRESETS))
    ap.add_argument("--hours", type=float, default=24.0, help="game hours to play per planet")
    ap.add_argument("--speed", type=float, default=100.0, help="economy speed of the universe (yours is x100)")
    ap.add_argument("--start", type=float, default=100_000.0, help="starting stock of each resource (agents got 100k)")
    ap.add_argument("--generations", type=int, default=15)
    ap.add_argument("--population", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=6, help="random games per candidate")
    ap.add_argument("--check", action="store_true", help="only measure the current weights")
    ap.add_argument("--with-traits", action="store_true", help="also tune balance, patience and store alarm (they are part of the character: off by default)")
    ap.add_argument("--with-stores", action="store_true", help="also tune the store weights (they usually define the character: off by default)")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    base = PRESETS[args.persona]
    train, test = range(100, 100 + args.seeds), range(900, 900 + 3 * args.seeds)   # test games are never used to choose
    start_score = score(base, test, args.hours, args.speed, args.start)
    print(f"{args.persona}: current weights give an hourly production worth {start_score:,.0f} after {args.hours:g} game hours "
          f"(speed x{args.speed:g}, average of {len(test)} games)")
    if args.check:
        return 0

    rng = random.Random(args.seed)
    best, best_score = base, score(base, train, args.hours, args.speed, args.start)
    sigma = 0.3
    for gen in range(1, args.generations + 1):
        improved = False
        for _ in range(args.population):
            cand = mutate(best, rng, sigma, base, TUNED if args.with_stores else MEANS, args.with_traits)
            s = score(cand, train, args.hours, args.speed, args.start)
            if s > best_score:
                best, best_score, improved = cand, s, True
        sigma = max(0.08, sigma * (0.9 if improved else 0.8))
        print(f"  generation {gen}/{args.generations}: best training score {best_score:,.0f}", flush=True)

    final = score(best, test, args.hours, args.speed, args.start)
    gain = (final / start_score - 1) * 100
    print(f"\nOn fresh games the tuned weights give {final:,.0f} against {start_score:,.0f} ({gain:+.1f}%).")
    print(describe(base).replace("(economy part)", "(now)"))
    print(describe(best).replace("(economy part)", "(tuned)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
