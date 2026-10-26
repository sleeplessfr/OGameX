"""Learned building style (plan step 2, see claude/plan-real-learning.md).

The persona's building weights stay the agent's character. On top of them an agent tries a few STYLES, each a small tilt of
those weights (more mines, more research, more factories ...), one per EPOCH (6 server hours). At the end of an epoch the
result is how much the agent's own goal grew: income for everyone except scientists (research levels). A style is judged by
its ADVANTAGE: growth in its epoch minus the average growth of all epochs, so the normal slowing of growth as an agent gets
bigger does not favour whichever style happened to run early. Thompson sampling picks the next style; old results fade.

About one agent in four (targetlearn.is_control) always stays on the plain style but records the same numbers, so the
report can compare learners with that control group. Plain numbers in the agent's memory file; damaged data = defaults.
"""
from __future__ import annotations

import random
from dataclasses import replace

from . import targetlearn

EPOCH = 6 * 3600.0
STYLES: dict[str, dict[str, float]] = {
    "plain": {},
    "mines": {"metal_mine": 1.4, "crystal_mine": 1.4, "deuterium_synthesizer": 1.4},
    "lab": {"research_lab": 1.8, "energy_technology": 1.0},
    "factories": {"robot_factory": 1.6, "nano_factory": 1.6, "shipyard": 1.4},
    "power": {"solar_plant": 1.5, "fusion_plant": 1.8, "metal_store": 1.3, "crystal_store": 1.3, "deuterium_store": 1.3},
}
PRIOR_N = 1.0
SPREAD = 0.25
DECAY = 0.97
VALUE = {"metal": 1.0, "crystal": 1.5, "deuterium": 3.0}


def new_state() -> dict:
    return {"arm": "plain", "since": None, "start": None, "styles": {}, "base": {"n": 0.0, "mean": 0.0}, "epochs": 0}


def clean_state(data) -> dict:
    st = new_state()
    if not isinstance(data, dict):
        return st
    try:
        if data.get("arm") in STYLES:
            st["arm"] = data["arm"]
        for k in ("since", "start"):
            if data.get(k) is not None:
                st[k] = float(data[k])
        st["epochs"] = max(0, int(data.get("epochs", 0)))
        b = data.get("base") or {}
        st["base"] = {"n": float(b.get("n", 0.0)), "mean": float(b.get("mean", 0.0))}
        for k, v in (data.get("styles") or {}).items():
            if k in STYLES and -5 <= float(v["mean"]) <= 5 and 0 <= float(v["n"]) <= 1e6:
                st["styles"][k] = {"n": float(v["n"]), "mean": float(v["mean"])}
    except (TypeError, ValueError, KeyError, AttributeError):
        return new_state()
    return st


def metric(persona_name: str, planets: list[dict], research_levels: dict | None) -> float:
    """The number whose growth is the reward: research levels for a scientist, hourly production value for everyone else."""
    if persona_name == "scientist":
        return float(sum(int(v or 0) for v in (research_levels or {}).values()))
    total = 0.0
    for p in planets:
        prod = p.get("production_per_hour") or {}
        total += sum(float(prod.get(r, 0) or 0) * w for r, w in VALUE.items())
    return total


def tilted(weights: dict[str, float], style: str) -> dict[str, float]:
    tilt = STYLES.get(style) or {}
    return {k: v * tilt.get(k, 1.0) for k, v in weights.items()}


def apply(persona, style: str):
    """The persona with its building weights tilted by `style` (the persona itself is never changed)."""
    if style == "plain" or style not in STYLES:
        return persona
    return replace(persona, building_weights=tilted(persona.building_weights, style))


def _est(row: dict | None) -> tuple[float, float]:
    if not row:
        return 0.0, 0.0
    return row["mean"], row["n"]


def choose(st: dict, rng: random.Random) -> str:
    best, best_s = "plain", -1e9
    for name in STYLES:
        mean, n = _est(st["styles"].get(name))
        est = mean * n / (PRIOR_N + n)
        s = rng.gauss(est, SPREAD / (PRIOR_N + n) ** 0.5)
        if s > best_s:
            best, best_s = name, s
    return best


def record(st: dict, style: str, growth: float) -> float:
    """Add one finished epoch. Returns the advantage that was credited to the style."""
    base_mean, base_n = _est(st["base"])
    adv = growth - base_mean if base_n >= 1 else 0.0
    for row in st["styles"].values():
        row["n"] *= DECAY
    row = st["styles"].setdefault(style, {"n": 0.0, "mean": 0.0})
    row["n"] += 1.0
    row["mean"] += (adv - row["mean"]) / row["n"]
    st["base"]["n"] = base_n + 1.0
    st["base"]["mean"] = base_mean + (growth - base_mean) / st["base"]["n"]
    st["epochs"] += 1
    return adv


def step(st: dict, now: float, value: float, rng: random.Random, control: bool) -> dict | None:
    """Call every round with the current metric. At the end of an epoch records it and picks the next style.
    Returns {style, growth, advantage, next} when an epoch ended, else None."""
    if st["since"] is None or st["start"] is None:
        st["since"], st["start"] = now, max(value, 1e-9)
        return None
    if now - st["since"] < EPOCH:
        return None
    style = st["arm"]
    growth = max(-1.0, min(5.0, value / max(st["start"], 1e-9) - 1.0))
    adv = record(st, style, growth)
    st["arm"] = "plain" if control else choose(st, rng)
    st["since"], st["start"] = now, max(value, 1e-9)
    return {"style": style, "growth": round(growth, 3), "advantage": round(adv, 3), "next": st["arm"]}
