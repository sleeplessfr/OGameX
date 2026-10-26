import unittest
from swarm.defense import find_threats

class Target(unittest.TestCase):
    def test_threat_knows_where_it_flies(self):
        m = [{"id": 1, "mission": "attack", "seconds_remaining": 50, "ships": {"light_fighter": 3},
              "from": {"galaxy": 1, "system": 4, "position": 2}, "to": {"planet_id": 9, "galaxy": 1, "system": 7, "position": 5}}]
        t = find_threats(m)[0]
        self.assertEqual(t.target, (1, 7, 5)); self.assertEqual(t.origin, (1, 4, 2))
    def test_missing_target_is_none(self):
        m = [{"id": 2, "mission": "espionage", "seconds_remaining": 5, "from": {"galaxy": 1, "system": 4, "position": 2}}]
        self.assertIsNone(find_threats(m)[0].target)
if __name__ == "__main__": unittest.main()
