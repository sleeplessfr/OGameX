#!/usr/bin/env python3
"""Trains a better genome for each personality inside the economy simulator (step 5 / M1 -> step 4 seed, "B").

    python train_genome.py --persona miner                      (about a minute on a few cores)
    python train_genome.py --persona all --generations 15 --procs 12
    python train_genome.py --persona scientist --hours 96 --out genomes

What it does: a simple evolution strategy plays whole players (swarm/econplayer.py: the agents' real building and research
decisions on the game's formulas) with candidate genomes (swarm/genome.py: bounded dials around the personality), keeps the
best, mutates them, repeats. The best genome is then tested on games it never trained on and compared with the plain
personality on those same games. Result: genomes/<personality>.json, which run_swarm.py --genomes genomes can hand to the agents.

The score is the personality's own goal (research levels for the scientist, hourly production value for everyone else),
averaged over a few checkpoints so a late sprint does not win by itself. Economy only, and as good as the simulator:
trust it as far as validate_econsim.py says it matches the game. Nothing is changed in your running swarm.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from swarm import econplayer, genome as G
from swarm.personas import PRESETS


def score_one(args: tuple) -> float:
    """One simulated game -> the personality's goal averaged over three checkpoints (top level so worker processes can run it)."""
    name, gen, seed, hours = args
    persona = G.apply(PRESETS[name], gen)
    r = econplayer.play(persona, hours=hours, seed=seed)
    marks = [min(len(r.timeline) - 1, int(hours * f)) for f in (1 / 3, 2 / 3, 1.0)]
    key = "research" if name == "scientist" else "worth"
    return statistics.mean(r.timeline[m][key] for m in marks)


class Evaluator:
    def __init__(self, procs: int):
        self.pool = ProcessPoolExecutor(max_workers=procs) if procs > 1 else None

    def scores(self, name: str, genomes: list[dict], seeds: list[int], hours: float) -> list[float]:
        jobs = [(name, g, s, hours) for g in genomes for s in seeds]
        out = list(self.pool.map(score_one, jobs, chunksize=1)) if self.pool else [score_one(j) for j in jobs]
        return [statistics.mean(out[i * len(seeds):(i + 1) * len(seeds)]) for i in range(len(genomes))]

    def close(self) -> None:
        if self.pool:
            self.pool.shutdown()


def train(name: str, ev: Evaluator, *, hours: float, generations: int, population: int, keep: int, seeds: int, rng: random.Random,
          say=lambda text: print(text, flush=True)) -> dict:
    train_seeds = list(range(100, 100 + seeds))
    test_seeds = list(range(900, 900 + 2 * seeds))      # never used to choose
    parents = [G.neutral()]
    best, best_score = G.neutral(), ev.scores(name, [G.neutral()], train_seeds, hours)[0]
    sigma = 0.25
    for gen in range(1, generations + 1):
        children = []
        for _ in range(population):
            a = rng.choice(parents)
            child = G.crossover(a, rng.choice(parents), rng) if rng.random() < 0.25 else a
            children.append(G.mutate(child, rng, sigma=sigma, rate=0.4))
        scores = ev.scores(name, children, train_seeds, hours)
        ranked = sorted(zip(scores, range(len(children))), reverse=True)
        improved = ranked[0][0] > best_score
        if improved:
            best_score, best = ranked[0][0], children[ranked[0][1]]
        parents = [best] + [children[i] for _, i in ranked[:keep - 1]]
        sigma = max(0.06, sigma * (0.92 if improved else 0.8))
        say(f"  {name}: generation {gen}/{generations}: best training score {best_score:,.0f}")
    base_test = ev.scores(name, [G.neutral()], test_seeds, hours)[0]
    best_test = ev.scores(name, [best], test_seeds, hours)[0]
    return {"persona": name, "genome": best, "hours": hours, "generations": generations, "population": population,
            "train_score": best_score, "test_score": best_test, "baseline_test_score": base_test,
            "gain_percent": round((best_test / base_test - 1) * 100, 1) if base_test else 0.0,
            "metric": "research levels" if name == "scientist" else "hourly production value", "trained_at": int(time.time())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", default="all", help="a personality name or 'all' (every personality)")
    ap.add_argument("--hours", type=float, default=72.0, help="game hours per simulated player")
    ap.add_argument("--generations", type=int, default=12)
    ap.add_argument("--population", type=int, default=12)
    ap.add_argument("--keep", type=int, default=3, help="parents kept per generation")
    ap.add_argument("--seeds", type=int, default=4, help="games per candidate (the test uses twice as many fresh ones)")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="worker processes (default: all cores but one)")
    ap.add_argument("--out", default="genomes")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    names = sorted(PRESETS) if args.persona == "all" else [n.strip() for n in args.persona.split(",")]
    bad = [n for n in names if n not in PRESETS]
    if bad:
        print(f"unknown personality: {', '.join(bad)} (known: {', '.join(sorted(PRESETS))})")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ev = Evaluator(args.procs)
    try:
        for name in names:
            t0 = time.time()
            res = train(name, ev, hours=args.hours, generations=args.generations, population=args.population, keep=args.keep,
                        seeds=args.seeds, rng=random.Random(f"{args.seed}:{name}"))
            (out / f"{name}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
            print(f"{name}: on fresh games the trained genome gives {res['test_score']:,.0f} against {res['baseline_test_score']:,.0f} "
                  f"({res['gain_percent']:+.1f}%) for the plain personality [{res['metric']}, {time.time() - t0:.0f} s]\n"
                  f"   changed dials: {G.describe(res['genome'], top=8)}   -> {out / (name + '.json')}", flush=True)
    finally:
        ev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
