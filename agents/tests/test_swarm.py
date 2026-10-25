import random
import tempfile
import unittest
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.brain import can_pay, choose_building, choose_research, seconds_to_afford
from swarm.personas import PRESETS, make_persona
from tests.fakegame import FakeGame


def building(name, level=0, cost=None, met=True, afford=True):
    return {"machine_name": name, "level": level, "cost": cost or {"metal": 100, "crystal": 50, "deuterium": 0},
            "requirements_met": met, "can_afford": afford}


def planet(energy=10, res=None, prod=None):
    return {"id": 1, "energy": energy, "resources": res or {"metal": 1000, "crystal": 1000, "deuterium": 1000},
            "storage": {"metal": 10000, "crystal": 10000, "deuterium": 10000},
            "production_per_hour": prod or {"metal": 100, "crystal": 50, "deuterium": 10}}


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.p = PRESETS["balanced"]
        self.rng = random.Random(1)

    def test_low_energy_forces_solar_plant(self):
        pl = planet(energy=0)
        bs = [building("metal_mine", 5), building("solar_plant", 0)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "solar_plant")

    def test_low_energy_unaffordable_solar_waits_instead_of_buying_a_mine(self):
        pl = planet(energy=-5, res={"metal": 10, "crystal": 0, "deuterium": 0})
        bs = [building("metal_mine", 5), building("solar_plant", 0, cost={"metal": 75, "crystal": 30, "deuterium": 0}, afford=False)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertIsNone(c.machine_name)
        self.assertAlmostEqual(c.wait_seconds, max(65 / 100, 30 / 50) * 3600)

    def test_full_store_gets_built(self):
        pl = planet()
        pl["resources"]["metal"] = 9500
        bs = [building("metal_mine", 1), building("metal_store", 0)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "metal_store")

    def test_research_lab_not_chosen_while_researching(self):
        pl = planet()
        bs = [building("research_lab", 0)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=True, units_busy=False, have=dict(pl["resources"]))
        self.assertIsNone(c.machine_name)

    def test_shipyard_not_chosen_while_units_building(self):
        pl = planet()
        bs = [building("shipyard", 0)]
        c = choose_building(PRESETS["raider"], self.rng, pl, bs, research_busy=False, units_busy=True, have=dict(pl["resources"]))
        self.assertIsNone(c.machine_name)

    def test_requirements_not_met_are_skipped(self):
        pl = planet()
        bs = [building("fusion_plant", 0, met=False), building("metal_mine", 0)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "metal_mine")

    def test_zero_weight_buildings_never_chosen(self):
        pl = planet()
        bs = [building("terraformer", 0)]
        c = choose_building(self.p, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertIsNone(c.machine_name)

    def test_impatient_agent_buys_cheaper_thing(self):
        impatient = PRESETS["balanced"].__class__(**{**PRESETS["balanced"].__dict__, "patience": 0.0, "randomness": 0.0})
        pl = planet(res={"metal": 120, "crystal": 60, "deuterium": 0})
        bs = [building("metal_mine", 0, cost={"metal": 5000, "crystal": 0, "deuterium": 0}, afford=False),
              building("crystal_mine", 0, cost={"metal": 100, "crystal": 50, "deuterium": 0})]
        c = choose_building(impatient, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "crystal_mine")

    def test_does_not_save_for_unreachable_target(self):
        # Seen in the real game: research lab needs deuterium, production is 0 -> waiting is pointless.
        pl = planet(res={"metal": 300, "crystal": 100, "deuterium": 0}, prod={"metal": 30, "crystal": 15, "deuterium": 0})
        bs = [building("research_lab", 0, cost={"metal": 200, "crystal": 400, "deuterium": 200}, afford=False),
              building("metal_mine", 0, cost={"metal": 60, "crystal": 15, "deuterium": 0})]
        patient = PRESETS["scientist"].__class__(**{**PRESETS["scientist"].__dict__, "patience": 1.0, "randomness": 0.0})
        c = choose_building(patient, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "metal_mine")

    def test_does_not_save_for_target_hours_away(self):
        # Seen in the real game: "saving up for metal_store hint=45257" (12 hours).
        pl = planet(res={"metal": 10, "crystal": 0, "deuterium": 0}, prod={"metal": 30, "crystal": 15, "deuterium": 0})
        bs = [building("metal_store", 0, cost={"metal": 1000, "crystal": 0, "deuterium": 0}, afford=False),
              building("metal_mine", 0, cost={"metal": 5, "crystal": 0, "deuterium": 0})]
        patient = PRESETS["turtle"].__class__(**{**PRESETS["turtle"].__dict__, "patience": 1.0, "randomness": 0.0,
                                                  "building_weights": {"metal_store": 9.0, "metal_mine": 1.0}})
        c = choose_building(patient, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "metal_mine")

    def test_still_saves_for_a_near_target(self):
        pl = planet(res={"metal": 80, "crystal": 0, "deuterium": 0}, prod={"metal": 600, "crystal": 15, "deuterium": 0})
        bs = [building("metal_mine", 0, cost={"metal": 100, "crystal": 0, "deuterium": 0}, afford=False),
              building("crystal_mine", 0, cost={"metal": 10, "crystal": 0, "deuterium": 0})]
        patient = PRESETS["miner"].__class__(**{**PRESETS["miner"].__dict__, "patience": 1.0, "randomness": 0.0})
        c = choose_building(patient, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertIsNone(c.machine_name)
        self.assertAlmostEqual(c.wait_seconds, 20 / 600 * 3600)

    def test_builds_solar_before_a_mine_that_would_drain_the_power(self):
        pl = planet(energy=5)  # positive, but the next metal mine level needs more than 5
        bs = [building("metal_mine", 3), building("solar_plant", 1)]
        agent = PRESETS["miner"].__class__(**{**PRESETS["miner"].__dict__, "randomness": 0.0})
        c = choose_building(agent, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "solar_plant")

    def test_builds_solar_one_step_ahead_when_it_is_affordable_but_not_when_it_is_not(self):
        # enough power for the next mine level, not for the one after it
        from swarm.brain import extra_energy_needed
        need = extra_energy_needed("metal_mine", 3)
        agent = PRESETS["miner"].__class__(**{**PRESETS["miner"].__dict__, "randomness": 0.0, "energy_buffer": 0})
        bs = [building("metal_mine", 3), building("solar_plant", 1)]
        pl = planet(energy=need + need // 2)
        c = choose_building(agent, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual((c.machine_name, c.reason), ("solar_plant", "power ahead of metal_mine"))
        poor = [building("metal_mine", 3), building("solar_plant", 1, afford=False)]
        c = choose_building(agent, self.rng, pl, poor, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "metal_mine")   # no stall: the mine is still fine, the solar plant is just not affordable yet
        plenty = planet(energy=need * 5)
        c = choose_building(agent, self.rng, plenty, bs, research_busy=False, units_busy=False, have=dict(plenty["resources"]))
        self.assertEqual(c.machine_name, "metal_mine")

    def test_extra_energy_estimate(self):
        from swarm.brain import extra_energy_needed
        self.assertEqual(extra_energy_needed("solar_plant", 3), 0)
        self.assertGreater(extra_energy_needed("metal_mine", 5), extra_energy_needed("metal_mine", 1))

    def test_builds_the_producer_of_a_missing_resource(self):
        # Seen in the real game: research lab needs deuterium, but nothing produces deuterium.
        pl = planet(res={"metal": 300, "crystal": 500, "deuterium": 0}, prod={"metal": 30, "crystal": 15, "deuterium": 0})
        bs = [building("research_lab", 0, cost={"metal": 200, "crystal": 400, "deuterium": 200}, afford=False),
              building("deuterium_synthesizer", 0, cost={"metal": 225, "crystal": 75, "deuterium": 0}),
              building("metal_mine", 3)]
        sci = PRESETS["scientist"].__class__(**{**PRESETS["scientist"].__dict__, "randomness": 0.0})
        c = choose_building(sci, self.rng, pl, bs, research_busy=False, units_busy=False, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "deuterium_synthesizer")

    def test_research_choice(self):
        pl = planet()
        rs = [building("energy_technology", 0), building("not_wanted_tech", 0)]
        c = choose_research(self.p, self.rng, pl, rs, have=dict(pl["resources"]))
        self.assertEqual(c.machine_name, "energy_technology")

    def test_seconds_to_afford(self):
        self.assertEqual(seconds_to_afford({"metal": 100}, {"metal": 0}, {"metal": 100}), 3600)
        self.assertIsNone(seconds_to_afford({"deuterium": 5}, {"deuterium": 0}, {"deuterium": 0}))
        self.assertEqual(seconds_to_afford({"metal": 1}, {"metal": 5}, {}), 0)

    def test_personas_differ_and_vary(self):
        a = make_persona(0, random.Random(1))
        b = make_persona(5, random.Random(2))  # same preset (rotation of 5), different noise
        self.assertEqual(a.name, b.name)
        self.assertNotEqual(a.building_weights, b.building_weights)
        self.assertEqual(len({make_persona(i, random.Random(i)).name for i in range(5)}), 5)


class SimulationTests(unittest.TestCase):
    def run_agent(self, persona_name, seed, hours=30):
        game = FakeGame()
        tmp = tempfile.mkdtemp()
        store = TokenStore(str(Path(tmp) / "t.json"))
        rng = random.Random(seed)
        agent = Agent("sim", PRESETS[persona_name], "http://x", store, rng, log_dir=tmp, client=game)
        agent.log = lambda *a, **k: None  # keep test output quiet
        elapsed = 0.0
        while elapsed < hours * 3600:
            delay = agent.tick()
            self.assertGreater(delay, 0)
            game.advance(delay)
            elapsed += delay
        return game, agent

    def test_every_persona_plays_without_being_refused(self):
        for name in PRESETS:
            for seed in (1, 2, 3):
                game, agent = self.run_agent(name, seed)
                self.assertEqual(agent.stats["refused"], 0, f"{name}/{seed} was refused")
                self.assertGreater(agent.stats["builds"], 10, f"{name}/{seed} barely played")
                self.assertGreaterEqual(game.levels["solar_plant"], 1, f"{name}/{seed} never built power")

    def test_scientist_researches_miner_builds_more_mines(self):
        sci, a1 = self.run_agent("scientist", 7)
        miner, a2 = self.run_agent("miner", 7)
        self.assertGreater(a1.stats["researches"], 0)
        self.assertGreaterEqual(sci.levels["research_lab"], 1)
        self.assertGreater(miner.levels["metal_mine"], miner.levels["research_lab"])

    def test_same_seed_same_game(self):
        g1, _ = self.run_agent("balanced", 42, hours=10)
        g2, _ = self.run_agent("balanced", 42, hours=10)
        self.assertEqual(g1.levels, g2.levels)


if __name__ == "__main__":
    unittest.main()
