import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.military import (Intel, Memory, assess_attack, build_strike, can_carry_enough, choose_units, judge_trade, summarize_battle, count_fighters, distance,
                            pick_attack, pick_target, summarize_report)
from swarm.personas import PRESETS
from tests.fakegame import FakeGame


def player(uid, galaxy=1, system=2, position=5, score=0, is_me=False):
    return {"user_id": uid, "username": f"p{uid}", "score": score, "is_me": is_me,
            "main_planet": {"galaxy": galaxy, "system": system, "position": position}}


RAIDER = replace(PRESETS["raider"], randomness=0.0)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(1)
        self.me = (1, 1, 1)

    def pick(self, players, **kw):
        args = dict(persona=RAIDER, rng=self.rng, me=self.me, my_score=1000, players=players, memory=Memory(), now=10_000.0,
                    allowed_ids=None, cooldown=1800.0)
        args.update(kw)
        return pick_target(**args)

    def test_never_picks_myself_or_non_agents(self):
        players = [player(1, is_me=True), player(2), player(3)]
        t = self.pick(players, allowed_ids={2})
        self.assertEqual(t["user_id"], 2)
        self.assertIsNone(self.pick([player(3)], allowed_ids={2}))
        self.assertIsNone(self.pick([player(3)], allowed_ids=set()))  # no known agents = nobody is a target

    def test_none_allowed_means_anyone(self):
        self.assertIsNotNone(self.pick([player(3)], allowed_ids=None))

    def test_skips_much_stronger_players(self):
        self.assertIsNone(self.pick([player(2, score=100_000)]))
        self.assertIsNotNone(self.pick([player(2, score=1200)]))  # raider accepts a bit stronger

    def test_peaceful_personas_avoid_stronger_players_more(self):
        miner = replace(PRESETS["miner"], randomness=0.0)
        self.assertIsNone(self.pick([player(2, score=1400)], persona=miner))

    def test_respects_cooldown_and_avoid_list(self):
        mem = Memory()
        mem.spied[(1, 2, 5)] = 9_500.0
        self.assertIsNone(self.pick([player(2)], memory=mem))
        mem = Memory()
        mem.avoid_until[(1, 2, 5)] = 20_000.0
        self.assertIsNone(self.pick([player(2)], memory=mem))

    def test_too_far_is_skipped_and_nearest_preferred(self):
        self.assertIsNone(self.pick([player(2, galaxy=9)]))
        near, far = player(2, system=3), player(3, system=300)
        picks = {self.pick([near, far])["user_id"] for _ in range(20)}
        self.assertIn(2, picks)  # the nearest is always among the candidates

    def test_distance_makes_galaxy_jumps_expensive(self):
        self.assertGreater(distance((1, 1, 1), (2, 1, 1)), distance((1, 1, 1), (1, 400, 1)))


