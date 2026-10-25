"""Seeing trouble coming and doing something about it. Pure functions: game data in, a choice out.

The API already lists foreign fleets that are flying towards the agent's planets (`GET /fleet`, `is_mine: false`):
what kind of mission, where it comes from and when it arrives, but not what is on board. That is what a real player
sees too. An attack takes up to half of the stock, so the sensible answer is to turn the stock into defence units
before the fleet lands. A spy probe is a warning that an attack may follow in the next minutes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .brain import RESOURCES, VALUE
from .combat import HULL_DIVISOR, STATS
from .personas import Persona

ALERT_SECONDS = 900.0       # after a hostile fleet was seen, stay alert (look often, defend) for this long
WATCH_SECONDS = 20.0        # how often an alert agent looks at the game
COOLDOWN_SECONDS = 600.0    # at most one defence order per this many seconds
SPY_ONLY_FACTOR = 0.5       # a probe is only a warning: spend half as much as against a real attack
MAX_ORDER = 300             # never order more than this many units at once
HOSTILE = ("attack", "acs_attack", "missile_attack")
SCOUTS = ("espionage",)


@dataclass
class Threat:
    id: int
    kind: str                   # the mission name
    arrives_in: float           # seconds
    origin: tuple[int, int, int]

    @property
    def is_attack(self) -> bool:
        return self.kind in HOSTILE


def find_threats(missions: list[dict]) -> list[Threat]:
    """Foreign fleets that are on their way to us (not returning home, not ours)."""
    found = []
    for m in missions:
        if m.get("is_mine") or m.get("is_return"):
            continue
        kind = m.get("mission")
        if kind not in HOSTILE and kind not in SCOUTS:
            continue
        src = m.get("from") or {}
        found.append(Threat(int(m["id"]), kind, float(m.get("seconds_remaining") or 0),
                            (int(src.get("galaxy") or 0), int(src.get("system") or 0), int(src.get("position") or 0))))
    return found


def _worth(name: str) -> float:
    stats = STATS[name]
    cost = stats[0] * VALUE["metal"] + stats[1] * VALUE["crystal"] + stats[2] * VALUE["deuterium"]
    hull, shield, attack = stats[3], stats[4], stats[5]
    return (hull / HULL_DIVISOR + shield + attack) / max(1.0, cost)   # fighting power per resource spent


def choose_defense(persona: Persona, units: list[dict], have: dict, share: float) -> tuple[str, int] | None:
    """The defence unit to order and how many, spending at most `share` of each resource in stock."""
    if share <= 0:
        return None
    best = None
    for u in units:
        name = u["machine_name"]
        if u.get("category") != "defense" or "missile" in name or name not in STATS:
            continue
        if not u.get("requirements_met") or not u.get("character_class_met", True) or u.get("max_buildable", 0) < 1:
            continue
        score = _worth(name) * persona.unit_weights.get(name, 1.0)
        if score > 0 and (best is None or score > best[0]):
            best = (score, u)
    if best is None:
        return None
    u = best[1]
    cost = u["cost_per_unit"]
    limits = [int(have.get(r, 0) * share // cost[r]) for r in RESOURCES if cost.get(r, 0) > 0]
    amount = min([u["max_buildable"], MAX_ORDER, *limits]) if limits else 0
    return (u["machine_name"], amount) if amount >= 1 else None


AGGRESSOR_MEMORY = 12 * 3600.0   # a planet that attacked us is treated as dangerous for this long


def defence_share(persona: Persona, threats: list[Threat], aggressors: set[tuple[int, int, int]] | None = None) -> float:
    """How much of the stock to turn into defence given what is coming.

    An attack always counts. A spy probe only counts when it comes from a planet that has attacked us before:
    otherwise every harmless scout (the raiders send them all the time) would eat the economy."""
    if any(t.is_attack for t in threats):
        return persona.caution
    if any(t.origin in (aggressors or set()) for t in threats):
        return persona.caution * SPY_ONLY_FACTOR
    return 0.0
