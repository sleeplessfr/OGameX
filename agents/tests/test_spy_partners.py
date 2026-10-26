import unittest
from swarm import diplomacy as D
from swarm.military import pick_target

class SpyPartners(unittest.TestCase):
    def test_peace_ids_excluded_from_targets(self):
        st = D.new_state() if hasattr(D, "new_state") else {"rel": {}}
        r = D.new_rel(0); r["step"] = "alliance"; st["rel"]["7"] = r
        self.assertEqual(D.peace_ids(st, 100), {7})
        r2 = D.new_rel(0); r2["step"] = "friendly"; st["rel"]["8"] = r2
        self.assertNotIn(8, D.peace_ids(st, 100))
if __name__ == "__main__": unittest.main()
