"""Personalities. A persona is a set of preferences; the brain turns them into decisions.

All weights are relative: a building with weight 3 is picked about three times as eagerly as one with weight 1.
Weight 0 (or missing) means "never build this".
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Persona:
    name: str
    description: str
    building_weights: dict[str, float]
    research_weights: dict[str, float]
    balance: float = 1.0        # >0: spreads upgrades over many buildings (higher = more even). 0 = pure favourites.
    randomness: float = 0.2     # 0..1 noise on the scores, so two agents with the same persona still differ
    patience: float = 0.7       # 0..1 chance to save up for the best target instead of buying something lesser
    energy_buffer: int = 0      # build a solar plant when energy drops to this level or below
    store_alarm: float = 0.9    # build a store when a resource is this full (fraction of storage)
    tick_seconds: float = 45.0  # typical pause between two looks at the game when nothing is pending
    research_share: float = 1.0  # 0..1 how keen the agent is to research at all
    max_save_seconds: float = 3600.0  # never save up for longer than this; buy something else instead
    caution: float = 0.0        # 0..1 share of the stock turned into defence when a hostile fleet is on its way
    # --- military / social traits (all default to "peaceful") ---
    aggression: float = 0.0      # 0..1 chance to attack when a good target is found (0 = never attacks)
    scout_interest: float = 0.0  # 0..1 chance to send spy probes at a neighbour when idle
    army_share: float = 0.0      # 0..1 share of spare resources spent on ships/defense each round
    unit_weights: dict[str, float] = field(default_factory=dict)  # which ships/defense it likes to build
    probes_wanted: int = 0       # espionage probes it keeps in stock
    probes_per_spy: int = 1      # probes sent per spying trip
    risk_ratio: float = 0.5      # attacks if defenders <= own fighters * risk_ratio
    gamble: float = 0.2          # chance to attack a target whose defenses are unknown
    min_loot: int = 500          # ignores targets that would give less than this
    strike_fraction: float = 0.8  # share of its fighters it sends on an attack
    max_target_ratio: float = 1.0  # only targets with score <= own score * this (+ a small slack)
    target_range: int = 400      # how far it looks, in "system distance" (one galaxy = 500)
    spy_cooldown_s: float = 3600.0     # does not spy on the same target again before this
    attack_cooldown_s: float = 7200.0  # does not hit the same target again before this


ECONOMY = {"metal_mine": 3.0, "crystal_mine": 2.5, "deuterium_synthesizer": 1.5, "solar_plant": 2.0,
           "robot_factory": 1.5, "metal_store": 0.7, "crystal_store": 0.7, "deuterium_store": 0.5,
           "research_lab": 1.2, "shipyard": 0.8, "fusion_plant": 0.8}

PRESETS: dict[str, Persona] = {
    "miner": Persona(
        "miner", "Pure economy: mines first, research only when rich.",
        {**ECONOMY, "metal_mine": 4.0, "crystal_mine": 3.0, "robot_factory": 1.5, "research_lab": 0.6, "shipyard": 0.2},
        {"energy_technology": 1.0, "computer_technology": 0.5, "combustion_drive": 0.2},
        balance=0.8, patience=0.85, research_share=0.4, tick_seconds=60, max_save_seconds=1800, caution=0.6,
        aggression=0.05, scout_interest=0.1, army_share=0.05, unit_weights={"small_cargo": 1.0, "espionage_probe": 1.0},
        probes_wanted=1, min_loot=2000, risk_ratio=0.3, max_target_ratio=0.8),
    "scientist": Persona(
        "scientist", "Rushes the research lab and climbs the tech tree.",
        {**ECONOMY, "research_lab": 4.0, "crystal_mine": 3.0, "metal_mine": 2.0, "shipyard": 0.4},
        {"energy_technology": 3.0, "computer_technology": 2.0, "laser_technology": 2.0, "ion_technology": 1.2,
         "hyperspace_technology": 1.5, "espionage_technology": 1.0, "combustion_drive": 1.0,
         "impulse_drive": 1.0, "astrophysics": 1.5, "plasma_technology": 1.0},
        balance=1.2, patience=0.6, research_share=1.0, tick_seconds=45, max_save_seconds=7200, caution=0.5,
        aggression=0.0, scout_interest=0.4, army_share=0.1, unit_weights={"espionage_probe": 3.0, "small_cargo": 1.0},
        probes_wanted=3, probes_per_spy=2, spy_cooldown_s=7200),
    "turtle": Persona(
        "turtle", "Defensive and safe: big stores, steady growth, never overspends.",
        {**ECONOMY, "metal_store": 2.0, "crystal_store": 2.0, "deuterium_store": 1.5, "robot_factory": 2.0,
         "shipyard": 1.0},
        {"shielding_technology": 2.0, "armor_technology": 2.0, "energy_technology": 1.5, "weapon_technology": 0.8,
         "computer_technology": 1.0},
        balance=1.5, patience=0.9, store_alarm=0.75, research_share=0.7, tick_seconds=75, max_save_seconds=7200, caution=0.9,
        aggression=0.0, scout_interest=0.05, army_share=0.3,
        unit_weights={"rocket_launcher": 3.0, "light_laser": 2.0, "heavy_laser": 1.0, "small_shield_dome": 1.0,
                      "espionage_probe": 0.5},
        probes_wanted=1),
    "raider": Persona(
        "raider", "Wants ships early: shipyard and drives, economy only as a means.",
        {**ECONOMY, "shipyard": 3.5, "robot_factory": 2.0, "deuterium_synthesizer": 2.5, "research_lab": 1.5},
        {"combustion_drive": 3.0, "impulse_drive": 2.0, "weapon_technology": 2.0, "armor_technology": 1.5,
         "shielding_technology": 1.0, "espionage_technology": 2.0, "energy_technology": 1.0},
        balance=0.7, randomness=0.35, patience=0.4, research_share=0.8, tick_seconds=30, max_save_seconds=900, caution=0.3,
        aggression=0.85, scout_interest=0.8, army_share=0.5,
        unit_weights={"light_fighter": 3.0, "cruiser": 2.0, "heavy_fighter": 1.5, "small_cargo": 1.5, "large_cargo": 1.0,
                      "espionage_probe": 1.0},
        probes_wanted=3, probes_per_spy=2, risk_ratio=1.0, gamble=0.35, min_loot=400, strike_fraction=0.9,
        max_target_ratio=1.3, spy_cooldown_s=1800, attack_cooldown_s=3600),
    "balanced": Persona(
        "balanced", "A bit of everything, like an average player.",
        dict(ECONOMY),
        {"energy_technology": 2.0, "computer_technology": 1.5, "laser_technology": 1.0, "combustion_drive": 1.0,
         "espionage_technology": 0.8, "weapon_technology": 0.6, "armor_technology": 0.6, "shielding_technology": 0.6},
        balance=1.0, patience=0.65, research_share=0.7, tick_seconds=45, caution=0.5,
        aggression=0.3, scout_interest=0.4, army_share=0.2,
        unit_weights={"light_fighter": 1.0, "small_cargo": 1.0, "espionage_probe": 1.0, "rocket_launcher": 1.0},
        probes_wanted=2, risk_ratio=0.6, min_loot=800),
}


# Economy weights found by tune_economy.py (offline search with the game's formulas; see README). Only the means of the
# economy were tuned (mines, solar plant, robot factory, spare power); stores, balance, patience and the rest are the
# personality's own. Each personality was scored on the production mix it would spend: standard values for miner and
# balanced, crystal and deuterium for the scientist, metal for the turtle, metal and crystal for the raider. Measured
# gain after 24 game hours at x100: miner +3.7%, scientist +15.0%, turtle +153.7%, raider +29.5%, balanced +19.0%.
HAND_MADE = dict(PRESETS)   # the personalities as first written, before the tuned economy weights below
TUNED_ECONOMY = {
    "miner": ({'metal_mine': 7.356, 'crystal_mine': 4.254, 'deuterium_synthesizer': 2.195, 'solar_plant': 1.397, 'robot_factory': 1.523}, 0),
    "scientist": ({'metal_mine': 6.0, 'crystal_mine': 3.648, 'deuterium_synthesizer': 1.895, 'solar_plant': 0.79, 'robot_factory': 1.042}, 10),
    "turtle": ({'metal_mine': 9.0, 'crystal_mine': 5.505, 'deuterium_synthesizer': 1.154, 'solar_plant': 0.667, 'robot_factory': 1.002}, 46),
    "raider": ({'metal_mine': 6.219, 'crystal_mine': 2.826, 'deuterium_synthesizer': 2.288, 'solar_plant': 2.527, 'robot_factory': 2.135}, 7),
    "balanced": ({'metal_mine': 7.651, 'crystal_mine': 3.929, 'deuterium_synthesizer': 1.422, 'solar_plant': 1.36, 'robot_factory': 0.987}, 14),
}
PRESETS = {name: replace(p, building_weights={**p.building_weights, **TUNED_ECONOMY[name][0]},
                         energy_buffer=TUNED_ECONOMY[name][1]) for name, p in PRESETS.items()}


def make_persona(index: int, rng: random.Random, names: list[str] | None = None) -> Persona:
    """Persona for the index-th agent: presets in rotation, each one slightly perturbed so no two agents are twins."""
    order = names or list(PRESETS)
    base = PRESETS[order[index % len(order)]]

    def jitter(weights: dict[str, float]) -> dict[str, float]:
        return {k: max(0.0, v * rng.uniform(0.7, 1.3)) for k, v in weights.items()}

    return replace(
        base,
        name=f"{base.name}",
        building_weights=jitter(base.building_weights),
        research_weights=jitter(base.research_weights),
        balance=max(0.0, base.balance * rng.uniform(0.8, 1.2)),
        patience=min(1.0, max(0.0, base.patience + rng.uniform(-0.15, 0.15))),
        tick_seconds=base.tick_seconds * rng.uniform(0.8, 1.3),
        aggression=min(1.0, max(0.0, base.aggression * rng.uniform(0.8, 1.2))),
        unit_weights=jitter(base.unit_weights),
        risk_ratio=max(0.05, base.risk_ratio * rng.uniform(0.8, 1.25)),
        # drawn LAST, so that adding it did not change any earlier draw: existing agents keep their personalities
        caution=min(1.0, max(0.0, base.caution + rng.uniform(-0.15, 0.15))) if base.caution > 0 else 0.0,
    )
