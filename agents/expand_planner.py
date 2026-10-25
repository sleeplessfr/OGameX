#!/usr/bin/env python3
"""Which is the best way to get more building fields: terraformer, a colony, or a moon?

    python expand_planner.py
    python expand_planner.py --energy-tech 12 --computer-tech 10 --robot 10 --nano 1 --income 600000,250000,120000
    python expand_planner.py --planets 2 --astro 1

Prices come from the game's own code (app/GameObjects). Resources are added up with the usual 1 : 1.5 : 3 values
(metal : crystal : deuterium). "Hours of income" is how long your hourly production needs to pay for the whole thing,
on one planet. It prints facts to compare and does not decide anything for you.

What the game says (checked in the source):
  * Terraformer: every level adds 5 fields, plus 1 more for every 2 levels. It needs a nanite factory (level 1: robot
    factory 10 and computer technology 10) and energy technology 12, and uses 1,000 energy.
  * Colony: needs astrophysics (one colony per 2 levels, rounded up from level 1), a colony ship, impulse drive 3,
    espionage technology 4 and a research lab 3. The new planet has its own fields (100 to 250 depending on the position)
    but starts with nothing built. Positions 1 and 15 need astrophysics 8, 2 and 14 need 6, 3 and 13 need 4.
  * Moon: only lunar base, sensor phalanx and jump gate can be built there. No mines. A moon does not add economy.
  * The first planet of every player has 163 fields.
"""
from __future__ import annotations

import argparse
import math
import sys

from swarm.brain import VALUE
from swarm.colonise import (ASTRO_FOR_POSITION, FIELD_RANGE, POSITION_BONUS, TEMPERATURE, astro_needed, max_colonies,
                            position_income)

# machine name: (metal, crystal, deuterium, growth per level). Level n costs base * factor ** (n - 1).
PRICES = {
    "terraformer": (0, 50_000, 100_000, 2.0), "nano_factory": (1_000_000, 500_000, 100_000, 2.0),
    "robot_factory": (400, 120, 200, 2.0),
    "energy_technology": (0, 800, 400, 2.0), "computer_technology": (0, 400, 600, 2.0),
    "astrophysics": (4_000, 8_000, 4_000, 1.75), "impulse_drive": (2_000, 4_000, 600, 2.0),
    "espionage_technology": (200, 1_000, 200, 2.0), "research_lab": (200, 400, 200, 2.0),
    "metal_mine": (60, 15, 0, 1.5), "crystal_mine": (48, 24, 0, 1.6), "deuterium_synthesizer": (225, 75, 0, 1.5),
    "solar_plant": (75, 30, 0, 1.5),
}
COLONY_SHIP = (10_000, 20_000, 10_000)
# Position tables and the colony rules live in swarm/colonise.py, so the agents and this calculator agree.


def level_cost(name: str, level: int) -> tuple[float, float, float]:
    m, c, d, f = PRICES[name]
    k = f ** (level - 1)
    return (m * k, c * k, d * k)


def climb(name: str, have: int, want: int) -> tuple[float, float, float]:
    total = [0.0, 0.0, 0.0]
    for lv in range(have + 1, want + 1):
        for i, v in enumerate(level_cost(name, lv)):
            total[i] += v
    return tuple(total)  # type: ignore[return-value]


def add(*costs) -> tuple[float, float, float]:
    return tuple(sum(c[i] for c in costs) for i in range(3))  # type: ignore[return-value]


def value(cost) -> float:
    return cost[0] * VALUE["metal"] + cost[1] * VALUE["crystal"] + cost[2] * VALUE["deuterium"]


def fields_from_terraformer(level: int) -> int:
    return 5 * level + level // 2


