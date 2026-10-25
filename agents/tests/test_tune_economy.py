import unittest

import tune_economy as te
from swarm.personas import PRESETS


class PlanetFormulaTests(unittest.TestCase):
    def test_costs_production_and_build_time_follow_the_game(self):
        self.assertEqual(te.price("metal_mine", 0), {"metal": 60, "crystal": 15, "deuterium": 0})
        self.assertEqual(te.price("metal_mine", 2), {"metal": 135, "crystal": 33, "deuterium": 0})
        p = te.Planet(speed=1, start=0)
        p.levels.update(metal_mine=10, solar_plant=20)
        self.assertAlmostEqual(p.production()["metal"], 30 + 30 * 10 * 1.1 ** 10, places=3)
        self.assertEqual(p.storage()["metal"], 10000)
        # level 1 metal mine, no robots, speed 1: (60+15) / (2500 * 3.5) hours, at least 1 second
        q = te.Planet(speed=1, start=0)
        self.assertEqual(q.build_seconds("metal_mine"), max(1, int(75 / (2500 * 3.5) * 3600)))

    def test_missing_energy_throttles_the_mines(self):
        p = te.Planet(speed=1, start=0)
        p.levels.update(metal_mine=10)
        self.assertAlmostEqual(p.production()["metal"], 30, places=3)   # only the free base output without power

    def test_a_game_is_repeatable_and_grows_with_time(self):
        persona = PRESETS["miner"]
        a = te.play(persona, 5, 6, 100, 100_000)
        self.assertEqual(a, te.play(persona, 5, 6, 100, 100_000))
        self.assertGreater(te.play(persona, 5, 24, 100, 100_000), a)


if __name__ == "__main__":
    unittest.main()


class GoalTests(unittest.TestCase):
    def test_each_personality_values_its_own_resource_mix(self):
        p = te.Planet(speed=1, start=0)
        p.levels.update(metal_mine=5, crystal_mine=5, solar_plant=20)
        miner = te.income_value(p, te.GOAL_VALUES["miner"])
        scientist = te.income_value(p, te.GOAL_VALUES["scientist"])
        turtle = te.income_value(p, te.GOAL_VALUES["turtle"])
        self.assertNotEqual(miner, scientist)
        self.assertGreater(turtle, scientist)   # a metal-heavy planet suits a turtle better than a scientist
        for name in ("miner", "scientist", "turtle", "raider", "balanced"):
            self.assertIn(name, te.GOAL_VALUES)

    def test_tuning_starts_from_the_hand_made_personality(self):
        from swarm.personas import HAND_MADE, PRESETS
        self.assertNotEqual(HAND_MADE["turtle"].building_weights["metal_mine"], PRESETS["turtle"].building_weights["metal_mine"])