class ReportTests(unittest.TestCase):
    def test_summary(self):
        intel = summarize_report({"target": {"owner_id": 7}, "resources": {"metal": 1000, "crystal": 500, "deuterium": 500},
                                  "ships": {"small_cargo": 2}, "defense": {"rocket_launcher": 3}})
        self.assertEqual((intel.loot, intel.defenders, intel.owner_id), (1000, 5, 7))

    def test_unknown_defenses_when_report_has_no_ships_or_defense(self):
        intel = summarize_report({"resources": {"metal": 100}, "ships": None, "defense": None})
        self.assertIsNone(intel.defenders)

    def test_attack_decision(self):
        rng = random.Random(0)
        eager = replace(RAIDER, aggression=1.0, min_loot=500, risk_ratio=1.0)
        self.assertTrue(assess_attack(eager, rng, Intel(5000, 3, None), 10)[0])
        self.assertFalse(assess_attack(eager, rng, Intel(100, 0, None), 10)[0])      # loot too small
        self.assertFalse(assess_attack(eager, rng, Intel(5000, 50, None), 10)[0])    # too well defended
        self.assertFalse(assess_attack(eager, rng, Intel(5000, 0, None), 0)[0])      # no fighters
        pacifist = replace(eager, aggression=0.0)
        self.assertFalse(assess_attack(pacifist, rng, Intel(5000, 0, None), 10)[0])

    def test_unknown_defenses_only_a_gamble(self):
        never = replace(RAIDER, gamble=0.0)
        always = replace(RAIDER, gamble=1.0)
        self.assertFalse(assess_attack(never, random.Random(0), Intel(5000, None, None), 10)[0])
        self.assertTrue(assess_attack(always, random.Random(0), Intel(5000, None, None), 10)[0])

    def test_pick_attack_prefers_richest_fresh_target_and_ignores_old_or_recent(self):
        eager = replace(RAIDER, aggression=1.0, risk_ratio=10.0)
        mem = Memory()
        mem.intel[(1, 2, 5)] = {"at": 9_000.0, "loot": 3000, "defenders": 0, "owner_id": 2}
        mem.intel[(1, 2, 6)] = {"at": 9_000.0, "loot": 9000, "defenders": 0, "owner_id": 3}
        mem.intel[(1, 2, 7)] = {"at": 0.0, "loot": 99999, "defenders": 0, "owner_id": 4}        # report too old
        mem.intel[(1, 2, 8)] = {"at": 9_500.0, "loot": 99999, "defenders": 0, "owner_id": 5}
        mem.attacked[(1, 2, 8)] = 9_900.0                                                        # hit a moment ago
        coord, intel, _ = pick_attack(eager, random.Random(0), mem, 10_000.0, {"light_fighter": 10})
        self.assertEqual(coord, (1, 2, 6))


class BattleLearningTests(unittest.TestCase):
    # shaped like the real report of the raider's attack on the turtle (numbers from the owner's server)
    REAL = {"target": {"galaxy": 1, "system": 3, "position": 4, "type": 1, "owner_id": 3},
            "attacker": {"player_id": 4, "resource_loss": 52000, "units": {"light_fighter": 13}},
            "defender": {"player_id": 3, "resource_loss": 14000, "units": {"rocket_launcher": 14}},
            "loot": {"metal": 0, "crystal": 0, "deuterium": 0, "percentage": 50},
            "debris": {"metal": 11700, "crystal": 3900, "deuterium": 0}, "rounds": 5}

    def test_summary_of_the_real_defeat(self):
        b = summarize_battle(self.REAL, my_user_id=4)
        self.assertEqual((b["role"], b["my_loss"], b["their_loss"], b["loot"], b["debris"]), ("attacker", 52000, 14000, 0, 15600))
        self.assertEqual(b["at"], "1:3:4")
        self.assertEqual(judge_trade(b), "bad")

    def test_defender_view_and_strangers(self):
        self.assertEqual(summarize_battle(self.REAL, my_user_id=3)["role"], "defender")
        self.assertIsNone(summarize_battle(self.REAL, my_user_id=99))

    def test_shot_down_probes_are_recognised(self):
        report = {**self.REAL, "attacker": {"player_id": 4, "resource_loss": 1000, "units": {"espionage_probe": 1}}}
        self.assertTrue(summarize_battle(report, 4)["probes_only"])

    def test_trades(self):
        win = {"loot": 30000, "their_loss": 0, "my_loss": 0}
        self.assertEqual(judge_trade(win), "good")
        self.assertEqual(judge_trade({"loot": 0, "their_loss": 0, "my_loss": 0}), "neutral")
        self.assertEqual(judge_trade({"loot": 1000, "their_loss": 5000, "my_loss": 7000}), "neutral")
        self.assertEqual(judge_trade({"loot": 100, "their_loss": 100, "my_loss": 9000}), "bad")

    def test_caution_shrinks_the_risk_a_raider_accepts(self):
        intel = Intel(loot=9000, defenders=10, owner_id=2)
        persona = replace(RAIDER, aggression=1.0, risk_ratio=1.0)
        self.assertTrue(assess_attack(persona, random.Random(1), intel, 12, risk_scale=1.0)[0])
        self.assertFalse(assess_attack(persona, random.Random(1), intel, 12, risk_scale=0.5)[0])


