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
