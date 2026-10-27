import random
import unittest

from swarm import military as m
from swarm.personas import PRESETS


class AllyIntelTest(unittest.TestCase):
    def setUp(self):
        self.persona = PRESETS["raider"]

    def test_no_gamble_on_ally_report_without_defences(self):
        intel = m.Intel(loot=500000, defenders=None, owner_id=2, at=1000.0, ally=7)
        for seed in range(50):   # gamble chance must not matter
            go, why = m.assess_attack(self.persona, random.Random(seed), intel, 100, now=1100.0)
            self.assertFalse(go)
            self.assertIn("own probes", why)

    def test_own_report_without_defences_still_may_gamble(self):
        intel = m.Intel(loot=500000, defenders=None, owner_id=2, at=1000.0)
        gos = [m.assess_attack(self.persona, random.Random(s), intel, 100, now=1100.0)[0] for s in range(200)]
        self.assertEqual(any(gos), self.persona.gamble > 0)

    def test_targets_known_only_from_an_ally_get_probed_first(self):
        mem = m.Memory()
        mem.intel[(1, 5, 5)] = {"at": 1.0, "loot": 1, "defenders": None, "from_ally": 9}
        players = [{"user_id": u, "score": 10, "main_planet": {"galaxy": 1, "system": s, "position": 5}, "is_me": False}
                   for u, s in ((2, 5), (3, 6), (4, 7))]
        t = m.pick_target(self.persona, random.Random(1), (1, 4, 5), 10, players, mem, 100.0,
                          allowed_ids=None, cooldown=0.0)
        self.assertEqual(t["coord"], (1, 5, 5))


if __name__ == "__main__":
    unittest.main()
