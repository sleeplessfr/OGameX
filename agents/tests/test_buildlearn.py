import random, unittest
from swarm import buildlearn as B
from swarm.military import Memory
from swarm.personas import PRESETS

class Style(unittest.TestCase):
    def test_apply_tilts_weights_not_persona(self):
        p = PRESETS["miner"]; q = B.apply(p, "mines")
        self.assertGreater(q.building_weights["metal_mine"], p.building_weights["metal_mine"])
        self.assertIs(B.apply(p, "plain"), p); self.assertEqual(p.building_weights["metal_mine"], 4.0)
    def test_metric(self):
        pl = [{"production_per_hour": {"metal": 100, "crystal": 100, "deuterium": 10}}]
        self.assertEqual(B.metric("miner", pl, None), 100 + 150 + 30)
        self.assertEqual(B.metric("scientist", pl, {"a": 3, "b": 4}), 7)
    def test_epoch_cycle_learns_best_style(self):
        st = B.new_state(); rng = random.Random(2); t = 0.0; value = 100.0
        self.assertIsNone(B.step(st, t, value, rng, False))
        growth_of = {"plain": 0.2, "mines": 0.5, "lab": 0.1, "factories": 0.15, "power": 0.2}
        picks = []
        for _ in range(60):
            t += B.EPOCH + 1
            value = st["start"] * (1 + growth_of[st["arm"]])
            res = B.step(st, t, value, rng, False); picks.append(res["next"])
        self.assertGreater(picks[30:].count("mines"), 15)
    def test_control_stays_plain(self):
        st = B.new_state(); rng = random.Random(1); t = 0.0
        B.step(st, t, 100, rng, True)
        for _ in range(5):
            t += B.EPOCH + 1; r = B.step(st, t, 120, rng, True); self.assertEqual(r["next"], "plain")
    def test_no_step_within_epoch(self):
        st = B.new_state(); B.step(st, 0, 100, random.Random(1), False)
        self.assertIsNone(B.step(st, 100, 500, random.Random(1), False))
    def test_roundtrip_and_damage(self):
        m = Memory(); B.record(m.build, "mines", 0.4); m.build["arm"] = "lab"
        self.assertEqual(Memory.from_dict(m.to_dict()).build, m.build)
        self.assertEqual(B.clean_state({"arm": "nonsense", "styles": 5}), B.new_state())
if __name__ == "__main__": unittest.main()