class FleetAndShopTests(unittest.TestCase):
    STATS = {"small_cargo": {"cargo_capacity": 5000}, "large_cargo": {"cargo_capacity": 25000},
             "light_fighter": {"cargo_capacity": 50}}

    def test_strike_takes_only_a_fraction_of_fighters_and_enough_cargo(self):
        persona = replace(RAIDER, strike_fraction=0.5)
        fleet = build_strike(persona, {"light_fighter": 10, "small_cargo": 10, "espionage_probe": 4}, self.STATS, loot=12_000)
        self.assertEqual(fleet["light_fighter"], 5)
        self.assertEqual(fleet["small_cargo"], 3)          # 12000 - 5*50 = 11750 -> 3 cargos of 5000
        self.assertNotIn("espionage_probe", fleet)

    def test_fleet_must_be_able_to_carry_a_decent_part_of_the_loot(self):
        # real server case: 1 light fighter (cargo 50) sent after 28,447 loot
        self.assertFalse(can_carry_enough(RAIDER, {"light_fighter": 1}, self.STATS, 28_447))
        self.assertFalse(can_carry_enough(RAIDER, {"light_fighter": 13}, self.STATS, 80_307))
        self.assertTrue(can_carry_enough(RAIDER, {"light_fighter": 10, "small_cargo": 2}, self.STATS, 10_000))
        self.assertTrue(can_carry_enough(replace(RAIDER, min_loot=10), {"light_fighter": 1}, self.STATS, 150))  # tiny loot, tiny haul is fine

    def test_cargo_ships_come_first_when_loot_could_not_be_carried(self):
        units = [self.unit("light_fighter", (3000, 1000, 0)), self.unit("small_cargo", (2000, 2000, 0))]
        have = {"metal": 1_000_000, "crystal": 1_000_000, "deuterium": 0}
        name, _ = choose_units(RAIDER, random.Random(1), units, have, {"espionage_probe": 9}, want_cargo=True)
        self.assertEqual(name, "small_cargo")

    def test_pick_attack_skips_targets_skipped_a_moment_ago(self):
        memory = Memory(intel={(1, 1, 1): {"at": 0, "loot": 9000, "defenders": 0, "owner_id": 2, "skip_until": 500}})
        self.assertIsNone(pick_attack(replace(RAIDER, aggression=1.0), random.Random(1), memory, 100, {"light_fighter": 5}))
        self.assertIsNotNone(pick_attack(replace(RAIDER, aggression=1.0), random.Random(1), memory, 600, {"light_fighter": 5}))

    def test_a_stock_poor_agent_can_still_afford_cargo_ships_when_it_needs_them(self):
        # real server case: the balanced agent kept its stock low and wanted cargo ships for hours without ever
        # affording one under its 20% army share (a small cargo costs 2000 metal + 2000 crystal)
        persona = replace(PRESETS["balanced"], probes_wanted=0)
        units = [self.unit("small_cargo", (2000, 2000, 0)), self.unit("light_fighter", (3000, 1000, 0))]
        have = {"metal": 9000, "crystal": 9000, "deuterium": 0}
        name, amount = choose_units(persona, random.Random(1), units, have, {}, want_cargo=True)
        self.assertEqual((name, amount), ("small_cargo", 2))   # half of the stock, 2 ships
        rich = {"metal": 10_000_000, "crystal": 10_000_000, "deuterium": 0}
        self.assertEqual(choose_units(persona, random.Random(1), units, rich, {}, want_cargo=True)[1], 10)   # never a huge order

    def test_old_spy_reports_are_not_trusted(self):
        persona = replace(RAIDER, aggression=1.0)
        memory = Memory(intel={(1, 1, 1): {"at": 0, "loot": 9000, "defenders": 0, "owner_id": 2}})
        self.assertIsNotNone(pick_attack(persona, random.Random(1), memory, 600, {"light_fighter": 5}))
        self.assertIsNone(pick_attack(persona, random.Random(1), memory, 3600, {"light_fighter": 5}))   # an hour old

    def test_no_gambling_with_an_expensive_fleet_but_a_cheap_one_may(self):
        always = replace(RAIDER, gamble=1.0, min_loot=10)
        unknown = Intel(30_000, None, None)
        self.assertFalse(assess_attack(always, random.Random(0), unknown, 20, at_risk=80_000)[0])   # real case: 68,000 lost for nothing
        self.assertTrue(assess_attack(always, random.Random(0), unknown, 3, at_risk=12_000)[0])
        self.assertTrue(assess_attack(always, random.Random(0), unknown, 20)[0])   # no value given = old behaviour

    def test_strike_needs_fighters(self):
        self.assertIsNone(build_strike(RAIDER, {"small_cargo": 5}, self.STATS, 1000))
        self.assertEqual(count_fighters({"light_fighter": 3, "cruiser": 2, "small_cargo": 9}), 5)

    def test_strike_never_asks_for_more_cargo_than_it_has(self):
        fleet = build_strike(RAIDER, {"light_fighter": 10, "small_cargo": 1}, self.STATS, loot=1_000_000)
        self.assertEqual(fleet["small_cargo"], 1)

    def unit(self, name, cost, maxb=100, met=True, cat="ship"):
        return {"machine_name": name, "category": cat, "requirements_met": met, "character_class_met": True,
                "cost_per_unit": {"metal": cost[0], "crystal": cost[1], "deuterium": cost[2]}, "max_buildable": maxb}

    def test_probes_come_first_and_only_up_to_the_wanted_stock(self):
        units = [self.unit("espionage_probe", (0, 300, 0)), self.unit("light_fighter", (600, 200, 0))]
        have = {"metal": 10_000, "crystal": 10_000, "deuterium": 0}
        pick = choose_units(RAIDER, random.Random(0), units, have, {"espionage_probe": 1})
        self.assertEqual(pick, ("espionage_probe", RAIDER.probes_wanted - 1))
        pick = choose_units(RAIDER, random.Random(0), units, have, {"espionage_probe": RAIDER.probes_wanted})
        self.assertEqual(pick[0], "light_fighter")

    def test_spends_at_most_army_share_of_what_is_spare(self):
        units = [self.unit("light_fighter", (1000, 0, 0))]
        persona = replace(RAIDER, army_share=0.5, probes_wanted=0)
        pick = choose_units(persona, random.Random(0), units, {"metal": 10_000, "crystal": 0, "deuterium": 0}, {})
        self.assertEqual(pick, ("light_fighter", 5))

    def test_nothing_when_requirements_missing_or_peaceful(self):
        persona = replace(RAIDER, probes_wanted=0)
        units = [self.unit("light_fighter", (600, 200, 0), met=False)]
        self.assertIsNone(choose_units(persona, random.Random(0), units, {"metal": 9999, "crystal": 9999, "deuterium": 0}, {}))
        none = replace(PRESETS["scientist"], army_share=0.0, probes_wanted=0)
        self.assertIsNone(choose_units(none, random.Random(0), [self.unit("small_cargo", (1, 1, 0))], {"metal": 9999, "crystal": 9999, "deuterium": 0}, {}))

    def test_turtle_builds_defense(self):
        turtle = replace(PRESETS["turtle"], randomness=0.0, probes_wanted=0)
        units = [self.unit("rocket_launcher", (500, 0, 0), cat="defense"), self.unit("small_cargo", (500, 500, 0))]
        pick = choose_units(turtle, random.Random(0), units, {"metal": 10_000, "crystal": 10_000, "deuterium": 0}, {})
        self.assertEqual(pick[0], "rocket_launcher")


