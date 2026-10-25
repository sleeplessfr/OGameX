"""Expeditions: who goes, with what, and what came of it. Pure functions: game data in, a choice out (no network).

Rules found in app/GameMissions/ExpeditionMission.php (this fork), which the decisions below are built on:

* Target is position 16 of any system. Needs astrophysics >= 1. `holding_hours` must be 1..astrophysics level.
* Slots: an expedition needs a free expedition slot (floor(sqrt(astrophysics)) + 2 for a Discoverer; `/status`
  `fleet_slots.expedition_used/expedition_max` says exactly) and, as any fleet, a normal fleet slot.
* There is no "maximum fleet points" rule. The outcome does not depend on the holding time either (a longer stay only makes
  a 'delay' outcome longer: 2x/3x/5x the holding time), so agents always hold 1 hour.
* Outcome weights (default settings, total 99.85): resources 35, nothing 25, ships 17, dark matter 7.5, delay 7.5, pirates 3,
  speedup 2.75, aliens 1.5, merchant 0.4, fleet lost (black hole) 0.2. A Discoverer halves pirates and aliens.
* Resources found are capped by the fleet's free cargo capacity: base max is 40,000 metal while the best player has under 10,000
  points (500,000 under 100,000 points, then 1.2M ...), random between 10% and 100% of it, times a variant (normal/rare/
  exceptional), x1.5 (x economy speed) for a Discoverer, x2 with a pathfinder in the fleet; crystal is 2/3 and deuterium 1/3 of
  that. Found ships are worth at most the same amount and are also capped by cargo. So cargo capacity is what pays, warships
  are only an escort for the (rare) pirate and alien fights.
* Messages (category `expeditions`) carry a `key` that names the outcome; the amounts are only in the plain-text body, e.g.
  "Metal 12,345 have been captured.", "(150 Dark Matter)" and "The following ships are now part of the fleet:\\nSmall Cargo: 3".
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field

from .personas import Persona

HOLDING_HOURS = 1                 # the outcome does not depend on it (see above)
MIN_ASTROPHYSICS_DISCOVERER = 1
MIN_ASTROPHYSICS_OTHER = 3        # one expedition slot needs level 1, but a non-Discoverer only bothers once it has a real fleet
MIN_CAPACITY = 4000               # smaller fleets find too little to be worth a trip
MAX_CAPACITY_TARGET = 300_000     # no point in carrying more than this, however rich the universe is
CAPACITY_SHARE = 0.5              # aim to carry this share of the largest possible find
SEND_SHARE = {True: 0.6, False: 0.4}   # share of the cargo ships at home an expedition may take (Discoverer / others)
ESCORT_SHARE = 0.3                # share of the fighters at home that may escort
GAP_SECONDS = {True: 900.0, False: 6 * 3600.0}   # pause after a launch (Discoverer / others)
RETRY_SECONDS = 1800.0
LOSS_PAUSE = 6 * 3600.0           # pause after a fleet was lost
SCALE_MIN, SCALE_MAX = 0.25, 1.5
INFLIGHT_MAX_AGE = 3 * 86400.0    # a launch with no report after this long is forgotten

FIGHTERS = ("light_fighter", "heavy_fighter", "cruiser", "battle_ship", "battlecruiser")
CARGO_ORDER_DISCOVERER = ("pathfinder", "large_cargo", "small_cargo")
CARGO_ORDER_OTHER = ("large_cargo", "small_cargo")

# find ceilings by the best player's points (from the game): metal, before multipliers
FIND_TIERS = ((10_000, 40_000), (100_000, 500_000), (1_000_000, 1_200_000), (5_000_000, 1_800_000), (25_000_000, 2_400_000),
              (50_000_000, 3_000_000), (75_000_000, 3_600_000), (100_000_000, 4_200_000))

# message key -> short outcome name used in memory, logs and the report
OUTCOMES = {
    "expedition_gain_resources": "resources", "expedition_gain_ships": "ships", "expedition_gain_dark_matter": "dark_matter",
    "expedition_failed": "nothing", "expedition_failed_and_delay": "delay", "expedition_failed_and_speedup": "speedup",
    "expedition_loss_of_fleet": "loss", "expedition_battle_pirates": "pirates", "expedition_battle_aliens": "aliens",
    "expedition_merchant_found": "merchant", "expedition_gain_item": "item",
}
SHIP_NAMES = {"battleship": "battle_ship"}   # title -> machine name where simply lower-casing is not enough


def is_discoverer(persona: Persona) -> bool:
    return persona.character_class == "discoverer"


def wants_expeditions(persona: Persona) -> bool:
    return is_discoverer(persona) or persona.expedition_interest > 0


def new_state() -> dict:
    return {"next_at": 0.0, "scale": 1.0, "inflight": [], "sent": 0, "outcomes": {}, "metal": 0, "crystal": 0, "deuterium": 0,
            "dark_matter": 0, "ships_found": {}, "ships_lost": {}, "lost_value": 0, "sent_value": 0, "last_read": 0.0}


def clean_state(data) -> dict:
    """Tolerant loader for the saved memory: anything missing or malformed falls back to the default."""
    out = new_state()
    if not isinstance(data, dict):
        return out
    for key, default in out.items():
        value = data.get(key)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                out[key] = type(default)(value) if value is not None else default
            except (TypeError, ValueError):
                pass
        elif isinstance(default, dict) and isinstance(value, dict):
            out[key] = dict(value)
        elif isinstance(default, list) and isinstance(value, list):
            out[key] = [v for v in value if isinstance(v, dict)]
    out["scale"] = min(SCALE_MAX, max(SCALE_MIN, out["scale"]))
    return out


# ---- who goes, with what ---------------------------------------------------------------------------------------------
def find_ceiling(top_score: int) -> int:
    """The most metal one find can be worth, from the best player's points."""
    for limit, ceiling in FIND_TIERS:
        if top_score < limit:
            return ceiling
    return 5_000_000


