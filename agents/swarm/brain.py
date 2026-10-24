"""Decision logic of an agent. Pure functions: game data in, a choice out. No network, so it is easy to test.

An agent thinks in rounds. Each round it asks: what is the most important thing for my personality that I can
start right now? If the best thing is too expensive, it either saves up for it (patient personalities) or buys
the best thing it can afford. It also returns how long it should wait before looking again.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .personas import Persona

RESOURCES = ("metal", "crystal", "deuterium")
STORE_FOR = {"metal": "metal_store", "crystal": "crystal_store", "deuterium": "deuterium_store"}
PRODUCER_OF = {"metal": "metal_mine", "crystal": "crystal_mine", "deuterium": "deuterium_synthesizer"}
ENERGY_USERS = {"metal_mine", "crystal_mine", "deuterium_synthesizer"}
ENERGY_BASE = {"metal_mine": 10, "crystal_mine": 10, "deuterium_synthesizer": 20}  # OGame: base * level * 1.1^level


def extra_energy_needed(machine_name: str, level: int) -> int:
    """Energy a mine will additionally draw when it goes from `level` to `level + 1` (0 for other buildings)."""
    base = ENERGY_BASE.get(machine_name)
    if base is None:
        return 0
    return int(base * ((level + 1) * 1.1 ** (level + 1) - level * 1.1 ** level)) + 1


@dataclass
class Choice:
    machine_name: str | None      # what to start (None = nothing now)
    wait_seconds: float | None    # when it would make sense to look again (None = no idea)
    reason: str


def seconds_to_afford(cost: dict, have: dict, production_per_hour: dict) -> float | None:
    """Time until `have` covers `cost` at the current production. None if it never will."""
    worst = 0.0
    for r in RESOURCES:
        missing = cost.get(r, 0) - have.get(r, 0)
        if missing <= 0:
            continue
        rate = production_per_hour.get(r, 0)
        if rate <= 0:
            return None
        worst = max(worst, missing / rate * 3600.0)
    return worst


def can_pay(cost: dict, have: dict) -> bool:
    return all(cost.get(r, 0) <= have.get(r, 0) for r in RESOURCES)


def pay(cost: dict, have: dict) -> None:
    for r in RESOURCES:
        have[r] = have.get(r, 0) - cost.get(r, 0)


def _score(item: dict, weights: dict[str, float], persona: Persona, rng: random.Random) -> float:
    w = weights.get(item["machine_name"], 0.0)
    if w <= 0:
        return 0.0
    noise = 1.0 + persona.randomness * (rng.random() * 2 - 1)
    return w * noise / ((item["level"] + 1) ** persona.balance)


def _pick(
    ranked: list[dict], persona: Persona, rng: random.Random, have: dict, production: dict, what: str
) -> Choice:
    """Take the top-ranked item; if unaffordable, save up (with probability = patience) or take the best affordable."""
    if not ranked:
        return Choice(None, None, f"no {what} candidate")

    def affordable(it: dict) -> bool:
        return bool(it.get("can_afford", True)) and can_pay(it["cost"], have)

    top = ranked[0]
    if affordable(top):
        return Choice(top["machine_name"], None, f"best {what}: {top['machine_name']}")

    wait = seconds_to_afford(top["cost"], have, production)
    # Saving up only makes sense for a target that is reachable in a reasonable time. Otherwise (no production
    # of a needed resource, or hours away) keep growing with whatever is affordable now.
    reachable = wait is not None and wait <= persona.max_save_seconds
    if reachable and rng.random() < persona.patience:
        return Choice(None, wait, f"saving up for {top['machine_name']}")

    for it in ranked[1:]:
        if affordable(it):
            return Choice(it["machine_name"], None, f"cannot afford {top['machine_name']}, took {it['machine_name']}")
    return Choice(None, wait, f"cannot afford anything, nearest: {top['machine_name']}")


def choose_building(
    persona: Persona, rng: random.Random, planet: dict, buildings: list[dict],
    *, research_busy: bool, units_busy: bool, have: dict,
) -> Choice:
    """Pick the next building for one planet. `have` is the resources still free this round."""
    production = planet["production_per_hour"]
    usable = [b for b in buildings if b["requirements_met"]]
    # The game refuses these while research / unit production is running.
    if research_busy:
        usable = [b for b in usable if b["machine_name"] != "research_lab"]
    if units_busy:
        usable = [b for b in usable if b["machine_name"] not in ("shipyard", "nano_factory")]
    by_name = {b["machine_name"]: b for b in usable}

    # 1. Energy: mines starve without it. Nothing else matters until the power is back.
    if planet["energy"] <= persona.energy_buffer and "solar_plant" in by_name:
        return _forced(by_name["solar_plant"], have, production, "low energy")

    # 2. A store that is nearly full wastes production.
    for r in RESOURCES:
        storage = planet["storage"].get(r, 0)
        if storage > 0 and have.get(r, 0) >= persona.store_alarm * storage and STORE_FOR[r] in by_name:
            return _forced(by_name[STORE_FOR[r]], have, production, f"{r} storage almost full")

    # 3. Otherwise follow the personality.
    scored = [(_score(b, persona.building_weights, persona, rng), b) for b in usable]
    ranked = [b for s, b in sorted(scored, key=lambda t: -t[0]) if s > 0]
    # Do not pile on energy consumers while the power is already negative.
    if planet["energy"] < 0:
        ranked = [b for b in ranked if b["machine_name"] not in ENERGY_USERS]
    # Look ahead: if the favourite is a mine that would push the power negative, build the solar plant first.
    if ranked and "solar_plant" in by_name:
        top = ranked[0]
        if planet["energy"] - extra_energy_needed(top["machine_name"], top["level"]) < persona.energy_buffer:
            return _forced(by_name["solar_plant"], have, production, f"power for {top['machine_name']}")
    # Dependency: the favourite needs a resource we do not produce at all -> build what produces it first.
    if ranked and not can_pay(ranked[0]["cost"], have) and seconds_to_afford(ranked[0]["cost"], have, production) is None:
        for r in RESOURCES:
            if ranked[0]["cost"].get(r, 0) > have.get(r, 0) and production.get(r, 0) <= 0:
                producer = by_name.get(PRODUCER_OF[r])
                if producer is not None:
                    return _forced(producer, have, production, f"{ranked[0]['machine_name']} needs {r}")
    return _pick(ranked, persona, rng, have, production, "building")


def choose_research(
    persona: Persona, rng: random.Random, planet: dict, research: list[dict], *, have: dict,
) -> Choice:
    production = planet["production_per_hour"]
    usable = [t for t in research if t["requirements_met"]]
    scored = [(_score(t, persona.research_weights, persona, rng), t) for t in usable]
    ranked = [t for s, t in sorted(scored, key=lambda x: -x[0]) if s > 0]
    return _pick(ranked, persona, rng, have, production, "research")


def _forced(item: dict, have: dict, production: dict, why: str) -> Choice:
    """A must-do item: build it if payable, otherwise wait for it (never buy something else instead)."""
    if bool(item.get("can_afford", True)) and can_pay(item["cost"], have):
        return Choice(item["machine_name"], None, f"{why}: {item['machine_name']}")
    return Choice(None, seconds_to_afford(item["cost"], have, production), f"{why}: waiting for {item['machine_name']}")

