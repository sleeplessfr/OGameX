import json
import random
import tempfile
import unittest
from pathlib import Path

import train_genome as T
from swarm import genome as G


class Trainer(unittest.TestCase):
    def test_one_tiny_training_writes_a_valid_genome(self):
        ev = T.Evaluator(1)
        try:
            res = T.train("miner", ev, hours=6, generations=2, population=3, keep=2, seeds=1, rng=random.Random(1), say=lambda t: None)
        finally:
            ev.close()
        self.assertEqual(res["persona"], "miner")
        self.assertEqual(res["genome"], G.clamp(res["genome"]))
        self.assertGreater(res["test_score"], 0)
        self.assertGreater(res["baseline_test_score"], 0)
        self.assertGreaterEqual(res["train_score"], 0)

    def test_same_seed_same_result(self):
        outs = []
        for _ in range(2):
            ev = T.Evaluator(1)
            try:
                outs.append(T.train("balanced", ev, hours=6, generations=1, population=3, keep=2, seeds=1, rng=random.Random(7), say=lambda t: None)["genome"])
            finally:
                ev.close()
        self.assertEqual(outs[0], outs[1])

    def test_the_command_writes_the_file_and_refuses_unknown_personalities(self):
        import sys
        out = tempfile.mkdtemp()
        old = sys.argv
        try:
            sys.argv = ["train_genome.py", "--persona", "turtle", "--hours", "6", "--generations", "1", "--population", "2", "--seeds", "1", "--procs", "1", "--out", out]
            self.assertEqual(T.main(), 0)
            data = json.loads((Path(out) / "turtle.json").read_text())
            self.assertEqual(data["persona"], "turtle")
            sys.argv = ["train_genome.py", "--persona", "wizard", "--out", out]
            self.assertEqual(T.main(), 2)
        finally:
            sys.argv = old


if __name__ == "__main__":
    unittest.main()