def target_capacity(top_score: int, discoverer: bool, scale: float = 1.0) -> int:
    """How much cargo capacity is worth sending."""
    ceiling = find_ceiling(top_score) * (1.5 if discoverer else 1.0)
    return int(min(MAX_CAPACITY_TARGET, ceiling * CAPACITY_SHARE * scale))


def choose_fleet(ships_here: dict[str, int], cargo_of: dict[str, int], want: int, discoverer: bool) -> dict[str, int]:
    """Cargo ships (largest first) until `want` capacity is reached, plus a few fighters as escort. {} if too little to be worth it."""
    share = SEND_SHARE[discoverer]
    fleet: dict[str, int] = {}
    capacity = 0
    for name in (CARGO_ORDER_DISCOVERER if discoverer else CARGO_ORDER_OTHER):
        have, cap = ships_here.get(name, 0), cargo_of.get(name, 0)
        if have <= 0 or cap <= 0 or capacity >= want:
            continue
        allowed = int(have * share)
        if discoverer:
            allowed = max(1, allowed)
        n = min(allowed, math.ceil((want - capacity) / cap))
        if n > 0:
            fleet[name] = n
            capacity += n * cap
    if capacity < MIN_CAPACITY:
        return {}
    escorts = sum(fleet.values()) // 2
    for name in FIGHTERS:
        n = min(int(ships_here.get(name, 0) * ESCORT_SHARE), escorts)
        if n > 0:
            fleet[name] = n
            escorts -= n
    return fleet


@dataclass
class Decision:
    ships: dict[str, int] = field(default_factory=dict)   # {} = do not go
    retry_in: float = RETRY_SECONDS                        # when to consider again
    why: str = ""
    holding_hours: int = HOLDING_HOURS
    capacity: int = 0


def can_consider(persona: Persona, *, now: float, state: dict, astro: int, fleet_slots: dict, on_guard: bool, alert: bool) -> str | None:
    """Cheap checks before anything is fetched. Returns the reason for staying home, or None."""
    disc = is_discoverer(persona)
    if not wants_expeditions(persona):
        return "not interested"
    if astro < (MIN_ASTROPHYSICS_DISCOVERER if disc else MIN_ASTROPHYSICS_OTHER):
        return "astrophysics too low"
    if "expedition_max" not in fleet_slots:
        return "game has no expedition slots"
    if fleet_slots["expedition_used"] >= fleet_slots["expedition_max"]:
        return "no free expedition slot"
    if fleet_slots["used"] >= fleet_slots["max"]:
        return "no free fleet slot"
    if persona.aggression >= 0.3 and fleet_slots["max"] > 1 and fleet_slots["used"] >= fleet_slots["max"] - 1:
        return "keeps the last slot for raids"
    if alert or (on_guard and persona.aggression >= 0.3):   # a peaceful agent leaves on guard (it only stays for a raid it can see coming)
        return "needs the fleet at home (raiders about)"
    if now < state.get("next_at", 0.0):
        return "cooling down"
    return None


