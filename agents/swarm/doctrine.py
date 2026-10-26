"""Military doctrine: which warship is worth buying, and when a fighting agent should put more money into its army.

Written for people, not only programmers. Three questions are answered here, all with plain functions (no network):

1. WHICH SHIP?  `rank_ships` plays pretend battles with the battle simulator (`combat.py`). For each warship type it
   takes a batch worth the same money, lets it fight a typical defender (what recent spy reports showed: rocket
   launchers, lasers, ...) and scores it:

        score = value of the defender's losses  /  (value of my own losses + a quarter of the batch price)

   The "quarter of the price" is the wear and tear (fuel, time) every ship has even when it survives. Values use the
   usual 3:2:1 prices (1 metal = 1, 1 crystal = 1.5, 1 deuterium = 3). The simulator knows rapid fire, so a cruiser
   (which shoots rocket launchers again and again) and a bomber (which does the same to all defences) beat a pile of
   light fighters, which die to rocket launchers. A ship that cannot win simply gets a low score.

2. HOW MUCH MONEY?  A raider that keeps meeting targets it cannot beat ("win chance 0%") is wasting its time with a
   small army. `army_share_now` raises the share of spare resources it spends on ships (up to `PUSH_CAP`) while such
   skips pile up, and lets it fall back when attacks start again. Only fighting personas do this.

3. WHAT TO RESEARCH?  The best ship may need a better shipyard, drives and weapons. `research_goals` lists
   everything still missing (the whole chain of prerequisites), as "goals" the agent works through like the ones for a
   colony or for recyclers. Only after a recent run of out-of-reach targets, only for fighting personas.

Memory (`Memory.doctrine`, saved to disk) is a small dict: see `new_state`.
"""
from __future__ import annotations

import random
from functools import lru_cache

from .brain import VALUE
from .combat import DEFENSE, DEFENSE_REPAIR, MAX_UNITS, STATS, Tech, _expand, _fight
from .personas import Persona

# ---- tunables ----------------------------------------------------------------------------------------------------
WARSHIPS = ("light_fighter", "heavy_fighter", "cruiser", "battle_ship", "bomber", "destroyer", "battlecruiser", "reaper")
GENERAL_ONLY = {"reaper"}           # ships only the General character class can build
FALLBACK_DEFENDERS = {"rocket_launcher": 1000}   # what a typical target is assumed to have when no spy report shows its defence
BUDGET_FACTORS = (2.0, 4.0)         # the pretend batch of each ship type is worth this many times the defender (a small and a big fleet, scores averaged)
WEAR = 0.25                         # share of the batch price counted as cost even if every ship survives
RUNS = 6                            # pretend battles per ship type
INTEL_AGE = 4 * 3600.0              # spy reports older than this (server seconds) are not used for the typical defender

POOR_STOCK = 20_000.0               # a stock worth less than this (3:2:1 prices) is a planet still starting up: see `is_poor`

WINDOW = 4 * 3600.0                 # how long a skip ("cannot win") or an attack counts as recent
MIN_SKIPS = 3                       # this many recent skips before the agent reacts at all
PUSH_CAP = 0.8                      # the army share never goes above this
PUSH_STEP = 6.0                     # skips (above MIN_SKIPS - 1) needed to reach the cap
HOPELESS_WIN_CHANCE = 0.10          # a target with a simulated win chance up to this is "out of reach"
MAX_EVENTS = 40                     # skips / attacks remembered

TECH_GOALS = {"weapon_technology": 6, "armor_technology": 5, "shielding_technology": 4}   # levels a push works towards
LEVEL_PENALTY = 0.04                # when choosing which ship to aim for: each missing level costs this much score

