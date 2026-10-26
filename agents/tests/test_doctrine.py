import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import summarize_logs as sl
from swarm import doctrine as d
from swarm.agent import Agent, TokenStore
from swarm.military import Memory, choose_units
from swarm.personas import PRESETS
from tests.fakegame import FakeGame

RAIDER, MINER = PRESETS["raider"], PRESETS["miner"]
RL = {"rocket_launcher": 1500}


def unit(name, cost, cat="ship", n=1000):
    return {"machine_name": name, "category": cat, "requirements_met": True, "character_class_met": True,
            "max_buildable": n, "cost_per_unit": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}}


class SimulatorRankingTests(unittest.TestCase):
    def test_cruisers_and_bombers_beat_light_fighters_against_rocket_launchers(self):
        table = {r["ship"]: r for r in d.evaluate(d.WARSHIPS, RL)}
        self.assertEqual(d.rank_ships(d.WARSHIPS, RL)[0][0], "cruiser")        # rapid fire against rocket launchers
        self.assertGreater(table["cruiser"]["score"], table["light_fighter"]["score"])
        self.assertGreater(table["bomber"]["score"], table["light_fighter"]["score"])
        self.assertGreater(table["cruiser"]["win"], table["light_fighter"]["win"])
        self.assertGreater(table["light_fighter"]["score"], table["destroyer"]["score"])

    def test_ranking_is_repeatable_and_does_not_use_up_anyones_dice(self):
        state = random.getstate()
        a = d.rank_ships(d.WARSHIPS, RL, (1, 1, 1))
        self.assertEqual(random.getstate(), state)
        self.assertEqual(a, d.rank_ships(d.WARSHIPS, RL, (1, 1, 1)))

    def test_reaper_only_for_the_general_class(self):
        self.assertIn("reaper", d.candidates(RAIDER))
        self.assertNotIn("reaper", d.candidates(replace(RAIDER, character_class="collector")))

    def test_prices_use_the_3_2_1_rates(self):
        self.assertEqual(d.price("light_fighter"), 3000 + 1.5 * 1000)
        self.assertEqual(d.price("cruiser"), 20000 + 1.5 * 7000 + 3 * 2000)

    def test_typical_defender_is_the_average_of_recent_reports_and_falls_back(self):
        intel = {(1, 1, 1): {"at": 100.0, "defense": {"rocket_launcher": 1000, "light_laser": 200}, "ships": {"small_cargo": 4}},
                 (1, 1, 2): {"at": 100.0, "defense": {"rocket_launcher": 2000}, "ships": {}},
                 (1, 1, 3): {"at": 100.0, "defense": None, "ships": {"light_fighter": 99}},       # defence not shown: unusable
                 (1, 1, 4): {"at": 0.0, "defense": {"plasma_turret": 50}}}                          # too old
        mix, why = d.typical_defenders(intel, 100.0 + d.INTEL_AGE - 1)
        self.assertEqual(mix, {"rocket_launcher": 1500, "light_laser": 100, "small_cargo": 2})
        self.assertIn("2 recent", why)
        mix, why = d.typical_defenders(intel, 10 ** 7)
        self.assertEqual(mix, d.FALLBACK_DEFENDERS)
        self.assertIn("rocket launchers", why)

    def test_warship_weights_follow_the_scores_among_buildable_ships(self):
        w = d.warship_weights(RAIDER, [("cruiser", 0.2), ("bomber", 0.1), ("light_fighter", 0.05)], {"light_fighter", "bomber"})
        self.assertEqual(set(w), {"light_fighter", "bomber"})
        self.assertAlmostEqual(w["bomber"], 3.0)                    # the best buildable one gets the persona's top weight
        self.assertAlmostEqual(w["light_fighter"], 3.0 * 0.25)      # half the score -> a quarter of the weight
        self.assertEqual(d.warship_weights(RAIDER, [("cruiser", 0.2)], {"light_fighter"}), {})


