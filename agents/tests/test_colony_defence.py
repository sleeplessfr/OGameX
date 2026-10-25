import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.defense import attribute, find_threats
from swarm.personas import PRESETS

HOME = {"id": 10, "coordinates": {"galaxy": 1, "system": 3, "position": 8}}
COLONY = {"id": 11, "coordinates": {"galaxy": 1, "system": 4, "position": 9}}


def fleet(mission_id, kind, to_planet=None, to_coord=None, left=120):
    m = {"id": mission_id, "mission": kind, "is_mine": False, "is_return": False, "seconds_remaining": left,
         "from": {"galaxy": 1, "system": 9, "position": 6}, "to": {}}
    if to_planet:
        m["to"]["planet_id"] = to_planet
    if to_coord:
        m["to"].update({"galaxy": to_coord[0], "system": to_coord[1], "position": to_coord[2]})
    return m


class AttributionTests(unittest.TestCase):
    def test_threats_go_to_the_planet_they_are_aimed_at(self):
        threats = find_threats([fleet(1, "attack", to_planet=11), fleet(2, "attack", to_planet=10)])
        got = attribute(threats, [HOME, COLONY], HOME)
        self.assertEqual([t.id for t in got[11]], [1])
        self.assertEqual([t.id for t in got[10]], [2])

    def test_coordinates_are_used_when_the_id_does_not_match(self):
        threats = find_threats([fleet(1, "attack", to_coord=(1, 4, 9))])
        got = attribute(threats, [HOME, COLONY], HOME)
        self.assertEqual([t.id for t in got[11]], [1])

    def test_missing_or_unknown_target_falls_back_to_home(self):
        threats = find_threats([fleet(1, "attack"), fleet(2, "attack", to_planet=99)])
        got = attribute(threats, [HOME, COLONY], HOME)
        self.assertEqual(sorted(t.id for t in got[10]), [1, 2])
        self.assertEqual(got[11], [])

    def test_one_planet_gets_everything(self):
        threats = find_threats([fleet(1, "attack", to_planet=55)])
        self.assertEqual([t.id for t in attribute(threats, [HOME], HOME)[10]], [1])


class StubGame:
    """Two planets, only what one agent round needs. Records the orders it receives."""

    def __init__(self, missions, colony_has_shipyard=True):
        self.missions = missions
        self.colony_has_shipyard = colony_has_shipyard
        self.posts = []
        self.time = 1000

    def planet(self, base, name):
        return {**base, "name": name, "type": "planet", "resources": {"metal": 500000, "crystal": 500000, "deuterium": 500000},
                "storage": {"metal": 10 ** 7, "crystal": 10 ** 7, "deuterium": 10 ** 7},
                "production_per_hour": {"metal": 1000, "crystal": 1000, "deuterium": 1000}, "energy": 100,
                "fields": {"used": 50, "max": 163}, "unit_queue": [],
                "building_queue": [{"active": True, "seconds_remaining": 100}]}

    def get(self, path):
        if path == "/status":
            return {"server_time": self.time, "player": {"current_planet_id": 10, "dark_matter": 0},
                    "planets": [self.planet(HOME, "Home"), self.planet(COLONY, "Colony")],
                    "research": {"levels": {}, "queue": [{"active": True, "seconds_remaining": 100}]},
                    "fleet_slots": {"used": 0, "max": 1}, "unread_messages": 0}
        if path == "/fleet":
            return {"missions": self.missions}
        if path.endswith("/units"):
            pid = int(path.split("/")[2])
            met = pid == 10 or self.colony_has_shipyard
            return {"units": [{"machine_name": "rocket_launcher", "category": "defense", "amount": 0,
                               "cost_per_unit": {"metal": 2000, "crystal": 0, "deuterium": 0}, "seconds_per_unit": 5,
                               "requirements_met": met, "character_class_met": True, "max_buildable": 200 if met else 0}]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"queue": []}


class RoutingTests(unittest.TestCase):
    def agent(self, game):
        persona = replace(PRESETS["turtle"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0,
                          expansion=0.0, character_class=None, caution=0.9)
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(1), client=game)
        agent.events = []
        agent.log = lambda event, **k: agent.events.append((event, k))
        agent.user_id = 1
        return agent

    def orders(self, game):
        return [p for p, _ in game.posts if p.endswith("/units")]

    def test_an_attack_on_the_colony_is_defended_on_the_colony(self):
        game = StubGame([fleet(1, "attack", to_planet=11)])
        agent = self.agent(game)
        agent.tick()
        self.assertEqual(self.orders(game), ["/planets/11/units"])
        defend = [k for e, k in agent.events if e == "defend"]
        self.assertEqual(defend[0]["planet"], 11)

    def test_an_attack_on_home_is_still_defended_on_home_only(self):
        game = StubGame([fleet(1, "attack", to_planet=10)])
        agent = self.agent(game)
        agent.tick()
        self.assertEqual(self.orders(game), ["/planets/10/units"])

    def test_no_target_given_means_home(self):
        game = StubGame([fleet(1, "attack")])
        agent = self.agent(game)
        agent.tick()
        self.assertEqual(self.orders(game), ["/planets/10/units"])

    def test_a_colony_that_was_hit_stays_on_guard_without_new_threats(self):
        game = StubGame([fleet(1, "attack", to_planet=11)])
        agent = self.agent(game)
        agent.tick()
        game.missions = []
        game.posts.clear()
        game.time = 1000 + 700            # past the order cooldown, still within the 3 hours
        agent.tick()
        self.assertEqual(self.orders(game), ["/planets/11/units"])
        self.assertNotIn("/planets/10/units", self.orders(game))

    def test_a_colony_without_a_shipyard_says_so_once_and_asks_for_one(self):
        game = StubGame([fleet(1, "attack", to_planet=11)], colony_has_shipyard=False)
        agent = self.agent(game)
        agent.tick()
        agent.tick()
        self.assertEqual(self.orders(game), [])
        self.assertEqual([k["planet"] for e, k in agent.events if e == "defend_impossible"], [11])
        self.assertEqual(agent._planet_goals[11], {"shipyard": 2})

    def test_quiet_colonies_are_left_alone(self):
        game = StubGame([])
        agent = self.agent(game)
        agent.tick()
        self.assertEqual(self.orders(game), [])


if __name__ == "__main__":
    unittest.main()