class SwarmBehaviourTests(unittest.TestCase):
    def play(self, persona_name, seed, *, victim_is_agent=True, hours=60):
        game = FakeGame()
        tmp = tempfile.mkdtemp()
        store = TokenStore(str(Path(tmp) / "t.json"))
        if victim_is_agent:
            store.put("victim", {"user_id": 2})
        agent = Agent("sim", PRESETS[persona_name], "x", store, random.Random(seed), client=game)
        agent.log = lambda *a, **k: None
        agent.user_id = 1  # the fake game's player (the real agent learns its id when it logs in)
        elapsed = 0.0
        while elapsed < hours * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        return game, agent

    def test_raider_spies_reads_reports_and_attacks_without_being_refused(self):
        for seed in (1, 2, 3):
            game, agent = self.play("raider", seed)
            self.assertGreater(agent.stats["spies"], 0)
            self.assertGreater(agent.stats["reports"], 0)
            self.assertGreater(agent.stats["attacks"], 0, f"seed {seed}")
            self.assertEqual(agent.stats["refused"], 0)
            self.assertGreater(game.attacks_resolved, 0)

    def test_raider_reads_battle_results_and_gets_more_careful_after_a_defeat(self):
        lessons = 0
        for seed in (1, 2, 3, 4):
            game, agent = self.play("raider", seed, hours=80)
            if game.battle_losses > 0 and agent.stats["battles"] > 0:
                self.assertTrue(any(b["verdict"] == "bad" for b in agent.memory.battles), f"seed {seed}")
                self.assertGreater(agent.stats["lessons"], 0, f"seed {seed}")  # it became more careful (and relaxes again after wins)
                lessons += 1
            self.assertEqual(agent.stats["errors"], 0, f"seed {seed}")
        self.assertGreater(lessons, 0, "the simulation never produced a defeat that the agent read")

    def test_peaceful_personas_never_attack(self):
        for name in ("scientist", "turtle", "miner"):
            _, agent = self.play(name, 4)
            self.assertEqual(agent.stats["attacks"], 0, name)

    def test_turtle_defends_raider_attacks(self):
        turtle_game, turtle = self.play("turtle", 6)
        raider_game, raider = self.play("raider", 6)
        self.assertGreater(turtle_game.defense["rocket_launcher"], 0)
        self.assertGreater(raider_game.ships["light_fighter"], turtle_game.ships["light_fighter"])

    def test_agents_never_touch_players_who_are_not_agents(self):
        for name in ("raider", "balanced"):
            game, agent = self.play(name, 2, victim_is_agent=False)
            self.assertEqual(agent.stats["spies"], 0, name)
            self.assertEqual(agent.stats["attacks"], 0, name)
            self.assertEqual(game.attacks_resolved, 0, name)

    def test_attack_humans_switch(self):
        game = FakeGame()
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", PRESETS["raider"], "x", store, random.Random(1), client=game, attack_humans=True)
        agent.log = lambda *a, **k: None
        elapsed = 0.0
        while elapsed < 60 * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        self.assertGreater(agent.stats["spies"], 0)


