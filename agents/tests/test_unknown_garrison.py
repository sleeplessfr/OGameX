import random, unittest
from swarm import military as M
class UnknownGarrison(unittest.TestCase):
    def test_small_fleet_vs_unknown_is_refused(self):
        intel = M.Intel(loot=200000, defenders=None, owner_id=1, ships=None, defense=None, research=None)
        from swarm.personas import PRESETS
        persona = PRESETS["raider"]
        go, why, est = M.judge_fight(persona, random.Random(1), {"light_fighter": 1, "small_cargo": 10}, intel, None, 50000)
        self.assertFalse(go); self.assertIn("assumed", why)
if __name__ == "__main__": unittest.main()
