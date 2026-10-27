"""A genome: the dials an agent may turn on its own personality (plan step 4, see claude/plan-real-learning.md).

A personality (miner, scientist, ...) stays what it is: every dial is limited to a range around the preset, and the military,
social and diplomatic axes are NOT dials at all. What can move is how keen the agent is on mines, power, factories, the lab,
a few technologies, and how patient, balanced and cautious about full stores it is.

    genome = {"metal_mine": 1.3, "patience": +0.05, ...}      (multipliers start at 1.0, additive dials at 0.0)
    persona2 = apply(persona, genome)                       (the persona itself is never changed)

The same genome format is trained offline (train_genome.py, in the economy simulator) and evolved live (evolve.py).
"""
from __future__ import annotations

import math
import random
from dataclasses import replace

# name: (kind, target, low, high)   kind "mult" scales a weight (only if the preset gives it a weight > 0),
#                                  kind "add" shifts a number and clamps it to [low, high] AFTER the shift (see ADD_LIMITS)
MULT_RANGE = (0.4, 2.5)
BUILDING_GENES = ("metal_mine", "crystal_mine", "deuterium_synthesizer", "solar_plant", "robot_factory", "research_lab", "shipyard", "fusion_plant")
RESEARCH_GENES = ("energy_technology", "computer_technology", "astrophysics")
GENES: dict[str, tuple[str, str]] = {
    **{g: ("mult", "building_weights") for g in BUILDING_GENES},
    **{f"r_{g}": ("mult", "research_weights") for g in RESEARCH_GENES},
    "balance": ("mult", "balance"),
    "patience": ("add", "patience"),
    "store_alarm": ("add", "store_alarm"),
}
ADD_RANGE = {"patience": (-0.25, 0.25), "store_alarm": (-0.06, 0.06)}      # how far the dial may move from the preset
ADD_LIMITS = {"patience": (0.2, 0.98), "store_alarm": (0.7, 0.99)}          # what the final number must stay between
BALANCE_RANGE = (0.5, 1.6)


def neutral() -> dict[str, float]:
    return {g: (1.0 if kind == "mult" else 0.0) for g, (kind, _) in GENES.items()}


def clamp(genome: dict) -> dict[str, float]:
    """Only known dials, numbers only, each inside its range. Damaged input becomes the neutral genome."""
    out = neutral()
    if not isinstance(genome, dict):
        return out
    for g, (kind, target) in GENES.items():
        try:
            v = float(genome.get(g, out[g]))
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        if kind == "mult":
            lo, hi = BALANCE_RANGE if g == "balance" else MULT_RANGE
        else:
            lo, hi = ADD_RANGE[g]
        out[g] = round(min(hi, max(lo, v)), 4)
    return out


def apply(persona, genome: dict | None):
    """The persona with the genome's dials turned. A weight of 0 (never build / never research) stays 0."""
    if not genome:
        return persona
    g = clamp(genome)
    bw, rw = dict(persona.building_weights), dict(persona.research_weights)
    for name in BUILDING_GENES:
        if bw.get(name, 0) > 0:
            bw[name] = round(bw[name] * g[name], 4)
    for name in RESEARCH_GENES:
        if rw.get(name, 0) > 0:
            rw[name] = round(rw[name] * g[f"r_{name}"], 4)
    lo_p, hi_p = ADD_LIMITS["patience"]
    lo_s, hi_s = ADD_LIMITS["store_alarm"]
    return replace(persona, building_weights=bw, research_weights=rw,
                   balance=round(persona.balance * g["balance"], 4),
                   patience=round(min(hi_p, max(lo_p, persona.patience + g["patience"])), 4),
                   store_alarm=round(min(hi_s, max(lo_s, persona.store_alarm + g["store_alarm"])), 4))


def mutate(genome: dict, rng: random.Random, sigma: float = 0.15, rate: float = 0.5) -> dict[str, float]:
    """A copy with some dials nudged (multipliers on a log scale, additive dials in proportion to their range)."""
    g = clamp(genome)
    for name, (kind, _) in GENES.items():
        if rng.random() > rate:
            continue
        if kind == "mult":
            g[name] = g[name] * math.exp(rng.gauss(0.0, sigma))
        else:
            lo, hi = ADD_RANGE[name]
            g[name] = g[name] + rng.gauss(0.0, sigma * (hi - lo) / 2)
    return clamp(g)


def crossover(a: dict, b: dict, rng: random.Random) -> dict[str, float]:
    ca, cb = clamp(a), clamp(b)
    return clamp({n: (ca[n] if rng.random() < 0.5 else cb[n]) for n in GENES})


def distance(a: dict, b: dict) -> float:
    """How different two genomes are: mean distance per dial, multipliers on a log scale, additive dials relative to their range."""
    ca, cb = clamp(a), clamp(b)
    total = 0.0
    for name, (kind, _) in GENES.items():
        if kind == "mult":
            total += abs(math.log(ca[name]) - math.log(cb[name]))
        else:
            lo, hi = ADD_RANGE[name]
            total += abs(ca[name] - cb[name]) / (hi - lo)
    return total / len(GENES)


def describe(genome: dict, top: int = 5) -> str:
    """The dials that moved most, for the logs and the report."""
    g = clamp(genome)
    moves = []
    for name, (kind, _) in GENES.items():
        if kind == "mult":
            if abs(math.log(g[name])) >= 0.05:
                moves.append((abs(math.log(g[name])), f"{name} x{g[name]:.2f}"))
        elif abs(g[name]) >= 0.01:
            moves.append((abs(g[name]) / (ADD_RANGE[name][1]), f"{name} {g[name]:+.2f}"))
    moves.sort(reverse=True)
    return ", ".join(m for _, m in moves[:top]) or "unchanged"
