"""Seeing trouble coming and doing something about it. Pure functions: game data in, a choice out.

The API already lists foreign fleets that are flying towards the agent's planets (`GET /fleet`, `is_mine: false`):
what kind of mission, where it comes from and when it arrives, but not what is on board. That is what a real player
sees too. An attack takes up to half of the stock, so the sensible answer is to turn the stock into defence units
before the fleet lands. A spy probe is a warning that an attack may follow in the next minutes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .brain import RESOURCES, VALUE
from .combat import HULL_DIVISOR, STATS, harmless_fleet
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
        if kind in HOSTILE and harmless_fleet(m.get("ships")):   # no warship on board: it cannot hurt planet or fleet
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


def _power(name: str) -> float:
    stats = STATS[name]
    return stats[3] / HULL_DIVISOR + stats[4] + stats[5]


def choose_defense(persona: Persona, units: list[dict], have: dict, share: float,
                   seconds_available: float | None = None) -> tuple[str, int] | None:
    """The defence unit to order and how many, spending at most `share` of each resource in stock.

    Whatever is ordered is paid at once, so even an unfinished order keeps that stock out of the raider's hands.
    But only units that are finished when the fleet lands can fight. With `seconds_available` (the warning time)
    the choice therefore favours the unit that gets the most fighting power done in time; without it, the best
    power per resource spent."""
    if share <= 0:
        return None
    best = None
    for u in units:
        name = u["machine_name"]
        if u.get("category") != "defense" or "missile" in name or name not in STATS:
            continue
        if not u.get("requirements_met") or not u.get("character_class_met", True) or u.get("max_buildable", 0) < 1:
            continue
        cost = u["cost_per_unit"]
        limits = [int(have.get(r, 0) * share // cost[r]) for r in RESOURCES if cost.get(r, 0) > 0]
        amount = min([u["max_buildable"], MAX_ORDER, *limits]) if limits else 0
        if amount < 1:
            continue
        weight = persona.unit_weights.get(name, 1.0)
        spu = float(u.get("seconds_per_unit") or 0)
        if seconds_available is not None and spu > 0:
            done = min(amount, int(max(0.0, seconds_available) // spu))
            score = (done * _power(name) + 0.001 * amount * _power(name)) * weight   # unfinished units still protect the stock
        else:
            score = _worth(name) * weight
        if score > 0 and (best is None or score > best[0]):
            best = (score, name, amount)
    return (best[1], best[2]) if best else None


AGGRESSOR_MEMORY = 12 * 3600.0   # a planet that attacked us is treated as dangerous for this long
RECENT_RAID = 3 * 3600.0         # attacked within this time: stay on guard (look every GUARD_SECONDS, keep rearming)
GUARD_SECONDS = 60.0
PEACE_WATCH_SECONDS = 180.0      # even at peace, look for incoming fleets at least this often
STANDING_FACTOR = 0.25           # while on guard, this share of `caution` is always turned into defence


def defence_share(persona: Persona, threats: list[Threat], aggressors: set[tuple[int, int, int]] | None = None,
                  on_guard: bool = False) -> float:
    """How much of the stock to turn into defence given what is coming.

    An attack always counts. A spy probe only counts when it comes from a planet that has attacked us before:
    otherwise every harmless scout (the raiders send them all the time) would eat the economy."""
    if any(t.is_attack for t in threats):
        return persona.caution
    if any(t.origin in (aggressors or set()) for t in threats):
        return persona.caution * SPY_ONLY_FACTOR
    if on_guard:
        return persona.caution * STANDING_FACTOR   # recently robbed: rebuild instead of waiting for the next raid
    return 0.0
