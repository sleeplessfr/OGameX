import random, unittest
from swarm import targetlearn as T
from swarm.military import Memory, pick_attack
from swarm.personas import PRESETS

class Book(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(T.key_of(30_000, None, False), "L0G?o")
        self.assertEqual(T.key_of(500_000, 50, True), "L2G2a")
        self.assertEqual(T.key_of(2_000_000, 0, False), "L3G0o")
    def test_learns_good_and_bad_classes(self):
        b = T.new_book()
        for _ in range(10): T.update(b, "good", 0.9); T.update(b, "bad", -1.0)
        self.assertGreater(T.expected(b, "good"), 0.6); self.assertLess(T.expected(b, "bad"), -0.3)
        self.assertTrue(T.vetoed(b, "bad")); self.assertFalse(T.vetoed(b, "good"))
        r = random.Random(1)
        self.assertGreater(sum(T.factor(b, "good", r) for _ in range(50)), sum(T.factor(b, "bad", r) for _ in range(50)))
    def test_few_results_never_veto(self):
        b = T.new_book(); T.update(b, "x", -2.0); T.update(b, "x", -2.0)
        self.assertFalse(T.vetoed(b, "x"))
    def test_ratio(self):
        self.assertEqual(T.ratio_of(100_000, 0, 0, 100_000), 1.0)
        self.assertLess(T.ratio_of(0, 0, 50_000, 100_000), 0)
        self.assertEqual(T.ratio_of(0, 0, 10**9, 10), -2.0)
    def test_clean_and_group(self):
        self.assertEqual(T.clean_book({"a": {"n": "x", "mean": 1}, "b": {"n": 2, "mean": 0.5}}), {"b": {"n": 2.0, "mean": 0.5}})
        self.assertEqual(T.clean_book(None), {})
        groups = {T.group_of(f"agent{i:04d}") for i in range(1, 60)}
        self.assertEqual(groups, {"learned", "control"})
        self.assertEqual(T.group_of("agent0007"), T.group_of("agent0007"))
    def test_memory_roundtrip(self):
        m = Memory(); T.update(m.targets, "L1G2o", 0.5); m.target_for[(1, 2, 3)] = {"key": "L1G2o", "loot": 100000}
        m2 = Memory.from_dict(m.to_dict())
        self.assertEqual(m2.targets, m.targets); self.assertEqual(m2.target_for, m.target_for)

class Pick(unittest.TestCase):
    def _mem(self, now):
        m = Memory()
        m.intel[(1, 5, 5)] = {"at": now - 10, "loot": 300_000, "defenders": 10, "owner_id": 1, "ships": {}, "defense": {"rocket_launcher": 10}}
        return m
    def test_vetoed_class_is_dropped_only_for_learners(self):
        import dataclasses
        now = 100_000.0; persona = dataclasses.replace(PRESETS["raider"], aggression=1.0, gamble=1.0); ships = {"light_fighter": 50, "small_cargo": 50}
        m = self._mem(now)
        key = T.key_of(300_000, 10, False)
        for _ in range(10): T.update(m.targets, key, -1.5)
        ctrl = pick_attack(persona, random.Random(3), m, now, ships)
        learned = pick_attack(persona, random.Random(3), m, now, ships, learn_rng=random.Random(4))
        self.assertIsNotNone(ctrl); self.assertIsNone(learned)
if __name__ == "__main__": unittest.main()
