import random
import unittest

from swarm import military as M
from swarm.personas import PRESETS


class TypicalGarrison(unittest.TestCase):
    def test_floor_when_little_seen(self):
        self.assertEqual(M.typical_garrison(None), M.UNKNOWN_GARRISON)
        self.assertEqual(M.typical_garrison([500, 600]), M.UNKNOWN_GARRISON)

    def test_median_of_seen(self):
        self.assertEqual(M.typical_garrison([100, 200, 300, 150, 250]), 200)

    def test_never_below_floor_and_capped(self):
        self.assertEqual(M.typical_garrison([1, 2, 3]), M.UNKNOWN_GARRISON)
        self.assertEqual(M.typical_garrison([9000] * 5), M.GARRISON_CAP)

    def test_garbage_ignored(self):
        self.assertEqual(M.typical_garrison(["x", None, -5, 100]), M.UNKNOWN_GARRISON)

    def test_memory_roundtrip(self):
        m = M.Memory()
        m.garrisons = [10, 20, 30]
        self.assertEqual(M.Memory.from_dict(m.to_dict()).garrisons, [10, 20, 30])
        self.assertEqual(M.Memory.from_dict({"garrisons": "bad"}).garrisons, [])

    def test_judge_uses_bigger_guess(self):
        intel = M.Intel(loot=100000, defenders=None, owner_id=1)
        strike = {"light_fighter": 20}
        _, why, _ = M.judge_fight(PRESETS["raider"], random.Random(1), strike, intel, None, 100000, garrison=220)
        self.assertIn("assumed 220 rocket launchers", why)
        _, why, _ = M.judge_fight(PRESETS["raider"], random.Random(1), strike, intel, None, 100000)
        self.assertIn("assumed 40 rocket launchers", why)


if __name__ == "__main__":
    unittest.main()
