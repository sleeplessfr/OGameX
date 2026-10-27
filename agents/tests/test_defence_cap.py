import unittest

from swarm import defense as D
from swarm.personas import PRESETS

def unit(name, cost, amount=0, maxb=500):
    return {"machine_name": name, "category": "defense", "requirements_met": True, "character_class_met": True,
            "cost_per_unit": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}, "max_buildable": maxb, "amount": amount}

class DefenceCap(unittest.TestCase):
    def test_cap_scales_with_caution(self):
        t, r = D.defence_cap(PRESETS["turtle"]), D.defence_cap(PRESETS["raider"])
        self.assertGreater(t, 3 * r)
        self.assertAlmostEqual(D.defence_cap(PRESETS["balanced"]), 600_000, delta=1)
        self.assertEqual(D.defence_cap(PRESETS["balanced"], True), 900_000)

    def test_worth_counts_defence_only(self):
        us = [unit("rocket_launcher", (2000, 0, 0), 100), {**unit("small_cargo", (2000, 2000, 0), 50), "category": "ship"}]
        self.assertEqual(D.defence_worth(us), 200_000)

    def test_room_limits_the_order(self):
        us = [unit("rocket_launcher", (2000, 0, 0))]
        have = {"metal": 10_000_000, "crystal": 0, "deuterium": 0}
        pick = D.choose_defense(PRESETS["balanced"], us, have, 0.5, None, room=50_000)
        self.assertEqual(pick, ("rocket_launcher", 25))
        self.assertIsNone(D.choose_defense(PRESETS["balanced"], us, have, 0.5, None, room=1_000))
        self.assertEqual(D.choose_defense(PRESETS["balanced"], us, have, 0.5, None)[1], 300)  # no room given: the old behaviour

if __name__ == "__main__":
    unittest.main()