def fmt(n: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:,.1f}{unit}"
    return f"{n:,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--income", default="600000,250000,120000", help="hourly production of the planet: metal,crystal,deuterium")
    ap.add_argument("--terraformer", type=int, default=0)
    ap.add_argument("--nano", type=int, default=0)
    ap.add_argument("--robot", type=int, default=0)
    ap.add_argument("--energy-tech", type=int, default=0)
    ap.add_argument("--computer-tech", type=int, default=0)
    ap.add_argument("--astro", type=int, default=0)
    ap.add_argument("--impulse", type=int, default=0)
    ap.add_argument("--espionage", type=int, default=0)
    ap.add_argument("--lab", type=int, default=0)
    ap.add_argument("--planets", type=int, default=1, help="planets you own now (the colony would be one more)")
    ap.add_argument("--levels", type=int, default=3, help="how many terraformer levels to show")
    ap.add_argument("--colony-levels", default="25,22,20,25", help="mines (metal,crystal,deuterium) and solar plant levels you want on the new colony")
    args = ap.parse_args()

    m, c, d = (float(x) for x in args.income.split(","))
    hourly = value((m, c, d))
    print(f"Income of the planet: {fmt(hourly)} per hour (in metal-equivalents).\n")

    # ---- terraformer ----
    prereq = add(climb("robot_factory", args.robot, 10), climb("computer_technology", args.computer_tech, 10),
                 climb("nano_factory", args.nano, 1), climb("energy_technology", args.energy_tech, 12))
    print("TERRAFORMER (each level also needs a free field itself)")
    print(f"  prerequisites still missing: {fmt(value(prereq))}  ({value(prereq) / hourly:,.1f} hours of income)")
    total_fields, total_cost = 0, prereq
    for n in range(1, args.levels + 1):
        lv = args.terraformer + n
        gained = fields_from_terraformer(lv) - fields_from_terraformer(lv - 1)
        step = level_cost("terraformer", lv)
        total_cost = add(total_cost, step)
        total_fields += gained
        print(f"  level {lv}: +{gained} fields for {fmt(value(step))};  cumulative {total_fields} fields for {fmt(value(total_cost))} "
              f"= {fmt(value(total_cost) / total_fields)} per field, {value(total_cost) / hourly:,.1f} hours of income")
    print()

    # ---- colony ----
    need_level = astro_needed(args.planets)
    colony_cost = add(climb("astrophysics", args.astro, need_level) if args.astro < need_level else (0, 0, 0),
                      climb("impulse_drive", args.impulse, 3), climb("espionage_technology", args.espionage, 4),
                      climb("research_lab", args.lab, 3), COLONY_SHIP)
    print(f"COLONY (you own {args.planets} planet{'s' if args.planets != 1 else ''}; astrophysics {need_level} is needed, you have {args.astro})")
    print(f"  ship + research: {fmt(value(colony_cost))}  ({value(colony_cost) / hourly:,.2f} hours of income)")
    best = max(FIELD_RANGE, key=lambda p: sum(FIELD_RANGE[p]))
    for pos in (4, 6, 8, 10, 12):
        lo, hi = FIELD_RANGE[pos]
        print(f"  position {pos}: {lo}-{hi} fields (average {(lo + hi) // 2}) -> {fmt(value(colony_cost) / ((lo + hi) / 2))} per field")
    lm, lc, ld, ls = (int(x) for x in args.colony_levels.split(","))
    ladder = add(climb("metal_mine", 0, lm), climb("crystal_mine", 0, lc), climb("deuterium_synthesizer", 0, ld), climb("solar_plant", 0, ls))
    print(f"  the new planet starts empty: its own mines/solar plant up to {lm}/{lc}/{ld}/{ls} cost {fmt(value(ladder))} "
          f"({value(ladder) / hourly:,.1f} hours of the home planet's income)")
    print(f"  best position for fields: {best} (average {sum(FIELD_RANGE[best]) // 2}); positions 1-3 and 13-15 need more astrophysics\n")

    # ---- where ----
    print("WHERE TO COLONISE (same mines 25/22/20 everywhere; the number is the position in the system)")
    print("  pos  fields(avg)  metal  crystal  temp  deuterium  astro  income index")
    base = position_income(8)
    for pos in range(1, 16):
        lo, hi = FIELD_RANGE[pos]
        bm, bc = POSITION_BONUS.get(pos, (1, 1))
        t = sum(TEMPERATURE[pos]) / 2
        print(f"  {pos:>3}  {(lo + hi) // 2:>10}  x{bm:<5} x{bc:<6} {t:>4.0f}   x{1.44 - 0.004 * t:<7.2f}  {ASTRO_FOR_POSITION.get(pos, 0):>5}  {position_income(pos) / base:>6.0%}")
    print("  (income index: 100% = position 8. Solar plants do not depend on the position; only solar satellites do, with the temperature.)\n")

    # ---- moon ----
    print("MOON (lunar base: +3 fields per level, on the moon only)")
    print("  mines and the solar plant cannot be built on a moon, so a moon does not raise your income. Not an answer to a full planet.\n")

    # ---- summary ----
    t_cost = value(total_cost) / max(1, total_fields)
    avg = 194
    c_total = value(colony_cost) + value(ladder)
    print("IN SHORT")
    print(f"  Terraformer: about {fmt(t_cost)} per field after {args.levels} levels, plus the prerequisites above.")
    print(f"  Colony: about {fmt(value(colony_cost) / avg)} per field for the planet itself, or {fmt(c_total / avg)} per field once its economy is built up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
