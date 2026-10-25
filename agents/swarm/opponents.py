"""Learning level 2: what an agent remembers about each opponent, and how that changes who it attacks.

A record is a small dict kept in Memory.opponents under the target's coordinates:
  strength   value (metal-equivalents) of the ships and defence seen at the last spy report
  loot       loot seen at the last spy report
  spied_at   when that report was read
  attacks / wins / losses   our own attacks on it and how each ended (judge_trade verdict)
  net        total gained minus total lost against it (loot + destroyed - own losses)
  last_at    when we last attacked it
Old facts fade: after FADE_SECONDS a bad record counts half as much, so a once-strong neighbour is tried again.
"""
from __future__ import annotations

from .combat import unit_value

FADE_SECONDS = 12 * 3600   # server seconds (the game runs fast, so this is a short memory)
MIN_FACTOR, MAX_FACTOR = 0.25, 1.5


def strength(ships: dict | None, defense: dict | None) -> int:
    total = 0
    for group in (ships or {}, defense or {}):
        for name, n in group.items():
            try:
                total += int(unit_value(name) * int(n))
            except (KeyError, TypeError, ValueError):
                continue
    return total


def _blank() -> dict:
    return {"attacks": 0, "wins": 0, "losses": 0, "net": 0}


def record_spy(opponents: dict, coord, now: float, loot: int, ships: dict | None, defense: dict | None) -> dict:
    rec = opponents.setdefault(coord, _blank())
    rec.update(spied_at=now, loot=int(loot), strength=strength(ships, defense))
    return rec


def record_result(opponents: dict, coord, now: float, verdict: str | None, gained: int, lost: int) -> dict:
    rec = opponents.setdefault(coord, _blank())
    rec["attacks"] = rec.get("attacks", 0) + 1
    if verdict == "good":
        rec["wins"] = rec.get("wins", 0) + 1
    elif verdict == "bad":
        rec["losses"] = rec.get("losses", 0) + 1
    rec["net"] = rec.get("net", 0) + int(gained) - int(lost)
    rec["last_at"] = now
    return rec


def target_factor(rec: dict | None, now: float) -> float:
    """How much a target's loot is worth to us given what we know of it: above 1 for proven easy targets,
    below 1 for ones that beat us. 1.0 for strangers."""
    if not rec or not rec.get("attacks"):
        return 1.0
    attacks = rec["attacks"]
    score = (rec.get("wins", 0) - 2 * rec.get("losses", 0)) / (attacks + 1)   # a loss weighs double
    age = max(0.0, now - rec.get("last_at", now))
    fade = 0.5 if age > FADE_SECONDS else 1.0
    return min(MAX_FACTOR, max(MIN_FACTOR, 1.0 + 0.5 * score * fade))


def summary(coord, rec: dict) -> dict:
    return {"at": f"{coord[0]}:{coord[1]}:{coord[2]}", "strength": rec.get("strength"), "loot": rec.get("loot"),
            "attacks": rec.get("attacks", 0), "wins": rec.get("wins", 0), "losses": rec.get("losses", 0),
            "net": rec.get("net", 0)}