class ChooseUnitsTests(unittest.TestCase):
    UNITS = [unit("light_fighter", (3000, 1000, 0)), unit("cruiser", (20000, 7000, 2000)), unit("small_cargo", (2000, 2000, 0))]
    HAVE = {"metal": 10 ** 6, "crystal": 10 ** 6, "deuterium": 10 ** 6}

    def test_doctrine_replaces_the_fixed_weights(self):
        p = replace(RAIDER, randomness=0.0, probes_wanted=0)
        self.assertEqual(choose_units(p, random.Random(1), self.UNITS, self.HAVE, {})[0], "light_fighter")   # fixed weights: 3.0 vs 2.0
        pick = choose_units(p, random.Random(1), self.UNITS, self.HAVE, {}, ranking=[("cruiser", 0.2), ("light_fighter", 0.05)])
        self.assertEqual(pick[0], "cruiser")

    def test_same_dice_with_or_without_a_ranking(self):
        p = RAIDER
        a, b = random.Random(7), random.Random(7)
        choose_units(p, a, self.UNITS, self.HAVE, {})
        choose_units(p, b, self.UNITS, self.HAVE, {}, ranking=[("cruiser", 0.2)])
        self.assertEqual(a.random(), b.random())

    def test_share_argument_limits_the_spending(self):
        p = replace(RAIDER, randomness=0.0, probes_wanted=0)
        name, n = choose_units(p, random.Random(1), self.UNITS, {"metal": 100_000, "crystal": 100_000, "deuterium": 0}, {}, share=0.1)
        self.assertEqual((name, n), ("light_fighter", 3))     # 10% of 100,000 metal = 10,000 = 3 light fighters


class EvidenceTests(unittest.TestCase):
    def test_hopeless_reasons_are_recognised(self):
        self.assertTrue(d.out_of_reach("simulated fight: win chance 0%, expected loss 1,500,000 for loot 25,000; wants at least 57%"))
        self.assertTrue(d.out_of_reach("simulated fight: win chance 8%, expected loss 1 for loot 2; wants at least 57%"))
        self.assertFalse(d.out_of_reach("simulated fight: win chance 40%, expected loss 1 for loot 2; wants at least 57%"))
        self.assertFalse(d.out_of_reach("fleet cannot carry enough of the loot, wants cargo ships"))
        self.assertFalse(d.out_of_reach(""))

    def skips(self, n, at=1000.0):
        st = d.new_state()
        for i in range(n):
            d.note_skip(st, at + i)
        return st

    def test_share_rises_with_skips_capped_and_falls_back(self):
        base = RAIDER.army_share
        self.assertEqual(d.army_share_now(RAIDER, self.skips(2), 1100.0)[0], base)          # not yet
        s3 = d.army_share_now(RAIDER, self.skips(3), 1100.0)[0]
        s6 = d.army_share_now(RAIDER, self.skips(6), 1100.0)[0]
        self.assertTrue(base < s3 < s6 <= d.PUSH_CAP)
        self.assertEqual(d.army_share_now(RAIDER, self.skips(30), 1100.0)[0], d.PUSH_CAP)
        self.assertEqual(d.army_share_now(RAIDER, self.skips(6), 1100.0 + d.WINDOW + 100)[0], base)   # old skips are forgotten
        st = self.skips(6)
        d.note_attack(st, 1050.0)
        d.note_attack(st, 1060.0)                                                            # attacks started again
        self.assertEqual(d.army_share_now(RAIDER, st, 1100.0)[0], base)

    def test_only_fighting_personas_push(self):
        self.assertFalse(d.applies(MINER))
        self.assertEqual(d.army_share_now(MINER, self.skips(20), 1100.0), (MINER.army_share, ""))
        self.assertTrue(d.applies(PRESETS["balanced"]))
        self.assertTrue(d.applies(replace(MINER, character_class="general")))

    def test_surplus_keeps_the_reserve(self):
        self.assertEqual(d.surplus({"metal": 900, "crystal": 50, "deuterium": 10}, {"metal": 841, "crystal": 100}),
                         {"metal": 59, "crystal": 0, "deuterium": 10})
        self.assertEqual(d.surplus({"metal": 5}, None), {"metal": 5})

    def test_state_survives_a_save_and_junk_is_dropped(self):
        m = Memory()
        d.note_skip(m.doctrine, 5.0)
        m.doctrine.update(choice="cruiser", aim="bomber", share=0.6, table=[["cruiser", 0.2]])
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.doctrine, m.doctrine)
        self.assertEqual(Memory.from_dict({}).doctrine, d.new_state())
        junk = Memory.from_dict({"doctrine": {"skips": ["x", 3, True], "choice": 5, "table": [["a"], ["b", "c"], ["ok", 1]], "share": "x"}}).doctrine
        self.assertEqual((junk["skips"], junk["choice"], junk["table"], junk["share"]), ([3.0], None, [["ok", 1.0]], None))
        self.assertEqual(Memory.from_dict({"doctrine": 7}).doctrine, d.new_state())


