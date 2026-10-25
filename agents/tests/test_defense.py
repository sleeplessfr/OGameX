import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.defense import MAX_ORDER, Threat, choose_defense, defence_share, find_threats
from swarm.personas import PRESETS
from tests.fakegame import FakeGame

MINER = replace(PRESETS["miner"], caution=0.6)


def mission(id=1, kind="attack", mine=False, returning=False, left=90, origin=(1, 3, 7)):
    return {"id": id, "mission": kind, "is_mine": mine, "is_return": returning, "seconds_remaining": left,
            "from": {"galaxy": origin[0], "system": origin[1], "position": origin[2]}}


def unit(name, cost, cat="defense", maxb=500, met=True):
    return {"machine_name": name, "category": cat, "requirements_met": met, "character_class_met": True, "max_buildable": maxb,
            "cost_per_unit": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}}


class ThreatTests(unittest.TestCase):
    def test_finds_attacks_and_probes_coming_at_us_only(self):
        found = find_threats([mission(1, "attack"), mission(2, "espionage"), mission(3, "attack", mine=True),
                              mission(4, "attack", returning=True), mission(5, "transport"), mission(6, "expedition")])
        self.assertEqual([(t.id, t.kind) for t in found], [(1, "attack"), (2, "espionage")])
        self.assertEqual(found[0].origin, (1, 3, 7))
        self.assertTrue(found[0].is_attack)
        self.assertFalse(found[1].is_attack)

    def test_share_depends_on_what_is_coming(self):
        attack, probe = Threat(1, "attack", 60, (1, 1, 1)), Threat(2, "espionage", 60, (1, 1, 1))
        self.assertEqual(defence_share(MINER, []), 0.0)
        self.assertEqual(defence_share(MINER, [probe]), 0.0)                       # a stranger's probe: ignore
        self.assertEqual(defence_share(MINER, [probe], {(1, 1, 1)}), MINER.caution * 0.5)   # from someone who hit us before
        self.assertEqual(defence_share(MINER, [probe, attack]), MINER.caution)


class ChooseDefenseTests(unittest.TestCase):
    HAVE = {"metal": 100_000, "crystal": 50_000, "deuterium": 20_000}

    def test_only_defense_units_never_ships_or_missiles(self):
        units = [unit("light_fighter", (3000, 1000, 0), cat="ship"), unit("interplanetary_missile", (12500, 2500, 10000)),
                 unit("rocket_launcher", (2000, 0, 0))]
        name, amount = choose_defense(MINER, units, self.HAVE, 0.6)
        self.assertEqual(name, "rocket_launcher")
        self.assertEqual(amount, 30)        # 60% of 100,000 metal at 2,000 each

    def test_respects_share_requirements_and_the_order_limits(self):
        units = [unit("rocket_launcher", (2000, 0, 0), maxb=7)]
        self.assertEqual(choose_defense(MINER, units, self.HAVE, 0.6)[1], 7)       # the game's limit
        self.assertIsNone(choose_defense(MINER, [unit("rocket_launcher", (2000, 0, 0), met=False)], self.HAVE, 0.6))
        self.assertIsNone(choose_defense(MINER, units, {"metal": 100, "crystal": 0, "deuterium": 0}, 0.6))
        self.assertIsNone(choose_defense(MINER, units, self.HAVE, 0.0))
        rich = {"metal": 10 ** 9, "crystal": 10 ** 9, "deuterium": 10 ** 9}
        self.assertEqual(choose_defense(MINER, [unit("rocket_launcher", (2000, 0, 0), maxb=10 ** 6)], rich, 0.9)[1], MAX_ORDER)

    def test_personality_weights_steer_the_choice(self):
        units = [unit("rocket_launcher", (2000, 0, 0)), unit("light_laser", (1500, 500, 0))]
        turtle = replace(PRESETS["turtle"], unit_weights={"light_laser": 100.0, "rocket_launcher": 0.1})
        self.assertEqual(choose_defense(turtle, units, self.HAVE, 0.5)[0], "light_laser")


