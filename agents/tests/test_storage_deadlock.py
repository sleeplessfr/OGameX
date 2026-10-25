import random
import unittest
from dataclasses import replace

from swarm.brain import choose_building
from swarm.personas import PRESETS

MINER = replace(PRESETS["miner"], randomness=0.0, patience=1.0)


def item(name, cost, level=3):
    return {"machine_name": name, "level": level, "requirements_met": True, "can_afford": True,
            "cost": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}}


def planet(cap, energy=-50):
    return {"production_per_hour": {"metal": 1000, "crystal": 500, "deuterium": 200}, "energy": energy,
            "storage": {"metal": cap, "crystal": cap, "deuterium": cap}, "fields": {"used": 10, "max": 160}}


class StorageDeadlockTest(unittest.TestCase):
    def pick(self, p, have, solar_cost):
        buildings = [item("metal_mine", (100, 50, 0)), item("solar_plant", solar_cost),
                     item("metal_store", (1000, 0, 0), level=2), item("crystal_store", (1000, 500, 0), level=2)]
        return choose_building(MINER, random.Random(1), p, buildings, research_busy=False, units_busy=False, have=have)

    def test_a_solar_plant_dearer_than_the_store_makes_it_build_the_store_first(self):
        have = {"metal": 100_000, "crystal": 100_000, "deuterium": 100_000}      # the stock sits at the cap
        c = self.pick(planet(100_000), have, solar_cost=(30_000, 120_000, 0))
        self.assertEqual(c.machine_name, "crystal_store")
        self.assertIn("costs more crystal than the store holds", c.reason)

    def test_an_affordable_solar_plant_is_still_built_at_once(self):
        have = {"metal": 100_000, "crystal": 100_000, "deuterium": 100_000}
        c = self.pick(planet(100_000), have, solar_cost=(30_000, 40_000, 0))
        self.assertEqual(c.machine_name, "solar_plant")

    def test_without_a_store_to_build_it_waits_as_before(self):
        have = {"metal": 100_000, "crystal": 100_000, "deuterium": 100_000}
        buildings = [item("solar_plant", (30_000, 120_000, 0))]
        c = choose_building(MINER, random.Random(1), planet(100_000), buildings, research_busy=False, units_busy=False, have=have)
        self.assertIsNone(c.machine_name)
        self.assertIn("waiting for solar_plant", c.reason)
