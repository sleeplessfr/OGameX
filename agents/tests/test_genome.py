import math
import random
import unittest

from swarm import genome as G
from swarm.personas import PRESETS


class Genome(unittest.TestCase):
    def test_neutral_changes_nothing(self):
        p = PRESETS["miner"]
        self.assertEqual(G.apply(p, G.neutral()), p)
        self.assertIs(G.apply(p, None), p)

    def test_apply_scales_weights_and_never_touches_the_character_axes(self):
        p = PRESETS["raider"]
        g = {**G.neutral(), "metal_mine": 2.0, "patience": 0.1, "r_energy_technology": 0.5}
        q = G.apply(p, g)
        self.assertAlmostEqual(q.building_weights["metal_mine"], p.building_weights["metal_mine"] * 2)
        self.assertAlmostEqual(q.patience, min(0.98, p.patience + 0.1))
        for axis in ("aggression", "risk_ratio", "gamble", "expansion", "sociability", "army_share", "character_class", "name"):
            self.assertEqual(getattr(q, axis), getattr(p, axis))
        self.assertEqual(p.building_weights["metal_mine"], PRESETS["raider"].building_weights["metal_mine"])   # original untouched

    def test_zero_weights_stay_zero(self):
        p = PRESETS["miner"]
        zero = [n for n in G.BUILDING_GENES if p.building_weights.get(n, 0) == 0]
        g = {n: 2.5 for n in G.GENES if G.GENES[n][0] == "mult"}
        q = G.apply(p, g)
        for n in zero:
            self.assertEqual(q.building_weights.get(n, 0), 0)

    def test_clamp_limits_and_survives_garbage(self):
        c = G.clamp({"metal_mine": 99, "solar_plant": -3, "patience": 5, "store_alarm": "x", "nonsense": 1, "balance": float("nan")})
        self.assertEqual(c["metal_mine"], G.MULT_RANGE[1])
        self.assertEqual(c["solar_plant"], G.MULT_RANGE[0])
        self.assertEqual(c["patience"], G.ADD_RANGE["patience"][1])
        self.assertEqual(c["store_alarm"], 0.0)
        self.assertEqual(c["balance"], 1.0)
        self.assertNotIn("nonsense", c)
        self.assertEqual(G.clamp("junk"), G.neutral())

    def test_mutation_stays_in_range_and_moves(self):
        rng = random.Random(1)
        g = G.neutral()
        for _ in range(200):
            g = G.mutate(g, rng, sigma=0.4)
        self.assertEqual(g, G.clamp(g))
        self.assertGreater(G.distance(g, G.neutral()), 0.05)

    def test_distance(self):
        a = G.neutral()
        b = {**a, "metal_mine": math.exp(0.5)}
        self.assertAlmostEqual(G.distance(a, b), 0.5 / len(G.GENES), places=3)
        self.assertEqual(G.distance(a, a), 0.0)

    def test_crossover_takes_from_both(self):
        a, b = G.neutral(), {n: (2.0 if k == "mult" else 0.0) for n, (k, _) in G.GENES.items()}
        c = G.crossover(a, b, random.Random(3))
        self.assertTrue(any(c[n] == 1.0 for n in G.GENES if G.GENES[n][0] == "mult"))
        self.assertTrue(any(c[n] == 2.0 for n in G.GENES if G.GENES[n][0] == "mult"))

    def test_describe(self):
        self.assertEqual(G.describe(G.neutral()), "unchanged")
        self.assertIn("metal_mine x1.50", G.describe({**G.neutral(), "metal_mine": 1.5}))


if __name__ == "__main__":
    unittest.main()