class AgentDefenseTests(unittest.TestCase):
    def setUp(self):
        self.game = FakeGame()
        self.game.levels["shipyard"] = 1
        self.game.res = {"metal": 60_000.0, "crystal": 5_000.0, "deuterium": 0.0}
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        self.agent = Agent("sim", MINER, "x", store, random.Random(1), client=self.game)
        self.agent.user_id = 1
        self.logs = []
        self.agent.log = lambda event, **f: self.logs.append((event, f))

    def events(self, name):
        return [f for e, f in self.logs if e == name]

    def test_quiet_times_build_no_defence_and_do_not_alert(self):
        self.agent.tick()
        self.assertEqual(self.events("defend"), [])
        self.assertEqual(self.agent.stats["defences"], 0)

    def test_an_incoming_attack_turns_stock_into_defence_and_keeps_the_agent_alert(self):
        self.game.hostile = [mission(7, "attack", left=60) | {"arrive_at": self.game.now + 60}]
        delay = self.agent.tick()
        self.assertEqual(len(self.events("threat")), 1)
        defend = self.events("defend")
        self.assertEqual(len(defend), 1)
        self.assertEqual(defend[0]["what"], "rocket_launcher")
        self.assertGreater(self.game.res["metal"], 0)                    # kept the rest
        self.assertLess(self.game.res["metal"], 60_000 * 0.6 + 100)      # spent about its caution share
        self.assertLess(delay, 40.0)                                     # looks again soon

    def test_one_order_per_cooldown_and_the_threat_is_logged_once(self):
        self.game.hostile = [mission(7, "attack", left=600) | {"arrive_at": self.game.now + 600}]
        self.agent.tick()
        self.game.advance(30)
        self.agent.tick()
        self.assertEqual(len(self.events("defend")), 1)
        self.assertEqual(len(self.events("threat")), 1)
        self.game.advance(700)   # the cooldown is over, and the attack is still not here
        self.game.hostile = [mission(8, "attack", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        self.assertEqual(len(self.events("defend")), 2)

    def test_a_strangers_probe_is_ignored_but_a_known_aggressors_probe_is_not(self):
        self.game.hostile = [mission(7, "espionage", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        self.assertEqual(self.events("defend"), [])
        self.assertEqual(self.agent.memory.aggressors, {})

    def test_an_attack_makes_its_origin_a_known_aggressor_whose_probes_then_count(self):
        self.game.hostile = [mission(7, "attack", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        self.assertIn((1, 3, 7), self.agent.memory.aggressors)
        self.game.advance(700)
        self.game.hostile = [mission(8, "espionage", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        self.assertEqual(len(self.events("defend")), 2)

    def test_a_probe_alone_gets_a_smaller_reaction_than_an_attack(self):
        self.agent.memory.aggressors[(1, 3, 7)] = 0.0
        self.game.hostile = [mission(7, "espionage", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        spent_on_probe = 60_000 - self.game.res["metal"]
        other = FakeGame()
        other.levels["shipyard"] = 1
        other.res = {"metal": 60_000.0, "crystal": 5_000.0, "deuterium": 0.0}
        other.hostile = [mission(7, "attack", left=60) | {"arrive_at": 60}]
        agent2 = Agent("sim2", MINER, "x", TokenStore(str(Path(tempfile.mkdtemp()) / "t.json")), random.Random(1), client=other)
        agent2.user_id = 1
        agent2.log = lambda *a, **k: None
        agent2.tick()
        self.assertLess(spent_on_probe, 60_000 - other.res["metal"])

    def test_a_peaceful_persona_without_caution_does_nothing(self):
        self.agent.persona = replace(MINER, caution=0.0)
        self.game.hostile = [mission(7, "attack", left=60) | {"arrive_at": self.game.now + 60}]
        self.agent.tick()
        self.assertEqual(self.events("defend"), [])
        self.assertEqual(len(self.events("threat")), 1)   # it still notices

    def test_a_failing_fleet_call_never_breaks_the_round(self):
        original = self.game.get
        self.game.get = lambda path: (_ for _ in ()).throw(__import__("swarm.client", fromlist=["ApiError"]).ApiError(500, "x", "boom")) if path == "/fleet" else original(path)
        self.agent.tick()   # must not raise


if __name__ == "__main__":
    unittest.main()
