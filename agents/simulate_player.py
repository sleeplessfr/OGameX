#!/usr/bin/env python3
"""What-if tool: play whole players offline (home planet, colonies, research) and compare personalities or building styles.

    python simulate_player.py                                   (every personality, 72 game hours, 5 seeds)
    python simulate_player.py --personas miner,scientist --hours 168
    python simulate_player.py --styles --personas miner         (the learnable building styles of one personality)
    python simulate_player.py --csv growth.csv                  (hourly numbers of every run, for a chart)

The economy only: no fleets, fights, trade or expeditions. Trust it as far as validate_econsim.py says it matches the game.
`worth` is the value of one hour of production of the whole player (metal 1, crystal 1.5, deuterium 3).
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys

from swarm import buildlearn, econplayer
from swarm.personas import PRESETS


def run_many(persona, hours: float, speed: float, seeds: int):
    return [econplayer.play(persona, hours=hours, speed=speed, seed=s) for s in range(1, seeds + 1)]


def summarise(label: str, runs) -> dict:
    finals = [r.final for r in runs]
    def mean(key):
        return statistics.mean(f[key] for f in finals)
    firsts = [next((e[0] for e in r.events if e[1] == "colony_founded"), None) for r in runs]
    firsts = [f / 3600.0 for f in firsts if f is not None]
    return {"label": label, "worth": mean("worth"), "planets": mean("planets"), "research": mean("research"), "levels": mean("levels"),
            "first_colony_h": statistics.mean(firsts) if firsts else None,
            "spread": statistics.pstdev(f["worth"] for f in finals) / max(1.0, mean("worth"))}


def table(rows: list[dict], hours: float, seeds: int) -> str:
    base = rows[0]["worth"] or 1.0
    out = [f"{'':14s}{'hourly output':>15s}{'vs first':>10s}{'planets':>9s}{'research':>10s}{'1st colony':>12s}   (after {hours:g} game hours, average of {seeds} runs)"]
    for r in rows:
        first = f"{r['first_colony_h']:.1f} h" if r["first_colony_h"] is not None else "never"
        out.append(f"{r['label']:14s}{r['worth']:15,.0f}{(r['worth'] / base - 1) * 100:+9.0f}%{r['planets']:9.1f}{r['research']:10.0f}{first:>12s}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--personas", default=",".join(sorted(PRESETS)))
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--speed", type=float, default=100.0, help="economy speed of the universe (yours is x100)")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--styles", action="store_true", help="compare the building styles the agents can learn (buildlearn.STYLES)")
    ap.add_argument("--csv", help="write the hourly timeline of every run to this file")
    args = ap.parse_args()
    names = [n.strip() for n in args.personas.split(",") if n.strip()]
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        print(f"unknown personality: {', '.join(unknown)} (known: {', '.join(sorted(PRESETS))})")
        return 2
    rows, all_runs = [], []
    for name in names:
        variants = [(f"{name}" if not args.styles else f"{name}/{s}", buildlearn.apply(PRESETS[name], s)) for s in (buildlearn.STYLES if args.styles else ["plain"])]
        for label, persona in variants:
            runs = run_many(persona, args.hours, args.speed, args.seeds)
            rows.append(summarise(label, runs))
            all_runs.append((label, runs))
    print(table(rows, args.hours, args.seeds))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["variant", "seed", "hour", "worth", "planets", "research", "levels", "stock"])
            for label, runs in all_runs:
                for seed, r in enumerate(runs, 1):
                    for snap in r.timeline:
                        w.writerow([label, seed, snap["hour"], round(snap["worth"]), snap["planets"], snap["research"], snap["levels"], round(snap["stock"])])
        print(f"hourly numbers written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
