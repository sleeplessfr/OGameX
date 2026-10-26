"""A planet with no free building fields keeps making progress (swarm/fullplanet.py)."""
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm import colonise, fullplanet
from swarm.agent import Agent, TokenStore
from swarm.brain import FIELD_ALARM, choose_building, choose_research
from swarm.personas import PRESETS
from tests.fakegame import FakeGame

MINER = replace(PRESETS["miner"], randomness=0.0, patience=1.0)
TURTLE = replace(PRESETS["turtle"], randomness=0.0, patience=1.0)
RICH = {"metal": 10 ** 9, "crystal": 10 ** 9, "deuterium": 10 ** 9}


def planet(left, energy=500):
    return {"production_per_hour": {"metal": 1000, "crystal": 500, "deuterium": 200}, "energy": energy,
            "storage": {"metal": 10 ** 12, "crystal": 10 ** 12, "deuterium": 10 ** 12}, "fields": {"used": 200 - left, "max": 200}}


def item(name, level, met=True, cost=(100, 50, 0)):
    return {"machine_name": name, "level": level, "requirements_met": met, "can_afford": True,
            "cost": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}}


def buildings(robot=10, nano=0, terra=0, shipyard=4, lab=3, nano_met=True):
    return [item("metal_mine", 30), item("crystal_mine", 28), item("solar_plant", 25), item("metal_store", 8),
            item("shipyard", shipyard), item("research_lab", lab), item("robot_factory", robot),
            item("nano_factory", nano, met=nano_met), item("terraformer", terra, met=nano > 0)]


class ReserveTests(unittest.TestCase):
    def test_the_last_fields_are_kept_for_the_terraformer_chain(self):
        # robot factory 10 is done: nanite factory + terraformer need 2 fields, and exactly 2 are left
        b = buildings()
        for seed in range(10):
            c = choose_building(MINER, random.Random(seed), planet(2), b, research_busy=False, units_busy=False, have=RICH)
            self.assertIn(c.machine_name, (None, "nano_factory"), seed)
            self.assertNotIn(c.machine_name, ("metal_mine", "crystal_mine", "metal_store"))
        c = choose_building(MINER, random.Random(1), planet(2), b, research_busy=False, units_busy=False, have=RICH)
        self.assertEqual(c.machine_name, "nano_factory")

    def test_a_spare_field_may_still_be_used(self):
        c = choose_building(MINER, random.Random(1), planet(FIELD_ALARM), buildings(), research_busy=False, units_busy=False, have=RICH)
        self.assertIsNotNone(c.machine_name)

    def test_waiting_for_research_does_not_burn_the_reserve(self):
        b = buildings(nano_met=False)    # computer technology 10 still missing: the nanite factory cannot be built yet
        c = choose_building(MINER, random.Random(1), planet(2), b, research_busy=False, units_busy=False, have=RICH)
        self.assertIsNone(c.machine_name)
        self.assertTrue(c.reserved)
        self.assertFalse(c.full)
        self.assertIn("no spare fields", c.reason)

    def test_the_robot_factory_comes_first_when_the_chain_is_open(self):
        c = choose_building(MINER, random.Random(1), planet(20), buildings(robot=6), research_busy=False, units_busy=False, have=RICH)
        self.assertEqual(c.machine_name, "robot_factory")
        self.assertIn("terraformer", c.reason)

    def test_when_the_chain_no_longer_fits_the_old_behaviour_stays(self):
        # 3 fields left, the chain needs 5+1+1: unreachable, so the last fields are used as before
        b = buildings(robot=5)
        self.assertIsNone(fullplanet.reserve(b, 3, FIELD_ALARM))
        self.assertFalse(fullplanet.reachable(b, 3))
        c = choose_building(MINER, random.Random(1), planet(3), b, research_busy=False, units_busy=False, have=RICH)
        self.assertIsNotNone(c.machine_name)

    def test_a_game_without_a_nanite_factory_is_untouched(self):
        b = [x for x in buildings() if x["machine_name"] != "nano_factory"]
        self.assertIsNone(fullplanet.reserve(b, 2, FIELD_ALARM))

    def test_a_full_planet_researches_every_useful_technology(self):
        # the miner's own taste is three technologies; a full planet also gets the others
        techs = [item("energy_technology", 12), item("combustion_drive", 3), item("ion_technology", 2), item("astrophysics", 1),
                 item("graviton_technology", 0)]
        for t in techs:
            t["cost"] = {"metal": 0, "crystal": 10, "deuterium": 0}
        seen = set()
        for seed in range(60):
            c = choose_research(replace(MINER, randomness=0.9), random.Random(seed), planet(0), techs, have=RICH)
            seen.add(c.machine_name)
        self.assertTrue({"ion_technology", "astrophysics"} <= seen, seen)
        self.assertNotIn("graviton_technology", seen)
        # with free fields the miner sticks to its taste
        seen_free = {choose_research(replace(MINER, randomness=0.9), random.Random(s), planet(100), techs, have=RICH).machine_name for s in range(60)}
        self.assertNotIn("ion_technology", seen_free)


