"""Military and social decisions of an agent. Pure functions: game data in, a choice out (no network).

The loop an agent follows: find a neighbour -> spy on it -> read the report -> attack if it looks worth it.
Every threshold comes from the persona, so a raider and a turtle act differently on the same facts.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .brain import RESOURCES
from .combat import Estimate, Tech, estimate_battle, tech_from_research, unit_value
from .personas import Persona

FIGHTERS = ("light_fighter", "heavy_fighter", "cruiser", "battle_ship", "battlecruiser", "bomber", "destroyer")
CARGOS = ("small_cargo", "large_cargo")
CARGO_SHARE = 0.5         # when loot is lying around that the fleet cannot carry, up to this share of the stock may go on cargo ships
SYSTEMS_PER_GALAXY = 500  # distance weight of one galaxy jump, in "systems"
INTEL_MAX_AGE = 1500.0    # seconds: a spy report older than this is not trusted (targets keep building ships; at high game speed fast)
GAMBLE_MAX_RISK = 2.0     # gamble on an unknown defence only if the fleet at risk is worth at most this many times the loot
LOOT_SHARE = 0.5          # an attack can take at most half of the resources on a planet
SCORE_SLACK = 200         # new players have ~0 points: allow some room around the own score


Coord = tuple[int, int, int]


@dataclass
class Memory:
    """What an agent remembers about others. Times are server timestamps (seconds)."""
    spied: dict[Coord, float] = field(default_factory=dict)      # when we last sent probes there
    attacked: dict[Coord, float] = field(default_factory=dict)   # when we last attacked there
    pending: dict[Coord, float] = field(default_factory=dict)    # probes sent, report not read yet
    intel: dict[Coord, dict] = field(default_factory=dict)       # read reports: {'at', 'loot', 'defenders', 'owner_id'}
    avoid_until: dict[Coord, float] = field(default_factory=dict)  # targets the game refused (protection, vacation, ...)
    need_cargo: bool = False   # a worthwhile target had to be skipped because the fleet cannot carry enough loot
    risk_scale: float = 1.0    # learned caution: shrinks after costly defeats, recovers slowly after profitable raids
    battles: list[dict] = field(default_factory=list)  # recent results of own attacks (newest last, capped)
    aggressors: dict[Coord, float] = field(default_factory=dict)  # planets that attacked us (when we last saw one coming)
    trade_cooldown_until: float = 0.0  # no new merchant call before this server time (dark matter is scarce)

    # Saved to disk so a restart does not make the agent forget (JSON cannot have tuple keys, so coordinates become "g:s:p").
    _COORD_MAPS = ("spied", "attacked", "pending", "intel", "avoid_until", "aggressors")

    def to_dict(self) -> dict:
        out: dict = {"need_cargo": self.need_cargo, "risk_scale": self.risk_scale, "battles": self.battles,
                     "trade_cooldown_until": self.trade_cooldown_until}
        for name in self._COORD_MAPS:
            out[name] = {":".join(map(str, c)): v for c, v in getattr(self, name).items()}
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        """Tolerant: anything missing or malformed is simply left at its default."""
        m = cls()
        for name in cls._COORD_MAPS:
            for key, value in (data.get(name) or {}).items():
                try:
                    g, s, p = (int(x) for x in key.split(":"))
                except (ValueError, AttributeError):
                    continue
                getattr(m, name)[(g, s, p)] = value
        m.need_cargo = bool(data.get("need_cargo", False))
        try:
            m.risk_scale = min(1.5, max(0.1, float(data.get("risk_scale", 1.0))))
        except (TypeError, ValueError):
            pass
        try:
            m.trade_cooldown_until = float(data.get("trade_cooldown_until", 0.0))
        except (TypeError, ValueError):
            pass
        battles = data.get("battles")
        m.battles = battles if isinstance(battles, list) else []
        return m


@dataclass
class Intel:
    loot: int                 # what an attack could take
    defenders: int | None     # ships + defense units seen; None = the report did not show them
    owner_id: int | None
    ships: dict | None = None      # what the report showed, by unit name (None = not shown)
    defense: dict | None = None
    research: dict | None = None   # the defender's technology levels, when the report showed them

    @property
    def composition_known(self) -> bool:
        return self.ships is not None or self.defense is not None

    def all_defenders(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        for part in (self.ships, self.defense):
            for name, n in (part or {}).items():
                if int(n or 0) > 0:
                    merged[name] = merged.get(name, 0) + int(n)
        return merged


def coord_of(galaxy: int, system: int, position: int) -> Coord:
    return (int(galaxy), int(system), int(position))


def distance(a: Coord, b: Coord) -> int:
    """Rough travel distance between two coordinates, in systems (a galaxy jump counts as a lot)."""
    return abs(a[0] - b[0]) * SYSTEMS_PER_GALAXY + abs(a[1] - b[1]) + (0 if a[:2] != b[:2] else abs(a[2] - b[2]) // 5)


# ---- targets --------------------------------------------------------------------------------------------------
def pick_target(
    persona: Persona, rng: random.Random, me: Coord, my_score: int, players: list[dict], memory: Memory, now: float,
    *, allowed_ids: set[int] | None, cooldown: float, exclude_user_id: int | None = None,
) -> dict | None:
    """Choose a neighbour to spy on. `players` are rows of GET /highscore. `allowed_ids` None = anyone."""
    options = []
    for p in players:
        if p.get("is_me") or p["user_id"] == exclude_user_id:
            continue
        if allowed_ids is not None and p["user_id"] not in allowed_ids:
            continue
        if p["score"] > max(my_score, 1) * persona.max_target_ratio + SCORE_SLACK:
            continue
        c = coord_of(p["main_planet"]["galaxy"], p["main_planet"]["system"], p["main_planet"]["position"])
        if now < memory.avoid_until.get(c, 0) or now - memory.spied.get(c, -1e12) < cooldown:
            continue
        d = distance(me, c)
        if d > persona.target_range:
            continue
        options.append((d, p, c))
    if not options:
        return None
    options.sort(key=lambda t: t[0])
    # Among the few nearest, pick one at random so agents do not all hammer the same neighbour.
    d, p, c = rng.choice(options[:3])
    return {**p, "coord": c, "distance": d}


# ---- reading a spy report -------------------------------------------------------------------------------------
def summarize_report(report: dict) -> Intel:
    """Boil an espionage_report (see docs/API.md) down to what matters for an attack decision."""
    res = report.get("resources") or {}
    total = sum(int(res.get(r, 0) or 0) for r in RESOURCES)
    ships, defense = report.get("ships"), report.get("defense")
    defenders = None
    if ships is not None or defense is not None:
        defenders = sum(int(v or 0) for v in (ships or {}).values()) + sum(int(v or 0) for v in (defense or {}).values())
    return Intel(loot=int(total * LOOT_SHARE), defenders=defenders, owner_id=(report.get("target") or {}).get("owner_id"),
                 ships=ships, defense=defense, research=report.get("research") or None)


def assess_attack(persona: Persona, rng: random.Random, intel: Intel, my_fighters: int,
                  risk_scale: float = 1.0, at_risk: float | None = None) -> tuple[bool, str]:
    """Decide whether this target is worth attacking with `my_fighters` fighters. Returns (go, reason)."""
    if my_fighters < 1:
        return False, "no fighters"
    if intel.loot < persona.min_loot:
        return False, f"loot {intel.loot} below minimum {persona.min_loot}"
    if intel.defenders is None:
        if at_risk is not None and at_risk > GAMBLE_MAX_RISK * intel.loot:
            return False, f"defenses unknown and my fleet (worth {at_risk:,.0f}) is worth too much to gamble for {intel.loot:,}"
        if rng.random() < persona.gamble:
            return True, "defenses unknown, gambling"
        return False, "defenses unknown"
    if not intel.composition_known and intel.defenders > my_fighters * persona.risk_ratio * risk_scale:
        return False, f"{intel.defenders} defenders vs {my_fighters} fighters is too risky"
    if rng.random() >= persona.aggression:
        return False, "not in the mood"
    return True, f"loot {intel.loot}, {intel.defenders} defenders vs {my_fighters} fighters"


def build_strike(persona: Persona, ships_here: dict[str, int], ship_stats: dict[str, dict], loot: int) -> dict[str, int] | None:
    """Fighters to send plus enough cargo ships to carry `loot`. None if there is nothing to fight with."""
    fleet = {}
    for name in FIGHTERS:
        n = int(ships_here.get(name, 0) * persona.strike_fraction)
        if n > 0:
            fleet[name] = n
    if not fleet:
        return None

    def capacity(name: str) -> int:
        return int(ship_stats.get(name, {}).get("cargo_capacity", 0))

    carried = sum(n * capacity(name) for name, n in fleet.items())
    for name in CARGOS:
        if carried >= loot:
            break
        cap = capacity(name)
        have = int(ships_here.get(name, 0))
        if cap > 0 and have > 0:
            n = min(have, -(-(loot - carried) // cap))  # ceiling division
            fleet[name] = n
            carried += n * cap
    return fleet


def _sum_values(d) -> int:
    return sum(int(v or 0) for v in d.values()) if isinstance(d, dict) else 0


def summarize_battle(report: dict, my_user_id: int | None) -> dict | None:
    """Boil a battle_report (see docs/API.md) down to: who I was, what each side lost, what I took.
    Returns None if the report is not about me. Resource losses are the game's own value of the destroyed units."""
    att, dfn = report.get("attacker") or {}, report.get("defender") or {}
    if my_user_id is not None and att.get("player_id") == my_user_id:
        role = "attacker"
    elif my_user_id is not None and dfn.get("player_id") == my_user_id:
        role = "defender"
    else:
        return None
    units = att.get("units") or {}
    probes_only = bool(units) and all(name == "espionage_probe" for name in units)
    t = report.get("target") or {}
    loot = report.get("loot") or {}
    debris = report.get("debris") or {}
    mine, theirs = (att, dfn) if role == "attacker" else (dfn, att)
    return {
        "role": role, "probes_only": probes_only,
        "at": f"{t.get('galaxy')}:{t.get('system')}:{t.get('position')}" if t else None,
        "coord": (t.get("galaxy"), t.get("system"), t.get("position")) if t else None,
        "my_loss": int(mine.get("resource_loss") or 0), "their_loss": int(theirs.get("resource_loss") or 0),
        "loot": sum(int(loot.get(r) or 0) for r in RESOURCES),
        "debris": sum(int(debris.get(r) or 0) for r in RESOURCES),
        "sent_units": _sum_values(units), "rounds": report.get("rounds"),
    }


