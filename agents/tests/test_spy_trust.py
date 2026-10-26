import unittest
from swarm import diplomacy as D

class SpyTrust(unittest.TestCase):
    def test_stranger_small_with_cooldown(self):
        r = D.new_rel(0); r["trust"] = 10
        a = D.on_spied(r, 100000); b = D.on_spied(r, 100200)
        self.assertLess(a["change"], 0); self.assertEqual(b["change"], 0)
        self.assertEqual(r["spied_us"], 2); self.assertFalse(a["lie"])
    def test_partner_spy_is_lie(self):
        for st in ("friendly", "pact", "alliance"):
            r = D.new_rel(0); r["step"] = st; r["trust"] = 70
            res = D.on_spied(r, 100)
            self.assertTrue(res["lie"]); self.assertEqual(r["spy_lies"], 1)
            self.assertEqual(r["trust"], 40); self.assertEqual(r["step"], D.prev_step(st))
    def test_clean_rel_keeps_fields(self):
        r = D.clean_rel({"spy_lies": 2, "spied_at": 5}); self.assertEqual(r["spy_lies"], 2)
if __name__ == "__main__": unittest.main()

class Touchy(unittest.TestCase):
    def test_raider_shrugs_turtle_minds(self):
        a = D.new_rel(0); a["trust"] = 10; b = D.new_rel(0); b["trust"] = 10
        ra = D.on_spied(a, 100000, D.touchiness(1.0)); rb = D.on_spied(b, 100000, D.touchiness(0.0))
        self.assertGreater(ra["change"], rb["change"])
        self.assertAlmostEqual(D.touchiness(0.5), 1.0)
    def test_lie_costs_the_same(self):
        x = D.new_rel(0); x["step"] = "pact"; x["trust"] = 70
        y = D.new_rel(0); y["step"] = "pact"; y["trust"] = 70
        D.on_spied(x, 100, 0.5); D.on_spied(y, 100, 1.5)
        self.assertEqual(x["trust"], y["trust"])
