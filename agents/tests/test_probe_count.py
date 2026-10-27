import unittest

from swarm import military as M
from swarm.personas import PRESETS


class ProbeCount(unittest.TestCase):
    def setUp(self):
        self.p = PRESETS["raider"]
        self.mem = M.Memory()
        self.c = (1, 5, 5)

    def test_threshold_is_three(self):
        self.assertEqual(M.probes_to_send(self.p, self.mem, self.c, 10), 3 if self.p.probes_per_spy <= 3 else self.p.probes_per_spy)
        self.assertEqual(M.probes_to_send(self.p, self.mem, self.c, 2), 0)   # 1-2 probes never show defences: do not send

    def test_hidden_defence_raises_the_count(self):
        M.learn_probes(self.mem, self.c, False)
        self.assertEqual(self.mem.probes_for[self.c], 5)
        M.learn_probes(self.mem, self.c, False)
        self.assertEqual(M.probes_to_send(self.p, self.mem, self.c, 20), 7)
        for _ in range(20):
            M.learn_probes(self.mem, self.c, False)
        self.assertEqual(M.probes_to_send(self.p, self.mem, self.c, 99), M.SPY_MAX_PROBES)

    def test_shown_defence_keeps_the_count(self):
        M.learn_probes(self.mem, self.c, False)
        M.learn_probes(self.mem, self.c, True)
        self.assertEqual(self.mem.probes_for[self.c], 5)

    def test_limited_by_stock(self):
        self.mem.probes_for[self.c] = 9
        self.assertEqual(M.probes_to_send(self.p, self.mem, self.c, 4), 4)

    def test_stock_and_roundtrip(self):
        self.assertGreaterEqual(M.probe_stock(self.p), 4)
        self.mem.probes_for[self.c] = 7
        again = M.Memory.from_dict(self.mem.to_dict())
        self.assertEqual(again.probes_for[self.c], 7)


if __name__ == "__main__":
    unittest.main()
