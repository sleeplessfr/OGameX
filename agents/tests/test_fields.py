import random
import unittest
from dataclasses import replace

from swarm.brain import FIELD_ALARM, choose_building, choose_research, fields_left
from swarm.personas import PRESETS

MINER = replace(PRESETS["miner"], randomness=0.0, patience=1.0)
RICH = {"metal": 10 ** 9, "crystal": 10 ** 9, "deuterium": 10 ** 9}


def planet(left, energy=500):
    return {"production_per_hour": {"metal": 1000, "crystal": 500, "deuterium": 200}, "energy": energy,
            "storage": {"metal": 10 ** 12, "crystal": 10 ** 12, "deuterium": 10 ** 12}, "fields": {"used": 100 - left, "max": 100}}


def item(name, level=3, cost=(100, 50, 0), met=True):
    return {"machine_name": name, "level": level, "requirements_met": met, "can_afford": True,
            "cost": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}}


BUILDINGS = [item("metal_mine"), item("crystal_mine"), item("solar_plant"), item("metal_store", level=2), item("shipyard", level=2),
             item("robot_factory", level=4)]


class FieldTests(unittest.TestCase):
    def test_reads_the_free_fields(self):
        self.assertEqual(fields_left(planet(7)), 7)
        self.assertIsNone(fields_left({}))
        self.assertIsNone(fields_left({"fields": {"used": 3}}))

    def test_a_full_planet_builds_nothing_and_says_why(self):
        c = choose_building(MINER, random.Random(1), planet(0), BUILDINGS, research_busy=False, units_busy=False, have=RICH)
        self.assertIsNone(c.machine_name)
        self.assertTrue(c.full)
        self.assertIn("no free fields", c.reason)

    def test_without_field_information_nothing_changes(self):
        p = planet(50)
        del p["fields"]
        c = choose_building(MINER, random.Random(1), p, BUILDINGS, research_busy=False, units_busy=False, have=RICH)
        self.assertIsNotNone(c.machine_name)
        self.assertFalse(c.full)

    def test_runs_low_then_goes_for_the_terraformer(self):
        b = BUILDINGS + [item("terraformer", level=0, cost=(0, 50000, 100000))]
        c = choose_building(MINER, random.Random(1), planet(FIELD_ALARM), b, research_busy=False, units_busy=False, have=RICH)
        self.assertEqual(c.machine_name, "terraformer")
        c = choose_building(MINER, random.Random(1), planet(FIELD_ALARM + 1), b, research_busy=False, units_busy=False, have=RICH)
        self.assertNotEqual(c.machine_name, "terraformer")

    def test_the_last_fields_are_not_spent_on_minor_upgrades(self):
        weights = {"metal_store": 99.0, "shipyard": 99.0}   # a personality that loves them
        keen = replace(MINER, building_weights={**MINER.building_weights, **weights})
        for seed in range(10):
            c = choose_building(keen, random.Random(seed), planet(2), BUILDINGS, research_busy=False, units_busy=False, have=RICH)
            self.assertNotIn(c.machine_name, ("metal_store", "shipyard"), seed)

    def test_energy_technology_is_pushed_when_fields_run_low(self):
        techs = [{"machine_name": "energy_technology", "level": 5, "requirements_met": True, "can_afford": True,
                  "cost": {"metal": 0, "crystal": 100, "deuterium": 50}},
                 {"machine_name": "laser_technology", "level": 1, "requirements_met": True, "can_afford": True,
                  "cost": {"metal": 10, "crystal": 10, "deuterium": 0}}]
        lab = replace(PRESETS["scientist"], randomness=0.0, research_weights={"energy_technology": 0.1, "laser_technology": 3.0})
        calm = choose_research(lab, random.Random(1), planet(50), techs, have=RICH)
        tight = choose_research(lab, random.Random(1), planet(3), techs, have=RICH)
        self.assertEqual(calm.machine_name, "laser_technology")
        self.assertEqual(tight.machine_name, "energy_technology")


if __name__ == "__main__":
    unittest.main()


class FullPlanetInTheFakeGameTests(unittest.TestCase):
    def play(self, field_max, hours=30, levels=None, stock=None):
        import tempfile
        from pathlib import Path
        from swarm.agent import Agent, TokenStore
        from tests.fakegame import FakeGame
        game = FakeGame()
        game.field_max = field_max
        game.levels.update(levels or {})
        game.res.update(stock or {})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", replace(PRESETS["raider"], expansion=0.0), "x", store, random.Random(3), client=game)
        agent.log = lambda *a, **k: None
        agent.user_id = 1
        elapsed = 0.0
        while elapsed < hours * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        return game, agent

    def test_the_agent_stops_building_at_the_limit_without_being_refused(self):
        game, agent = self.play(field_max=12)
        self.assertEqual(sum(game.levels.values()), 12)
        self.assertEqual(getattr(game, "field_refusals", 0), 0)
        self.assertEqual(agent.stats["errors"], 0)

    def test_a_full_planet_still_gets_ships(self):
        game, agent = self.play(field_max=10, hours=20, levels={"shipyard": 1, "metal_mine": 5, "solar_plant": 4},
                                stock={"metal": 100_000, "crystal": 100_000, "deuterium": 50_000})
        self.assertGreater(sum(game.ships.values()) + sum(game.defense.values()), 0)
