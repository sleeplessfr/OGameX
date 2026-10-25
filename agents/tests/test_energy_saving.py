import random
import tempfile
import unittest
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.personas import PRESETS
from tests.fakegame import FakeGame


class PowerFirstTests(unittest.TestCase):
    def play(self, seed, solar_level):
        game = FakeGame()
        game.levels.update({"metal_mine": 20, "crystal_mine": 15, "deuterium_synthesizer": 10, "solar_plant": solar_level,
                            "shipyard": 2, "research_lab": 1})
        game.res.update({"metal": 5000, "crystal": 2000, "deuterium": 500})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", PRESETS["raider"], "x", store, random.Random(seed), client=game)
        agent.log = lambda *a, **k: None
        agent.user_id = 1
        for _ in range(6):
            agent.tick()
        return game, agent

    def test_a_raider_short_of_power_does_not_spend_the_stock_on_ships(self):
        for seed in range(1, 25):
            game, _ = self.play(seed, solar_level=15)      # power is short and the next solar plant is out of reach
            self.assertLess(game.energy_prod(), game.energy_need())
            orders = [p for p, b in game.post_log if p.endswith("/units")]
            self.assertEqual(orders, [], f"seed {seed}")

    def test_with_enough_power_it_still_buys_ships(self):
        bought = 0
        for seed in range(1, 25):
            game, _ = self.play(seed, solar_level=30)
            bought += sum(1 for p, b in game.post_log if p.endswith("/units"))
        self.assertGreater(bought, 0)


if __name__ == "__main__":
    unittest.main()
