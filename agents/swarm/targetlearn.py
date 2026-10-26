"""Learned raid targeting (plan step 1, see claude/plan-real-learning.md).

A raider groups every possible target into a coarse CLASS (how much loot, how big the garrison, whose intel) and learns
from real results what each class pays. The reward of an attack is the share of the announced loot it really netted:

    ratio = (loot taken + half of what we destroyed - what we lost) / max(announced loot, 10,000)     (kept between -2 and 1)

Thompson sampling: for every candidate the agent draws a plausible ratio from what it knows about that class (few results =
wide guess, many = narrow) and ranks candidates by loot * drawn ratio. A class that keeps losing is dropped (VETO). Old
results fade (DECAY), so a changing universe is followed. About a quarter of the agents (CONTROL) keep the old rule-only
ranking but still record results, so the report can compare learners with the control group.

Plain numbers saved in the agent's memory file; damaged data falls back to an empty book.
"""
from __future__ import annotations

import os
import random
import zlib

PRIOR_MEAN = 0.35      # before any result, a class is expected to net about a third of the announced loot
PRIOR_N = 2.0          # ... worth two results
SPREAD = 0.6           # how much results of one class differ from each other
DECAY = 0.98           # every result pulls all classes a little back to the prior
VETO_MIN_N = 5.0       # a class is dropped only after this much evidence ...
VETO_BELOW = -0.15     # ... that on average it lost money
FACTOR_RANGE = (0.2, 3.0)
CONTROL_SHARE = 4      # one agent in four is a control (rule-only ranking)
MIN_LOOT_BASE = 10_000


def enabled() -> bool:
    return os.environ.get("OGAMEX_LEARN", "on").lower() != "off"


def is_control(agent_name: str) -> bool:
    """Stable: the same agent is always a control or always a learner."""
    return zlib.crc32(str(agent_name).encode()) % CONTROL_SHARE == 0


def group_of(agent_name: str) -> str:
    return "off" if not enabled() else "control" if is_control(agent_name) else "learned"


def key_of(loot: int, defenders, from_ally: bool) -> str:
    """The class of a target: loot size, garrison size and whether the report is our own or an ally's."""
    loot = int(loot or 0)
    lt = "L0" if loot < 50_000 else "L1" if loot < 200_000 else "L2" if loot < 1_000_000 else "L3"
    if defenders is None:
        gt = "G?"
    else:
        d = int(defenders)
        gt = "G0" if d == 0 else "G1" if d <= 20 else "G2" if d <= 80 else "G3"
    return f"{lt}{gt}{'a' if from_ally else 'o'}"


def new_book() -> dict:
    return {}


def clean_book(data) -> dict:
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        try:
            n, mean = float(v["n"]), float(v["mean"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(k, str) and 0 <= n <= 1e6 and -5 <= mean <= 5:
            out[k] = {"n": n, "mean": mean}
    return out


def _stat(book: dict, key: str) -> tuple[float, float]:
    row = book.get(key)
    if not row:
        return PRIOR_MEAN, 0.0
    return row["mean"], row["n"]


def expected(book: dict, key: str) -> float:
    """The average ratio learned for this class (the prior until there are results)."""
    n_mean, n = _stat(book, key)
    return (PRIOR_MEAN * PRIOR_N + n_mean * n) / (PRIOR_N + n)


def sample(book: dict, key: str, rng: random.Random) -> float:
    mean, n = _stat(book, key)
    est = (PRIOR_MEAN * PRIOR_N + mean * n) / (PRIOR_N + n)
    return rng.gauss(est, SPREAD / (PRIOR_N + n) ** 0.5)


def factor(book: dict, key: str, rng: random.Random) -> float:
    """What to multiply a target's worth with: its drawn ratio relative to the prior, kept in a sane range."""
    lo, hi = FACTOR_RANGE
    return max(lo, min(hi, sample(book, key, rng) / PRIOR_MEAN))


def vetoed(book: dict, key: str) -> bool:
    mean, n = _stat(book, key)
    return n >= VETO_MIN_N and mean < VETO_BELOW


def ratio_of(loot_taken: int, their_loss: int, my_loss: int, announced_loot: int) -> float:
    r = (int(loot_taken) + 0.5 * int(their_loss) - int(my_loss)) / max(int(announced_loot), MIN_LOOT_BASE)
    return max(-2.0, min(1.0, r))


def update(book: dict, key: str, ratio: float) -> None:
    """Record one result: fade everything towards the prior a little, then add this one to its class."""
    for row in book.values():
        row["n"] = row["n"] * DECAY      # less evidence: the estimate slides back towards the prior
    row = book.setdefault(key, {"n": 0.0, "mean": 0.0})
    n = row["n"] + 1.0
    row["mean"] = row["mean"] + (max(-2.0, min(1.0, ratio)) - row["mean"]) / n
    row["n"] = n


def summary(book: dict) -> list[dict]:
    """For the logs and the report: each class with its results so far, best first."""
    rows = [{"key": k, "n": round(v["n"], 1), "mean": round(expected(book, k), 2)} for k, v in book.items()]
    return sorted(rows, key=lambda r: -r["mean"])