class PlanTests(unittest.TestCase):
    def test_reachable_chain_is_the_plan(self):
        p = fullplanet.plan(left=14, buildings=buildings(robot=6), research_levels={"energy_technology": 5}, planets_owned=1,
                            expanding=False, spending=False)
        self.assertEqual(p["decision"], "terraformer path")
        self.assertTrue(p["reachable"])
        self.assertEqual(p["need"], 6)

    def test_full_planet_expands_or_spends_and_says_why(self):
        p = fullplanet.plan(left=0, buildings=buildings(), research_levels={}, planets_owned=2, expanding=True, spending=True)
        self.assertEqual(p["decision"], "expanding")
        self.assertIn("unreachable", p["why"])
        self.assertIn("ships and defence", p["why"])
        p = fullplanet.plan(left=0, buildings=buildings(), research_levels={}, planets_owned=fullplanet.FULL_PLANET_CAP,
                            expanding=False, spending=True)
        self.assertEqual(p["decision"], "spending")
        p = fullplanet.plan(left=0, buildings=buildings(shipyard=1), research_levels={}, planets_owned=2, expanding=False, spending=True)
        self.assertIn("colony blocked", p["why"])
        self.assertEqual(p["decision"], "spending")

    def test_nothing_is_said_when_there_is_nothing_to_do(self):
        b = [x for x in buildings(shipyard=1, lab=0)]
        p = fullplanet.plan(left=0, buildings=b, research_levels={}, planets_owned=fullplanet.FULL_PLANET_CAP, expanding=False, spending=False)
        self.assertEqual(p["decision"], "nothing")

    def test_a_full_home_wants_a_colony_whatever_the_appetite(self):
        home = planet(0)
        self.assertTrue(colonise.wants_colony_full(home, 5))
        self.assertFalse(colonise.wants_colony_full(home, fullplanet.FULL_PLANET_CAP))
        self.assertFalse(colonise.wants_colony_full(planet(3), 1))
        self.assertFalse(colonise.wants_colony(replace(MINER, expansion=0.0), home, 1, random.Random(1)))


class FullPlanetInTheFakeGame(unittest.TestCase):
    def play(self, persona, hours=8, field_max=None):
        game = FakeGame()
        game.colony_stuff = True
        game.slots_max = 2
        game.levels.update({"research_lab": 3, "shipyard": 4, "metal_mine": 5, "solar_plant": 4,
                           "metal_store": 9, "crystal_store": 9, "deuterium_store": 9})   # stores big enough to hold the stock
        game.field_max = sum(game.levels.values()) + 2 if field_max is None else field_max
        game.tech.update({"impulse_drive": 3, "espionage_technology": 4})
        game.res.update({"metal": 5_000_000, "crystal": 5_000_000, "deuterium": 5_000_000})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(3), client=game)
        agent.events = []
        agent.log = lambda event, **k: agent.events.append((event, k))
        agent.user_id = 1
        elapsed = 0.0
        while elapsed < hours * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        return game, agent

    def names(self, agent):
        return [e for e, _ in agent.events]

    def test_a_full_miner_does_not_just_wait(self):
        game, agent = self.play(replace(PRESETS["miner"], expansion=0.0))     # no appetite for colonies at all
        names = self.names(agent)
        self.assertEqual(sum(game.levels.values()), game.field_max)   # the planet is full
        self.assertIn("expand", names)                               # ... so it expands anyway
        self.assertIn("colony_sent", names)
        self.assertGreaterEqual(len(game.colonies), 1)
        self.assertGreater(names.count("research"), 0)
        self.assertGreater(sum(game.defense.values()) + sum(v for k, v in game.ships.items() if k != "colony_ship"), 0)
        self.assertEqual(agent.stats["errors"], 0)

    def test_a_full_turtle_that_owns_its_limit_still_expands_and_spends(self):
        game, agent = self.play(replace(PRESETS["turtle"], expansion=0.1))    # max_planets = 1: content with the home planet
        names = self.names(agent)
        self.assertIn("colony_sent", names)
        self.assertGreater(sum(game.defense.values()) + sum(game.ships.values()), 0)
        self.assertGreater(names.count("research"), 0)

    def test_the_plan_is_logged_once_an_hour(self):
        game, agent = self.play(replace(PRESETS["miner"], expansion=0.0), hours=5)
        plans = [k for e, k in agent.events if e == "full_planet_plan"]
        self.assertGreaterEqual(len(plans), 3)
        self.assertLessEqual(len(plans), 6)
        self.assertEqual(plans[-1]["free"], 0)
        self.assertIn(plans[-1]["decision"], ("expanding", "spending"))
        self.assertIn("why", plans[-1])

    def test_a_planet_with_plenty_of_fields_is_unchanged(self):
        game, agent = self.play(replace(PRESETS["miner"], expansion=0.0), hours=3, field_max=10 ** 6)
        names = self.names(agent)
        for e in ("full_planet_plan", "expand", "colony_sent"):
            self.assertNotIn(e, names)


if __name__ == "__main__":
    unittest.main()


class ReportTests(unittest.TestCase):
    def test_the_report_shows_what_a_full_planet_decided(self):
        import summarize_logs as sl
        events = [{"t": 1000 + i * 10, "agent": "agent0001", "event": "wait", "why": "no free fields on this planet (a terraformer adds some)"} for i in range(3)]
        events.append({"t": 1100, "agent": "agent0001", "event": "full_planet_plan", "planet": 7, "free": 0, "decision": "expanding",
                       "why": "terraformer unreachable: no free field; founding colony number 2 of up to 7"})
        text = sl.summarize(events)
        self.assertIn("3 waits with a full planet", text)
        self.assertIn("agent0001 expanding x1", text)
        self.assertIn("terraformer unreachable", text)
        self.assertIn("none", sl.summarize([{"t": 1, "agent": "a", "event": "build"}]).split("FULL PLANETS")[1].split("\n")[1])
