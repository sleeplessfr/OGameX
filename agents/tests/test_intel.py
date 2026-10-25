import random
import unittest

from swarm.military import assess_attack, summarize_report
from swarm.personas import PRESETS


def report(ships, defense):
    return {"resources": {"metal": 40000, "crystal": 20000, "deuterium": 10000}, "ships": ships, "defense": defense,
            "target": {"owner_id": 2}}


class HiddenDefenceTests(unittest.TestCase):
    def test_ships_without_the_defence_are_not_a_known_garrison(self):
        intel = summarize_report(report({"light_fighter": 5, "espionage_probe": 3}, None))
        self.assertFalse(intel.composition_known)
        self.assertIsNone(intel.defenders)

    def test_a_revealed_empty_defence_is_known(self):
        for empty in ({}, []):
            intel = summarize_report(report({"light_fighter": 2}, empty))
            self.assertTrue(intel.composition_known)
            self.assertEqual(intel.defenders, 2)

    def test_a_raider_does_not_treat_hidden_defence_as_none(self):
        intel = summarize_report(report({"light_fighter": 2}, None))
        raider = PRESETS["raider"]
        never_gamble = type(raider)(**{**raider.__dict__, "gamble": 0.0})
        go, why = assess_attack(never_gamble, random.Random(1), intel, my_fighters=20)
        self.assertFalse(go)
        self.assertIn("unknown", why)


if __name__ == "__main__":
    unittest.main()