# What the game asks for (copied from app/GameObjects/*Objects.php). name: ((thing, level), ...)
SHIP_NEEDS = {
    "light_fighter": (("shipyard", 1), ("combustion_drive", 1)),
    "heavy_fighter": (("shipyard", 3), ("armor_technology", 2), ("impulse_drive", 2)),
    "cruiser": (("shipyard", 5), ("impulse_drive", 4), ("ion_technology", 2)),
    "battle_ship": (("shipyard", 7), ("hyperspace_drive", 4)),
    "battlecruiser": (("shipyard", 8), ("hyperspace_drive", 5), ("hyperspace_technology", 5), ("laser_technology", 12)),
    "bomber": (("shipyard", 8), ("impulse_drive", 6), ("plasma_technology", 5)),
    "destroyer": (("shipyard", 9), ("hyperspace_drive", 6), ("hyperspace_technology", 5)),
    "reaper": (("shipyard", 6), ("impulse_drive", 6), ("hyperspace_drive", 4), ("weapon_technology", 8), ("shielding_technology", 6)),
}
TECH_NEEDS = {
    "energy_technology": (("research_lab", 1),),
    "laser_technology": (("energy_technology", 2), ("research_lab", 1)),
    "ion_technology": (("research_lab", 4), ("energy_technology", 4), ("laser_technology", 5)),
    "hyperspace_technology": (("research_lab", 7), ("energy_technology", 5), ("shielding_technology", 5)),
    "plasma_technology": (("research_lab", 4), ("energy_technology", 8), ("laser_technology", 10), ("ion_technology", 5)),
    "combustion_drive": (("energy_technology", 1), ("research_lab", 1)),
    "impulse_drive": (("energy_technology", 1), ("research_lab", 2)),
    "hyperspace_drive": (("research_lab", 7), ("hyperspace_technology", 3)),
    "weapon_technology": (("research_lab", 4),),
    "shielding_technology": (("research_lab", 6), ("energy_technology", 3)),
    "armor_technology": (("research_lab", 2),),
}
BUILDINGS = {"shipyard", "research_lab"}


# ---- who does this -----------------------------------------------------------------------------------------------
def applies(persona: Persona) -> bool:
    """Fighting personalities (raider, balanced, anything with the General class). The others never use the doctrine."""
    return persona.character_class == "general" or persona.aggression >= 0.2


def candidates(persona: Persona) -> tuple[str, ...]:
    """Warship types this personality may buy (the reaper only for the General class)."""
    return tuple(s for s in WARSHIPS if s not in GENERAL_ONLY or persona.character_class == "general")


def price(name: str) -> float:
    """Price of one unit at the 3:2:1 reference rates (metal 1, crystal 1.5, deuterium 3)."""
    s = STATS[name]
    return s[0] * VALUE["metal"] + s[1] * VALUE["crystal"] + s[2] * VALUE["deuterium"]


# ---- the pretend battles -----------------------------------------------------------------------------------------
def typical_defenders(intel: dict, now: float) -> tuple[dict[str, int], str]:
    """The defender a fleet should be built against: the average of the recent spy reports that showed the defence.
    Returns (units by name, a sentence saying where it comes from). No usable report: the rocket-launcher fallback."""
    reports = []
    for info in (intel or {}).values():
        if not isinstance(info, dict) or info.get("defense") is None or now - float(info.get("at", 0)) > INTEL_AGE:
            continue
        mix: dict[str, int] = {}
        for part in (info.get("ships"), info.get("defense")):
            for name, n in (part or {}).items():
                if name in STATS and name not in ("espionage_probe", "solar_satellite") and int(n or 0) > 0:
                    mix[name] = mix.get(name, 0) + int(n)
        if mix:
            reports.append(mix)
    if not reports:
        return dict(FALLBACK_DEFENDERS), "no spy report with a defence: assuming rocket launchers"
    total: dict[str, float] = {}
    for mix in reports:
        for name, n in mix.items():
            total[name] = total.get(name, 0) + n / len(reports)
    mixed = {name: max(1, _round2(n)) for name, n in total.items()}
    return mixed, f"average of {len(reports)} recent spy report{'s' if len(reports) != 1 else ''}"


def _round2(n: float) -> int:
    """Round to two significant digits, so a small change in the reports does not change the answer every time."""
    n = int(round(n))
    if n < 100:
        return n
    return int(round(n, -(len(str(n)) - 2)))


def simulate(ship: str, count: int, defenders: dict[str, int], attacker_tech: Tech, defender_tech: Tech | None,
             runs: int, rng: random.Random) -> dict:
    """Pretend `count` ships of one type attack `defenders` `runs` times. Returns averages:
    dealt (defender's loss), lost (my loss), win (share of won battles), survive (share of ships alive); values at 3:2:1 prices."""
    defender_tech = defender_tech if defender_tech is not None else attacker_tech
    total = count + sum(max(0, n) for n in defenders.values())
    scale = min(1.0, MAX_UNITS / total) if total else 1.0
    dealt = lost = win = survive = 0.0
    for _ in range(runs):
        att = _expand({ship: count}, attacker_tech, scale)
        dfn = _expand(defenders, defender_tech, scale)
        att_start, dfn_start = list(att), list(dfn)
        _fight(att, dfn, rng)
        win += 1.0 if (att and not dfn) else 0.0
        lost += sum(price(u[0]) for u in att_start if u[4] <= 0) / scale
        dealt += sum(price(u[0]) * (1 - DEFENSE_REPAIR if u[0] in DEFENSE else 1) for u in dfn_start if u[4] <= 0) / scale
        survive += len(att) / max(1, len(att_start))
    return {"dealt": dealt / runs, "lost": lost / runs, "win": win / runs, "survive": survive / runs}