def judge_trade(b: dict) -> str:
    """'bad' if an attack cost far more than it gained (loot + what it destroyed), 'good' if clearly profitable."""
    gained = b["loot"] + b["their_loss"]
    if b["my_loss"] > 0 and b["my_loss"] >= 2 * gained:
        return "bad"
    if gained > 0 and b["loot"] > 0 and b["my_loss"] * 2 <= gained:
        return "good"
    return "neutral"


def required_win_chance(persona: Persona, risk_scale: float = 1.0) -> float:
    """How sure of winning an agent wants to be. Bold personalities (high risk_ratio) accept coin flips, careful ones
    want near certainty; after costly defeats (risk_scale below 1) everybody wants more certainty."""
    return min(0.95, max(0.4, 0.95 - 0.45 * persona.risk_ratio * risk_scale))


def judge_fight(persona: Persona, rng: random.Random, strike: dict[str, int], intel: Intel, my_tech: Tech | None,
                haul: int, risk_scale: float = 1.0) -> tuple[bool, str, Estimate | None]:
    """Simulate the fight with the fleet we would really send. Returns (go, reason, estimate). When the report did not
    show the defenders' composition there is nothing to simulate: (True, '', None) and the old rules decide."""
    if not intel.composition_known:
        return True, "", None
    est = estimate_battle(strike, intel.all_defenders(), rng, my_tech or (0, 0, 0), tech_from_research(intel.research))
    gain = est.win_chance * haul + 0.5 * est.their_loss_value - est.my_loss_value
    facts = f"win chance {est.win_chance:.0%}, expected loss {est.my_loss_value:,.0f} for loot {haul:,}"
    need = required_win_chance(persona, risk_scale)
    if est.win_chance < need:
        return False, f"simulated fight: {facts}; wants at least {need:.0%}", est
    if gain <= 0:
        return False, f"simulated fight: {facts}; not worth it", est
    return True, f"simulated fight: {facts}", est


