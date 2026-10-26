import unittest
from swarm import calibrate as C
from swarm.military import Memory

class Calib(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual([C.bucket(x) for x in (0, .29, .3, .79, .8, .94, .95, 1.0)], [0, 0, 1, 2, 3, 3, 4, 4])
    def test_no_data_means_no_change(self):
        self.assertEqual(C.corrected({}, 0.9), 0.9); self.assertIsNone(C.corrected({}, None))
    def test_overconfident_simulator_is_corrected_down(self):
        c = {}
        for i in range(20): C.update(c, 0.9, i % 2 == 0)      # says 90%, wins half
        self.assertLess(C.corrected(c, 0.9), 0.65)
    def test_underconfident_is_corrected_up(self):
        c = {}
        for _ in range(20): C.update(c, 0.5, True)
        self.assertGreater(C.corrected(c, 0.5), 0.8)
    def test_few_results_move_little(self):
        c = {}; C.update(c, 0.9, False)
        self.assertGreater(C.corrected(c, 0.9), 0.65)
    def test_none_prediction_ignored_and_won(self):
        c = {}; C.update(c, None, True); self.assertEqual(c, {})
        self.assertTrue(C.won_battle(100, 0, 50)); self.assertTrue(C.won_battle(0, 500, 0)); self.assertFalse(C.won_battle(0, 500, 10))
    def test_roundtrip_and_damage(self):
        m = Memory(); C.update(m.calib, 0.7, True)
        self.assertEqual(Memory.from_dict(m.to_dict()).calib, m.calib)
        self.assertEqual(C.clean_state({"9": {"n": 1, "w": 1}, "1": {"n": 1, "w": 5}, "2": "x"}), {})
if __name__ == "__main__": unittest.main()