@lru_cache(maxsize=64)
def _evaluate(ships: tuple, defenders_key: tuple, my_tech: Tech, their_tech: Tech | None, budget_factors: tuple) -> tuple:
    defenders = dict(defenders_key)
    value = sum(price(n) * v for n, v in defenders.items())
    rng = random.Random(repr((ships, defenders_key, my_tech, their_tech)))   # a stream of its own: no agent's dice are used up
    rows = []
    for ship in ships:
        scores, wins = [], []
        for factor in budget_factors:
            count = max(1, int(factor * value // price(ship)))
            r = simulate(ship, count, defenders, my_tech, their_tech, RUNS, rng)
            scores.append(r["dealt"] / (r["lost"] + WEAR * count * price(ship)))
            wins.append(r["win"])
        rows.append((ship, round(sum(scores) / len(scores), 4), round(sum(wins) / len(wins), 2), count, int(r["dealt"]), int(r["lost"])))
    return tuple(sorted(rows, key=lambda row: -row[1]))


def evaluate(ships, defenders: dict[str, int], my_tech: Tech | None = None, their_tech: Tech | None = None,
             budget_factors: tuple = BUDGET_FACTORS) -> list[dict]:
    """The full table, best first: ship, score, average win chance, ships in the biggest batch and what that batch dealt / lost."""
    key = tuple(sorted((k, int(v)) for k, v in defenders.items() if int(v) > 0))
    rows = _evaluate(tuple(ships), key, tuple(my_tech or (0, 0, 0)), tuple(their_tech) if their_tech else None, tuple(budget_factors))
    return [{"ship": s, "score": sc, "win": w, "count": c, "dealt": d, "lost": l} for s, sc, w, c, d, l in rows]


def rank_ships(ships, defenders: dict[str, int], my_tech: Tech | None = None, their_tech: Tech | None = None,
               budget_factors: tuple = BUDGET_FACTORS) -> list[tuple[str, float]]:
    """[(ship, score)] best first. Score = damage dealt per unit of cost, from pretend battles (see the module text)."""
    return [(r["ship"], r["score"]) for r in evaluate(ships, defenders, my_tech, their_tech, budget_factors)]


def warship_weights(persona: Persona, ranking: list[tuple[str, float]], usable) -> dict[str, float]:
    """Buying weights for the warships that can be built now, from the doctrine's scores instead of the persona's fixed list.
    The best one gets the persona's highest fighter weight; a ship scoring half as much gets a quarter of it (squared)."""
    scores = {s: sc for s, sc in ranking if s in usable and sc > 0}
    if not scores:
        return {}
    top = max([persona.unit_weights.get(s, 0.0) for s in WARSHIPS] + [1.0])
    best = max(scores.values())
    return {s: top * (sc / best) ** 2 for s, sc in scores.items()}


# ---- evidence: are the targets out of reach? ---------------------------------------------------------------------
def new_state() -> dict:
    return {"skips": [], "attacks": [], "choice": None, "aim": None, "share": None, "table": []}


def clean_state(raw) -> dict:
    """Tolerant loader for the saved state: anything malformed is dropped."""
    out = new_state()
    if not isinstance(raw, dict):
        return out

    def number(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for key in ("skips", "attacks"):
        vals = raw.get(key)
        if isinstance(vals, list):
            out[key] = [float(v) for v in vals if number(v)][-MAX_EVENTS:]
    for key in ("choice", "aim"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    if number(raw.get("share")):
        out["share"] = float(raw["share"])
    if isinstance(raw.get("table"), list):
        out["table"] = [[str(r[0]), float(r[1])] for r in raw["table"]
                        if isinstance(r, (list, tuple)) and len(r) == 2 and number(r[1])][:10]
    return out


def out_of_reach(why: str) -> bool:
    """Does an `attack_skipped` reason say the fight cannot be won ('simulated fight: win chance 0%, ...')?"""
    if not why or not why.startswith("simulated fight"):
        return False
    try:
        pct = int(why.split("win chance ", 1)[1].split("%", 1)[0])
    except (IndexError, ValueError):
        return False
    return pct / 100.0 <= HOPELESS_WIN_CHANCE


def note_skip(state: dict, now: float) -> None:
    state["skips"] = (state.get("skips") or [])[-(MAX_EVENTS - 1):] + [float(now)]


def note_attack(state: dict, now: float) -> None:
    state["attacks"] = (state.get("attacks") or [])[-(MAX_EVENTS - 1):] + [float(now)]


def pressure(state: dict, now: float) -> int:
    """Recent hopeless skips minus twice the recent attacks (an attack that went out shows targets are reachable again)."""
    skips = sum(1 for t in state.get("skips") or [] if 0 <= now - t <= WINDOW)
    attacks = sum(1 for t in state.get("attacks") or [] if 0 <= now - t <= WINDOW)
    return skips - 2 * attacks


def pushing(persona: Persona, state: dict, now: float) -> bool:
    return applies(persona) and pressure(state, now) >= MIN_SKIPS


def army_share_now(persona: Persona, state: dict, now: float) -> tuple[float, str]:
    """Share of spare resources to spend on ships. The persona's own `army_share`, raised towards PUSH_CAP while its
    targets stay out of reach, back to normal as the skips grow old or attacks start again."""
    base = persona.army_share
    if not pushing(persona, state, now) or base >= PUSH_CAP:
        return base, ""
    p = pressure(state, now)
    ramp = min(1.0, (p - MIN_SKIPS + 1) / PUSH_STEP)
    share = round(base + (PUSH_CAP - base) * ramp, 3)
    return share, f"{p} recent targets out of reach (net of attacks): army share {base:.2f} -> {share:.2f}"


def surplus(have: dict, reserve: dict | None) -> dict:
    """What is left of the stock when the cost of the item the economy is saving for is kept aside."""
    keep = reserve or {}
    return {r: max(0, int(have.get(r, 0)) - int(keep.get(r, 0) or 0)) for r in set(have) | set(keep)}


def is_poor(have: dict) -> bool:
    """A planet that has hardly any stock yet (a new account). There is no real surplus to speak of, so the first ships
    still follow the old rule: a coin flip with the persona's army_share, as if no item were being saved for."""
    return sum(VALUE[r] * max(0, int(have.get(r, 0))) for r in VALUE) < POOR_STOCK


# ---- research ------------------------------------------------------------------------------------------------------
def chain(thing: str, level: int, out: dict | None = None) -> dict:
    """Every building / research level needed to reach `thing` at `level`, prerequisites included."""
    out = {} if out is None else out
    if out.get(thing, 0) >= level:
        return out
    out[thing] = level
    for dep, lv in TECH_NEEDS.get(thing, ()):
        chain(dep, lv, out)
    return out


def needs_of(ship: str) -> dict:
    """Everything the game asks for before this ship can be built: {name: level}."""
    out: dict = {}
    for thing, lv in SHIP_NEEDS.get(ship, ()):
        chain(thing, lv, out)
    return out


def aim_ship(ranking: list[tuple[str, float]], research_levels: dict) -> str | None:
    """The ship to work towards: the best score, with a small discount for every research level still missing (a slightly
    worse ship that is within reach beats a perfect one that needs hours of research)."""
    levels = research_levels or {}
    best = None
    for ship, score in ranking:
        if score <= 0 or ship not in SHIP_NEEDS:
            continue
        gap = sum(max(0, lv - int(levels.get(n, 0) or 0)) for n, lv in needs_of(ship).items() if n not in BUILDINGS)
        adjusted = score / (1 + LEVEL_PENALTY * gap)
        if best is None or adjusted > best[0]:
            best = (adjusted, ship)
    return best[1] if best else None


def research_goals(persona: Persona, state: dict, now: float, ranking: list[tuple[str, float]] | None,
                   research_levels: dict) -> tuple[dict, dict]:
    """(buildings, research) a pushing fighter works towards: the aimed ship's whole chain plus weapons / shielding / armour.
    Empty unless the agent fights AND its targets have been out of reach recently."""
    if not pushing(persona, state, now) or not ranking:
        return {}, {}
    levels = research_levels or {}
    goal = needs_of(aim_ship(ranking, levels) or "")
    for name, lv in TECH_GOALS.items():
        goal = chain(name, lv, goal)
    buildings = {n: lv for n, lv in goal.items() if n in BUILDINGS}
    research = {n: lv for n, lv in goal.items() if n not in BUILDINGS and int(levels.get(n, 0) or 0) < lv}
    # prerequisites first, so a goal the agent must wait for never hides one that could be started now
    return buildings, dict(sorted(research.items(), key=lambda kv: (_depth(kv[0]), kv[0])))


def _depth(name: str, seen: frozenset = frozenset()) -> int:
    deps = [d for d, _ in TECH_NEEDS.get(name, ()) if d not in BUILDINGS and d not in seen]
    return 0 if not deps else 1 + max(_depth(d, seen | {name}) for d in deps)