def decide(persona: Persona, rng: random.Random, *, ships_here: dict[str, int], cargo_of: dict[str, int], top_score: int, state: dict) -> Decision:
    """Call after can_consider() said yes. Rolls the persona's interest, then builds the fleet."""
    disc = is_discoverer(persona)
    if not disc and rng.random() >= persona.expedition_interest:
        return Decision(retry_in=RETRY_SECONDS, why="not in the mood")
    want = target_capacity(top_score, disc, state.get("scale", 1.0))
    fleet = choose_fleet(ships_here, cargo_of, want, disc)
    if not fleet:
        return Decision(retry_in=RETRY_SECONDS * 2, why="not enough cargo ships at home")
    capacity = sum(n * cargo_of.get(name, 0) for name, n in fleet.items())
    return Decision(ships=fleet, retry_in=GAP_SECONDS[disc], holding_hours=HOLDING_HOURS, capacity=capacity,
                    why=f"{'discoverer' if disc else 'curious'}: carries {capacity:,} of a possible {want:,}")


# ---- results -----------------------------------------------------------------------------------------------------------
@dataclass
class Result:
    outcome: str                                  # a value of OUTCOMES, or "other"
    metal: int = 0
    crystal: int = 0
    deuterium: int = 0
    dark_matter: int = 0
    ships: dict[str, int] = field(default_factory=dict)


def _number(text: str) -> int:
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else 0


def ship_machine_name(title: str) -> str:
    plain = re.sub(r"\s+", " ", title.strip()).lower()
    return SHIP_NAMES.get(plain.replace(" ", ""), plain.replace(" ", "_"))


def parse_message(key: str, body: str) -> Result:
    """Turn an expedition message (key + plain-text body) into what it says."""
    outcome = OUTCOMES.get(key or "", "other")
    r = Result(outcome)
    body = body or ""
    if outcome == "resources":
        m = re.search(r"(Metal|Crystal|Deuterium)\s+([\d.,]+)\s+have been captured", body, re.I)
        if m:
            setattr(r, m.group(1).lower(), _number(m.group(2)))
    elif outcome == "dark_matter":
        m = re.search(r"\(\s*([\d.,]+)\s*Dark Matter\s*\)", body, re.I)
        if m:
            r.dark_matter = _number(m.group(1))
    elif outcome == "ships":
        _, _, tail = body.partition("The following ships are now part of the fleet:")
        for title, amount in re.findall(r"^\s*([A-Za-z][A-Za-z ]*?):\s*([\d.,]+)\s*$", tail, re.M):
            n = _number(amount)
            if n > 0:
                name = ship_machine_name(title)
                r.ships[name] = r.ships.get(name, 0) + n
    return r


def fleet_value(ships: dict[str, int], cost_of: dict[str, int]) -> int:
    return sum(n * cost_of.get(name, 0) for name, n in ships.items())


def record_sent(state: dict, now: float, ships: dict[str, int], value: int, retry_in: float) -> None:
    state["sent"] += 1
    state["sent_value"] += value
    state["inflight"] = (state["inflight"] + [{"at": now, "ships": dict(ships), "value": value}])[-10:]
    state["next_at"] = now + retry_in


def record_result(state: dict, now: float, result: Result) -> dict:
    """Count one outcome. Returns the fields for the `expedition_result` log event.

    Reports carry no mission id, so the oldest launch still in flight is assumed to be the one that came back (all go for 1 hour,
    so they return in order); a fleet lost in a black hole is charged to it.
    """
    state["inflight"] = [f for f in state["inflight"] if now - f.get("at", 0.0) < INFLIGHT_MAX_AGE]
    launch = state["inflight"].pop(0) if state["inflight"] else None
    state["outcomes"][result.outcome] = state["outcomes"].get(result.outcome, 0) + 1
    for res in ("metal", "crystal", "deuterium", "dark_matter"):
        state[res] += getattr(result, res)
    for name, n in result.ships.items():
        state["ships_found"][name] = state["ships_found"].get(name, 0) + n
    fields: dict = {"outcome": result.outcome}
    for res in ("metal", "crystal", "deuterium", "dark_matter"):
        if getattr(result, res):
            fields[res] = getattr(result, res)
    if result.ships:
        fields["ships"] = result.ships
    if result.outcome == "loss":
        lost = launch["ships"] if launch else {}
        for name, n in lost.items():
            state["ships_lost"][name] = state["ships_lost"].get(name, 0) + n
        state["lost_value"] += launch["value"] if launch else 0
        state["scale"] = max(SCALE_MIN, state["scale"] * 0.6)
        state["next_at"] = max(state["next_at"], now + LOSS_PAUSE)
        fields["lost"] = lost
        fields["lost_value"] = launch["value"] if launch else 0
    elif result.outcome != "other":
        state["scale"] = min(SCALE_MAX, state["scale"] * 1.05)
    return fields
