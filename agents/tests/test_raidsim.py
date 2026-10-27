import random
import unittest

from swarm import raidsim as r


class RaidSimTest(unittest.TestCase):
    def test_distance_rules(self):
        self.assertEqual(r.distance((1, 5, 3), (1, 5, 3)), 5)
        self.assertEqual(r.distance((1, 5, 3), (1, 5, 8)), 1025)
        self.assertEqual(r.distance((1, 5, 3), (1, 15, 3)), 2700 + 950)
        self.assertEqual(r.distance((1, 5, 3), (1, 495, 3)), 2700 + 9 * 95)  # donut system: 490 apart = 9 the short way
        self.assertEqual(r.distance((1, 5, 3), (3, 5, 3)), 40000)
        self.assertEqual(r.distance((1, 5, 3), (9, 5, 3)), 20000)  # donut galaxy

    def test_travel_matches_formula(self):
        t = r.travel_seconds((1, 1, 1), (1, 11, 1), {"small_cargo": 10}, 100, 1)
        self.assertEqual(t, round(350 * (3650 * 10 / 5000) ** 0.5 + 10))
        self.assertLess(r.travel_seconds((1, 1, 1), (1, 11, 1), {"small_cargo": 10}, 100, 8), t)
        self.assertLess(t, r.travel_seconds((1, 1, 1), (1, 11, 1), {"small_cargo": 10}, 50, 1))

    def test_split_loot(self):
        full = {"metal": 9000, "crystal": 3000, "deuterium": 0}
        self.assertEqual(r.split_loot(full, 20000), full)
        got = r.split_loot(full, 6000)
        self.assertAlmostEqual(sum(got.values()), 6000, delta=1)
        # thirds of 2000 each, deuterium has none, so its share is split over metal and crystal: 3000 each
        self.assertAlmostEqual(got["crystal"], 3000, delta=1)
        self.assertAlmostEqual(got["metal"], 3000, delta=1)
        self.assertEqual(got["deuterium"], 0)

    def test_empty_planet_is_looted(self):
        res = r.resolve({"large_cargo": 4, "light_fighter": 20}, {"metal": 10000, "crystal": 4000, "deuterium": 2000},
                        {}, random.Random(1))
        self.assertTrue(res.won)
        self.assertEqual(res.loot, {"metal": 5000.0, "crystal": 2000.0, "deuterium": 1000.0})
        self.assertEqual(sum(res.debris.values()), 0)

    def test_losing_raid_gives_no_loot_and_debris(self):
        res = r.resolve({"light_fighter": 10}, {"metal": 1e6, "crystal": 1e6, "deuterium": 1e6},
                        {"rocket_launcher": 60}, random.Random(2))
        self.assertFalse(res.won)
        self.assertEqual(sum(res.loot.values()), 0)
        self.assertGreater(res.debris["metal"], 0)         # dead fighters leave 30% of their price
        self.assertAlmostEqual(res.debris["metal"], 0.3 * 3000 * (10 - res.attacker_left["light_fighter"]), delta=1)

    def test_defence_is_rebuilt(self):
        res = r.resolve({"cruiser": 40}, {"metal": 0, "crystal": 0, "deuterium": 0}, {"rocket_launcher": 30}, random.Random(3))
        self.assertTrue(res.won)
        self.assertGreaterEqual(res.defender_left["rocket_launcher"], 21 - 1)  # 70% of 30 come back
        self.assertEqual(res.debris["metal"], 0.0 + res.debris["metal"])
        self.assertAlmostEqual(res.defender_lost_value, 30 * 2000 * 0.3, delta=2000)

    def test_cargo_limits_loot(self):
        res = r.resolve({"small_cargo": 2, "cruiser": 10}, {"metal": 1e6, "crystal": 1e6, "deuterium": 1e6}, {}, random.Random(4))
        self.assertTrue(res.won)
        self.assertLessEqual(sum(res.loot.values()), r.capacity({"small_cargo": 2, "cruiser": 10}))


if __name__ == "__main__":
    unittest.main()
