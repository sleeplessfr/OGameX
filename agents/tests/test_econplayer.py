import io
import contextlib
import unittest
from dataclasses import replace

import simulate_player as S
from swarm import econplayer as P
from swarm.personas import PRESETS


class WholePlayer(unittest.TestCase):
    def test_same_seed_same_game_different_seed_differs(self):
        a = P.play(PRESETS["miner"], hours=12, seed=3).final
        b = P.play(PRESETS["miner"], hours=12, seed=3).final
        c = P.play(PRESETS["miner"], hours=12, seed=4).final
        self.assertEqual(a, b)
        self.assertNotEqual(a["worth"], c["worth"])

    def test_the_economy_grows_and_nothing_goes_negative(self):
        pl = P.Player(PRESETS["balanced"], seed=1)
        r = pl.run(24)
        worth = [s["worth"] for s in r.timeline]
        self.assertGreater(worth[-1], worth[0] * 5)
        self.assertEqual(sorted(worth)[0], worth[0])
        for planet in pl.planets:
            self.assertTrue(all(v >= 0 for v in planet.res.values()))

    def test_requirements_are_respected(self):
        pl = P.Player(PRESETS["scientist"], seed=2)
        pl.run(24)
        for name, lv in pl.research.items():
            if lv:
                self.assertTrue(pl._met(P.OBJECTS[name]["requires"], pl.planets[0]), name)

    def test_a_crowded_player_founds_colonies_through_the_chain(self):
        r = P.play(replace(PRESETS["scientist"], expansion=1.0), hours=48, seed=1)
        kinds = [e[1] for e in r.events]
        self.assertIn("colony_founded", kinds)
        self.assertLess(kinds.index("expand"), kinds.index("colony_sent"))
        self.assertLess(kinds.index("colony_sent"), kinds.index("colony_founded"))
        self.assertGreaterEqual(r.final["planets"], 2)

    def test_no_appetite_no_colony_until_the_planet_is_full(self):
        r = P.play(replace(PRESETS["miner"], expansion=0.0), hours=6, seed=1)
        self.assertEqual(r.final["planets"], 1)

    def test_a_colony_has_its_own_economy(self):
        pl = P.Player(replace(PRESETS["scientist"], expansion=1.0), seed=1)
        pl.run(48)
        self.assertGreater(len(pl.planets), 1)
        self.assertGreater(sum(pl.planets[1].levels.values()), 0)

    def test_collector_makes_more_than_the_same_player_without_the_class(self):
        a = P.play(replace(PRESETS["miner"], character_class="collector"), hours=24, seed=1).final["worth"]
        b = P.play(replace(PRESETS["miner"], character_class=None), hours=24, seed=1).final["worth"]
        self.assertGreater(a, b)


class Tool(unittest.TestCase):
    def run_main(self, *args):
        out = io.StringIO()
        import sys
        old = sys.argv
        sys.argv = ["simulate_player.py", *args]
        try:
            with contextlib.redirect_stdout(out):
                code = S.main()
        finally:
            sys.argv = old
        return code, out.getvalue()

    def test_table_for_two_personalities(self):
        code, text = self.run_main("--personas", "miner,raider", "--hours", "6", "--seeds", "1")
        self.assertEqual(code, 0)
        self.assertIn("miner", text)
        self.assertIn("raider", text)

    def test_unknown_personality_is_refused(self):
        code, text = self.run_main("--personas", "wizard", "--hours", "1")
        self.assertEqual(code, 2)
        self.assertIn("unknown personality", text)

    def test_styles_mode_lists_every_style(self):
        code, text = self.run_main("--styles", "--personas", "miner", "--hours", "4", "--seeds", "1")
        self.assertEqual(code, 0)
        for style in ("plain", "mines", "lab", "factories", "power"):
            self.assertIn(f"miner/{style}", text)


if __name__ == "__main__":
    unittest.main()
