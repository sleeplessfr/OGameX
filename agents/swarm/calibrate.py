"""Calibrated fight estimates (plan step 3, see claude/plan-real-learning.md).

The fight simulator says "win chance 85%". Is that true for this agent, against its real opponents? Every attack is put in a
bucket by its predicted chance and the real outcome (did we take loot / destroy without loss) is counted. From then on the
agent uses a corrected chance: the prediction blended with what really happened in that bucket (the prediction counts as K
earlier results, so a few surprises do not swing it, many do). A simulator that is over-confident therefore makes the raider
more careful, and an over-cautious one lets it attack more. Old results fade. Plain numbers saved in the memory file.
"""
from __future__ import annotations

EDGES = (0.0, 0.3, 0.6, 0.8, 0.95, 1.0001)
K = 4.0
DECAY = 0.98


def bucket(p: float) -> int:
    p = max(0.0, min(1.0, float(p)))
    for i in range(len(EDGES) - 1):
        if p < EDGES[i + 1]:
            return i
    return len(EDGES) - 2


def new_state() -> dict:
    return {}


def clean_state(data) -> dict:
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        try:
            n, w = float(v["n"]), float(v["w"])
            if str(k).isdigit() and int(k) < len(EDGES) - 1 and 0 <= w <= n <= 1e6:
                out[str(int(k))] = {"n": n, "w": w}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def corrected(calib: dict, p: float | None) -> float | None:
    """The predicted win chance blended with what happened in its bucket."""
    if p is None:
        return None
    row = calib.get(str(bucket(p)))
    if not row:
        return p
    return (p * K + row["w"]) / (K + row["n"])


def update(calib: dict, predicted: float | None, won: bool) -> None:
    if predicted is None:
        return
    for row in calib.values():
        row["n"] *= DECAY
        row["w"] *= DECAY
    row = calib.setdefault(str(bucket(predicted)), {"n": 0.0, "w": 0.0})
    row["n"] += 1.0
    row["w"] += 1.0 if won else 0.0


def won_battle(loot: int, their_loss: int, my_loss: int) -> bool:
    return int(loot) > 0 or (int(their_loss) > 0 and int(my_loss) == 0)
