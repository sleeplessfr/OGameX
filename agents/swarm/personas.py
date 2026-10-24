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


ECONOMY = {"metal_mine": 3.0, "crystal_mine": 2.5, "deuterium_synthesizer": 1.5, "solar_plant": 2.0,
           "robot_factory": 1.5, "metal_store": 0.7, "crystal_store": 0.7, "deuterium_store": 0.5,
           "research_lab": 1.2, "shipyard": 0.8, "fusion_plant": 0.8}

PRESETS: dict[str, Persona] = {
    "miner": Persona(
        "miner", "Pure economy: mines first, research only when rich.",
        {**ECONOMY, "metal_mine": 4.0, "crystal_mine": 3.0, "robot_factory": 1.5, "research_lab": 0.6, "shipyard": 0.2},
        {"energy_technology": 1.0, "computer_technology": 0.5, "combustion_drive": 0.2},
        balance=0.8, patience=0.85, research_share=0.4, tick_seconds=60, max_save_seconds=1800),
    "scientist": Persona(
        "scientist", "Rushes the research lab and climbs the tech tree.",
        {**ECONOMY, "research_lab": 4.0, "crystal_mine": 3.0, "metal_mine": 2.0, "shipyard": 0.4},
        {"energy_technology": 3.0, "computer_technology": 2.0, "laser_technology": 2.0, "ion_technology": 1.2,
         "hyperspace_technology": 1.5, "espionage_technology": 1.0, "combustion_drive": 1.0,
         "impulse_drive": 1.0, "astrophysics": 1.5, "plasma_technology": 1.0},
        balance=1.2, patience=0.6, research_share=1.0, tick_seconds=45, max_save_seconds=7200),
    "turtle": Persona(
        "turtle", "Defensive and safe: big stores, steady growth, never overspends.",
        {**ECONOMY, "metal_store": 2.0, "crystal_store": 2.0, "deuterium_store": 1.5, "robot_factory": 2.0,
         "shipyard": 1.0},
        {"shielding_technology": 2.0, "armor_technology": 2.0, "energy_technology": 1.5, "weapon_technology": 0.8,
         "computer_technology": 1.0},
        balance=1.5, patience=0.9, store_alarm=0.75, research_share=0.7, tick_seconds=75, max_save_seconds=7200),
    "raider": Persona(
        "raider", "Wants ships early: shipyard and drives, economy only as a means.",
        {**ECONOMY, "shipyard": 3.5, "robot_factory": 2.0, "deuterium_synthesizer": 2.5, "research_lab": 1.5},
        {"combustion_drive": 3.0, "impulse_drive": 2.0, "weapon_technology": 2.0, "armor_technology": 1.5,
         "shielding_technology": 1.0, "espionage_technology": 2.0, "energy_technology": 1.0},
        balance=0.7, randomness=0.35, patience=0.4, research_share=0.8, tick_seconds=30, max_save_seconds=900),
    "balanced": Persona(
        "balanced", "A bit of everything, like an average player.",
        dict(ECONOMY),
        {"energy_technology": 2.0, "computer_technology": 1.5, "laser_technology": 1.0, "combustion_drive": 1.0,
         "espionage_technology": 0.8, "weapon_technology": 0.6, "armor_technology": 0.6, "shielding_technology": 0.6},
        balance=1.0, patience=0.65, research_share=0.7, tick_seconds=45),
}


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
    )