class ResearchGoalTests(unittest.TestCase):
    RANKING = [("cruiser", 0.25), ("bomber", 0.19), ("light_fighter", 0.12)]

    def pushing_state(self):
        st = d.new_state()
        for i in range(4):
            d.note_skip(st, 1000.0 + i)
        return st

    def test_the_whole_chain_of_a_cruiser_is_listed(self):
        need = d.needs_of("cruiser")
        for name, lv in (("shipyard", 5), ("impulse_drive", 4), ("ion_technology", 2), ("laser_technology", 5),
                         ("energy_technology", 4), ("research_lab", 4)):
            self.assertGreaterEqual(need.get(name, 0), lv, name)

    def test_goals_for_a_pushing_raider(self):
        b, r = d.research_goals(RAIDER, self.pushing_state(), 1100.0, self.RANKING, {"energy_technology": 4})
        self.assertGreaterEqual(b["shipyard"], 5)
        self.assertGreaterEqual(b["research_lab"], 6)                       # shielding technology needs lab 6
        self.assertNotIn("energy_technology", r)                            # already reached
        for name in ("impulse_drive", "ion_technology", "laser_technology", "weapon_technology", "armor_technology", "shielding_technology"):
            self.assertIn(name, r)
        names = list(r)
        self.assertLess(names.index("laser_technology"), names.index("ion_technology"))   # prerequisites first

    def test_no_goals_without_a_run_of_hopeless_targets_or_for_peaceful_personas(self):
        self.assertEqual(d.research_goals(RAIDER, d.new_state(), 1100.0, self.RANKING, {}), ({}, {}))
        self.assertEqual(d.research_goals(MINER, self.pushing_state(), 1100.0, self.RANKING, {}), ({}, {}))
        self.assertEqual(d.research_goals(RAIDER, self.pushing_state(), 1100.0, None, {}), ({}, {}))
        self.assertEqual(d.research_goals(RAIDER, self.pushing_state(), 1100.0 + d.WINDOW + 5, self.RANKING, {}), ({}, {}))

    def test_a_near_ship_beats_a_far_slightly_better_one(self):
        ranking = [("reaper", 0.20), ("cruiser", 0.19)]
        self.assertEqual(d.aim_ship(ranking, {}), "cruiser")


class CostlyFakeGame(FakeGame):
    """A game whose solar plant is very dear in deuterium, so that an agent with a rich stock still waits for it."""
    SOLAR = {"metal": 2_500_000, "crystal": 1_000_000, "deuterium": 500_000}

    def cost(self, name, level_now, table):
        if name == "solar_plant" and table is not None and name in table:
            return dict(self.SOLAR)
        return super().cost(name, level_now, table)