if __name__ == "__main__":
    unittest.main()


class MemorySaveTests(unittest.TestCase):
    def test_memory_survives_a_round_trip_through_json(self):
        import json
        m = Memory()
        m.spied[(1, 2, 3)] = 100.0
        m.intel[(1, 2, 3)] = {"at": 100.0, "loot": 5000, "defenders": 4, "owner_id": 7, "skip_until": 900.0}
        m.avoid_until[(1, 4, 9)] = 777.0
        m.risk_scale = 0.36
        m.need_cargo = True
        m.battles.append({"profit": -5})
        back = Memory.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back, m)

    def test_damaged_or_odd_data_never_breaks_loading(self):
        m = Memory.from_dict({"spied": {"nonsense": 1, "1:2:3": 5.0}, "risk_scale": "abc", "battles": "x", "intel": None})
        self.assertEqual(m.spied, {(1, 2, 3): 5.0})
        self.assertEqual(m.risk_scale, 1.0)
        self.assertEqual(m.battles, [])
        self.assertEqual(Memory.from_dict({"risk_scale": 99}).risk_scale, 1.5)

    def test_agent_remembers_across_a_restart_and_survives_a_broken_file(self):
        tmp = tempfile.mkdtemp()
        store = TokenStore(str(Path(tmp) / "t.json"))
        a = Agent("sim", PRESETS["raider"], "x", store, random.Random(1), client=FakeGame(), memory_dir=tmp)
        a.memory.risk_scale = 0.4
        a.memory.avoid_until[(1, 2, 3)] = 5.0
        a.save_memory()
        b = Agent("sim", PRESETS["raider"], "x", store, random.Random(1), client=FakeGame(), memory_dir=tmp)
        self.assertEqual(b.memory.risk_scale, 0.4)
        self.assertEqual(b.memory.avoid_until, {(1, 2, 3): 5.0})
        (Path(tmp) / "sim.json").write_text("{broken")
        c = Agent("sim", PRESETS["raider"], "x", store, random.Random(1), client=FakeGame(), memory_dir=tmp)
        self.assertEqual(c.memory, Memory())
