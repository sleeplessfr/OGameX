import unittest
from unittest import mock

from swarm import logistics
from swarm.agent import Agent
from swarm.client import ApiError


class SmallFixesTest(unittest.TestCase):
    def test_a_fleet_of_a_couple_of_fighters_is_not_worth_saving(self):
        self.assertLess(logistics.fleet_value({"light_fighter": 2, "espionage_probe": 3}), logistics.MIN_FLEET_VALUE)
        self.assertGreater(logistics.fleet_value({"cruiser": 10}), logistics.MIN_FLEET_VALUE)

    def test_the_expedition_skip_is_logged_once_per_reason_and_hour(self):
        logged = []
        stub = mock.Mock(spec=Agent)
        stub.now = 10_000.0
        stub.log = lambda event, **kw: logged.append((event, kw))
        Agent._expedition_skipped(stub, "astrophysics too low")
        stub.now += 60
        Agent._expedition_skipped(stub, "astrophysics too low")
        stub.now += 60
        Agent._expedition_skipped(stub, "no free expedition slot")
        stub.now += 7200
        Agent._expedition_skipped(stub, "no free expedition slot")
        self.assertEqual([kw["why"] for _, kw in logged], ["astrophysics too low", "no free expedition slot", "no free expedition slot"])

    def test_a_busy_shipyard_is_not_counted_as_a_refusal(self):
        logged = []
        stub = mock.Mock(spec=Agent)
        stub.stats = {"refused": 0}
        stub.log = lambda event, **kw: logged.append((event, kw))
        stub.client = mock.Mock()
        stub.client.post.side_effect = ApiError(422, "shipyard_busy", "The shipyard is still busy.", {})
        choice = mock.Mock(machine_name="shipyard")
        out = Agent._act(stub, "build", {"id": 1}, choice, [{"machine_name": "shipyard", "cost": {}}], {}, "/planets/1/buildings")
        self.assertEqual(stub.stats["refused"], 0)
        self.assertEqual(logged[0][0], "busy")
        self.assertEqual(out, [60.0])


if __name__ == "__main__":
    unittest.main()


class GambleCapTest(unittest.TestCase):
    def test_a_costly_fleet_does_not_gamble_on_an_unknown_defence(self):
        import random
        from dataclasses import replace
        from swarm.military import Intel, assess_attack, GAMBLE_MAX_RISK
        from swarm.personas import PRESETS
        persona = replace(PRESETS["raider"], gamble=1.0, aggression=1.0, min_loot=0)
        intel = Intel(loot=50_000, defenders=None, owner_id=1, ships=None, defense=None, research=None)
        go, why = assess_attack(persona, random.Random(1), intel, 5, at_risk=GAMBLE_MAX_RISK * 50_000 * 1.5)
        self.assertFalse(go)
        self.assertIn("too much to gamble", why)
        go, why = assess_attack(persona, random.Random(1), intel, 5, at_risk=GAMBLE_MAX_RISK * 50_000 * 0.5)
        self.assertTrue(go)


class CargoBlockerTest(unittest.TestCase):
    def test_reasons_are_named(self):
        from swarm import logistics as L
        from swarm.personas import PRESETS
        raider, miner = PRESETS["raider"], PRESETS["miner"]
        self.assertIn("raider", L.cargo_build_blocker(raider, [], {}, L.SUPPLY_BUILDERS))
        self.assertEqual(L.cargo_build_blocker(miner, [], {}), "small cargo not offered")
        item = {"machine_name": "small_cargo", "requirements_met": False}
        self.assertIn("shipyard", L.cargo_build_blocker(miner, [item], {}))
        item = {"machine_name": "small_cargo", "requirements_met": True, "max_buildable": 0}
        self.assertIn("afford", L.cargo_build_blocker(miner, [item], {}))