class AgentTests(unittest.TestCase):
    def make(self, persona=RAIDER, game=None, seed=3):
        game = game or CostlyFakeGame()
        game.slots_max = 3
        game.levels.update({"shipyard": 4, "metal_mine": 5, "crystal_mine": 4})       # no solar plant: the energy is short
        game.storage = lambda: {"metal": 10 ** 8, "crystal": 10 ** 8, "deuterium": 10 ** 8}
        game.ships["espionage_probe"] = 3
        game.advance(100)
        game.res.update({"metal": 3_000_000, "crystal": 3_000_000, "deuterium": 100_000})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(seed), client=game)
        agent.events = []
        agent.log = lambda event, **k: agent.events.append((event, k))
        agent.user_id = 1
        return game, agent

    def round(self, game, agent):
        status = game.get("/status")
        agent.now = float(status["server_time"])
        home = status["planets"][0]
        have = dict(home["resources"])
        agent._economy_saving, agent._economy_reserve, agent._economy_forced = False, {}, False
        agent._plan_buildings(home, have, False)
        agent._plan_military(status, home, have, agent._economy_saving, agent._economy_reserve)
        return home, have

    def test_a_rich_raider_waiting_for_a_solar_plant_still_orders_ships_and_the_reserve_stays_intact(self):
        game, agent = self.make()
        self.round(game, agent)
        waits = [k for e, k in agent.events if e == "wait"]
        self.assertTrue(waits and "low energy" in waits[0]["why"], waits)
        self.assertGreater(game.ships["light_fighter"], 0)
        # the deuterium of the plant was short, so metal and crystal beyond the plant's price are the surplus
        self.assertGreaterEqual(game.res["metal"], CostlyFakeGame.SOLAR["metal"])
        self.assertGreaterEqual(game.res["crystal"], CostlyFakeGame.SOLAR["crystal"])
        self.assertGreaterEqual(game.res["deuterium"], 100_000)

    def test_it_never_dips_into_the_reserve_however_often_it_looks(self):
        for seed in range(1, 8):
            game, agent = self.make(seed=seed)
            for _ in range(12):
                self.round(game, agent)
                game.advance(30)
            self.assertGreaterEqual(game.res["metal"], CostlyFakeGame.SOLAR["metal"], f"seed {seed}")
            self.assertGreaterEqual(game.res["crystal"], CostlyFakeGame.SOLAR["crystal"], f"seed {seed}")

    def test_nothing_is_ordered_when_there_is_no_surplus(self):
        game, agent = self.make()
        game.res.update({"metal": 2_500_000, "crystal": 1_000_000})
        self.round(game, agent)
        self.assertEqual(game.ships["light_fighter"], 0)

    def test_hopeless_skips_raise_the_army_share_and_log_it(self):
        game, agent = self.make()
        for i in range(5):
            d.note_skip(agent.memory.doctrine, 50.0 + i)
        self.round(game, agent)
        push = [k for e, k in agent.events if e == "army_push"]
        self.assertEqual(len(push), 1)
        self.assertGreater(push[0]["share"], RAIDER.army_share)
        self.assertLessEqual(push[0]["share"], d.PUSH_CAP)
        self.round(game, agent)
        self.assertEqual(len([1 for e, _ in agent.events if e == "army_push"]), 1)          # logged once, not every round

    def test_a_defence_in_the_reports_makes_the_agent_log_its_doctrine_once(self):
        game, agent = self.make()
        agent.memory.intel[(1, 1, 5)] = {"at": 100.0, "loot": 1000, "defenders": 1500, "defense": {"rocket_launcher": 1500}, "ships": {}}
        game.levels.update({"shipyard": 5, "research_lab": 4})
        self.round(game, agent)
        self.round(game, agent)
        logged = [k for e, k in agent.events if e == "doctrine"]
        self.assertEqual(len(logged), 1)
        self.assertIn("1500 rocket_launcher", logged[0]["why"])
        self.assertTrue(logged[0]["table"])
        self.assertEqual(agent.memory.doctrine["aim"], logged[0]["aim"])

    def test_research_goals_reach_the_agent_only_after_skips(self):
        game, agent = self.make()
        status = game.get("/status")
        agent.now = 110.0
        self.assertEqual(agent._colony_goals(status, status["planets"][0]), (None, None))
        for i in range(4):
            d.note_skip(agent.memory.doctrine, 100.0 + i)
        agent.memory.intel[(1, 1, 5)] = {"at": 100.0, "loot": 1000, "defenders": 1500, "defense": RL, "ships": {}}
        buildings, research = agent._colony_goals(status, status["planets"][0])
        self.assertGreaterEqual(buildings["shipyard"], 5)
        self.assertIn("weapon_technology", research)

    def test_a_miner_is_unchanged(self):
        game, agent = self.make(MINER)
        for i in range(6):
            d.note_skip(agent.memory.doctrine, 50.0 + i)
        self.round(game, agent)
        self.assertNotIn("army_push", [e for e, _ in agent.events])
        self.assertNotIn("doctrine", [e for e, _ in agent.events])
        self.assertEqual(agent._colony_goals(game.get("/status"), game.get("/status")["planets"][0]), (None, None))


class ReportTests(unittest.TestCase):
    def test_doctrine_section_shows_the_last_choice_and_why(self):
        t = 1_700_000_000
        events = [
            {"t": t, "agent": "a1", "event": "doctrine", "chosen": "light_fighter", "aim": "cruiser",
             "table": [["cruiser", 0.25], ["light_fighter", 0.12]], "why": "against rocket_launcher 1500 (fallback)"},
            {"t": t + 5, "agent": "a1", "event": "doctrine", "chosen": "cruiser", "aim": "cruiser",
             "table": [["cruiser", 0.25], ["bomber", 0.19]], "why": "against rocket_launcher 1500 (average of 3 recent spy reports): best is cruiser"},
            {"t": t + 6, "agent": "a1", "event": "army_push", "share": 0.65, "base": 0.5, "why": "4 recent targets out of reach"},
        ]
        text = sl.summarize(events)
        self.assertIn("DOCTRINE", text)
        self.assertIn("a1: buys cruiser, aims for cruiser (2 changes)", text)
        self.assertIn("why: against rocket_launcher 1500 (average of 3 recent spy reports): best is cruiser", text)
        self.assertIn("scores: cruiser 0.25, bomber 0.19", text)
        self.assertIn("army push: 1 time, share now 0.65 (normally 0.5)", text)

    def test_doctrine_section_says_none_without_events(self):
        text = sl.summarize([{"t": 1000.0, "agent": "a1", "event": "build", "what": "metal_mine"}])
        self.assertRegex(text, r"DOCTRINE[^\n]*\n  none")


if __name__ == "__main__":
    unittest.main()