def carrying_capacity(fleet: dict[str, int], ship_stats: dict[str, dict]) -> int:
    return sum(int(n) * int(ship_stats.get(name, {}).get("cargo_capacity", 0)) for name, n in fleet.items())


def can_carry_enough(persona: Persona, fleet: dict[str, int], ship_stats: dict[str, dict], loot: int) -> bool:
    """An attack only pays if the fleet can carry back a decent part of the loot (at least an eighth of it)."""
    haul = min(loot, carrying_capacity(fleet, ship_stats))
    return haul >= max(persona.min_loot, loot // 8)


def count_fighters(ships_here: dict[str, int]) -> int:
    return sum(int(ships_here.get(n, 0)) for n in FIGHTERS)


def pick_attack(persona: Persona, rng: random.Random, memory: Memory, now: float, ships_here: dict[str, int]) -> tuple[Coord, Intel, str] | None:
    """Look through fresh spy reports for a target worth attacking now."""
    fighters = count_fighters(ships_here)
    best = None
    for c, info in memory.intel.items():
        if now - info["at"] > INTEL_MAX_AGE or now - memory.attacked.get(c, -1e12) < persona.attack_cooldown_s:
            continue
        if info.get("skip_until", 0) > now:  # skipped a moment ago (cannot carry the loot): do not retry every round
            continue
        intel = Intel(info["loot"], info["defenders"], info.get("owner_id"), info.get("ships"), info.get("defense"),
                      info.get("research"))
        risk = sum(unit_value(n) * int(ships_here.get(n, 0) * persona.strike_fraction) for n in FIGHTERS)
        go, reason = assess_attack(persona, rng, intel, fighters, memory.risk_scale, at_risk=risk)
        if go and (best is None or intel.loot > best[1].loot):
            best = (c, intel, reason)
    return best


# ---- what to build --------------------------------------------------------------------------------------------
def choose_units(
    persona: Persona, rng: random.Random, units: list[dict], have: dict, ships_here: dict[str, int],
    want_cargo: bool = False,
) -> tuple[str, int] | None:
    """Pick one ship or defense type to order and how many, spending at most `army_share` of what is spare."""
    if persona.army_share <= 0 and persona.probes_wanted <= 0:
        return None
    usable = {u["machine_name"]: u for u in units if u["requirements_met"] and u["character_class_met"] and u["max_buildable"] > 0}

    # Keep a small stock of probes if the persona spies (they are cheap).
    probes = ships_here.get("espionage_probe", 0)
    if probes < persona.probes_wanted and "espionage_probe" in usable:
        return "espionage_probe", min(persona.probes_wanted - probes, usable["espionage_probe"]["max_buildable"])

    if persona.army_share <= 0:
        return None
    weighted = [(persona.unit_weights.get(n, 0) * (1 + persona.randomness * (rng.random() * 2 - 1)), n) for n in usable]
    weighted = [(w, n) for w, n in weighted if w > 0 and n != "espionage_probe"]
    if want_cargo:  # worthwhile loot is lying around but the fleet cannot carry it: cargo ships first
        cargos = [(w, n) for w, n in weighted if n in CARGOS] or [(1.0, n) for n in CARGOS if n in usable]
        if cargos:
            weighted = [max(cargos)]
    if not weighted:
        return None
    weighted.sort(reverse=True)
    name = weighted[0][1]
    cost = usable[name]["cost_per_unit"]
    share = max(persona.army_share, CARGO_SHARE) if want_cargo and name in CARGOS else persona.army_share
    limits = [int(have.get(r, 0) * share // cost[r]) for r in RESOURCES if cost.get(r, 0) > 0]
    amount = min([usable[name]["max_buildable"], *limits]) if limits else 0
    if want_cargo and name in CARGOS:
        amount = min(amount, 10)
    return (name, amount) if amount >= 1 else None
