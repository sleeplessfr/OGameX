import random
import unittest

from swarm import brain
from swarm.personas import PRESETS


def item(name, level, cost, afford=True, met=True):
    return {"machine_name": name, "level": level, "cost": cost, "can_afford": afford, "requirements_met": met}


class TerraformerPower(unittest.TestCase):
    def planet(self, left=10):
        return {"production_per_hour": {"metal": 1000, "crystal": 1000, "deuterium": 1000}, "energy": 500,
                "storage": {"metal": 10 ** 9, "crystal": 10 ** 9, "deuterium": 10 ** 9}, "fields": {"used": 160 - left, "max": 160}}

    def test_missing_energy_means_more_power_first(self):
        rich = {"metal": 10 ** 7, "crystal": 10 ** 7, "deuterium": 10 ** 7}
        items = [item("terraformer", 4, {"metal": 0, "crystal": 400000, "deuterium": 800000, "energy": 8000}, afford=False),
                 item("solar_plant", 20, {"metal": 5000, "crystal": 2000, "deuterium": 0}),
                 item("metal_mine", 20, {"metal": 100, "crystal": 30, "deuterium": 0})]
        c = brain.choose_building(PRESETS["miner"], random.Random(1), self.planet(), items, research_busy=False, units_busy=False, have=rich)
        self.assertEqual(c.machine_name, "solar_plant")
        self.assertIn("energy produced", c.reason)

    def test_missing_resources_still_wait_for_the_terraformer(self):
        poor = {"metal": 10, "crystal": 10, "deuterium": 10}
        items = [item("terraformer", 4, {"metal": 0, "crystal": 400000, "deuterium": 800000, "energy": 8000}, afford=False),
                 item("solar_plant", 20, {"metal": 5000, "crystal": 2000, "deuterium": 0}, afford=False)]
        c = brain.choose_building(PRESETS["miner"], random.Random(1), self.planet(), items, research_busy=False, units_busy=False, have=poor)
        self.assertIsNone(c.machine_name)

    def test_affordable_terraformer_is_built(self):
        rich = {"metal": 10 ** 7, "crystal": 10 ** 7, "deuterium": 10 ** 7}
        items = [item("terraformer", 1, {"metal": 0, "crystal": 100000, "deuterium": 200000, "energy": 1000}),
                 item("solar_plant", 20, {"metal": 5000, "crystal": 2000, "deuterium": 0})]
        c = brain.choose_building(PRESETS["miner"], random.Random(1), self.planet(), items, research_busy=False, units_busy=False, have=rich)
        self.assertEqual(c.machine_name, "terraformer")


if __name__ == "__main__":
    unittest.main()
