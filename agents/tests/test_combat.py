import random
import unittest
from dataclasses import replace

from swarm.combat import STATS, estimate_battle, tech_from_research
from swarm.military import Intel, judge_fight, required_win_chance, summarize_report
from swarm.personas import PRESETS

RAIDER = PRESETS["raider"]


class CombatEstimateTests(unittest.TestCase):
    def test_matches_the_first_real_battle_on_the_owners_server(self):
        # 13 light fighters (armour 30%) attacked 14 rocket launchers (armour 50%): all fighters died (52,000 lost),
        # 7 launchers died (14,000), nobody won.
        e = estimate_battle({"light_fighter": 13}, {"rocket_launcher": 14}, random.Random(1), (0, 0, 3), (0, 0, 5), runs=40)
        self.assertEqual(e.win_chance, 0)
        self.assertAlmostEqual(e.my_loss_value, 52000, delta=6000)
        # the real report listed 14,000 destroyed; about 70% of destroyed defenses are rebuilt, so about 4,200 stays lost
        self.assertAlmostEqual(e.their_loss_value, 4200, delta=2500)
        self.assertLess(e.my_survival, 0.1)

    def test_a_big_fleet_beats_the_same_defence_cheaply(self):
        e = estimate_battle({"light_fighter": 40}, {"rocket_launcher": 14}, random.Random(1), runs=30)
        self.assertEqual(e.win_chance, 1.0)
        self.assertLess(e.my_loss_value, 14000)

    def test_no_defenders_is_a_free_win_and_nothing_to_shoot_with_is_not(self):
        self.assertEqual(estimate_battle({"light_fighter": 3}, {}, random.Random(1)).win_chance, 1.0)
        self.assertEqual(estimate_battle({}, {"rocket_launcher": 3}, random.Random(1)).win_chance, 0.0)

    def test_stats_come_from_the_game_source(self):
        self.assertEqual(STATS["light_fighter"][:3], (3000, 1000, 0))
        self.assertEqual(STATS["rocket_launcher"][3:6], (2000, 20, 80))

    def test_tech_levels(self):
        self.assertEqual(tech_from_research({"weapon_technology": 2, "shielding_technology": 1, "armor_technology": 5}), (2, 1, 5))
        self.assertEqual(tech_from_research({"armor_technology": {"level": 4}}), (0, 0, 4))
        self.assertIsNone(tech_from_research(None))

    def test_huge_fleets_stay_quick(self):
        e = estimate_battle({"cruiser": 5000}, {"battle_ship": 3000}, random.Random(1), runs=3)
        self.assertTrue(0.0 <= e.win_chance <= 1.0)


class FightJudgementTests(unittest.TestCase):
    TURTLE_INTEL = Intel(loot=80_000, defenders=14, owner_id=3, ships={}, defense={"rocket_launcher": 14})

    def test_the_raider_would_have_skipped_the_fight_that_cost_it_52000(self):
        go, why, _ = judge_fight(RAIDER, random.Random(1), {"light_fighter": 13}, self.TURTLE_INTEL, (0, 0, 3), 10_000)
        self.assertFalse(go)
        self.assertIn("win chance", why)

    def test_with_enough_fighters_it_goes(self):
        go, why, est = judge_fight(RAIDER, random.Random(1), {"light_fighter": 40}, self.TURTLE_INTEL, None, 30_000)
        self.assertTrue(go, why)
        self.assertGreater(est.win_chance, 0.9)

    def test_unknown_defenders_are_simulated_as_a_typical_garrison(self):
        go, why, est = judge_fight(RAIDER, random.Random(1), {"light_fighter": 5}, Intel(5000, None, 3), None, 1000)
        self.assertFalse(go); self.assertIn("assumed 40 rocket_launchers".replace("launchers", "launchers"), why.replace("rocket launchers", "rocket_launchers"))

    def test_careful_personalities_and_burnt_raiders_want_more_certainty(self):
        self.assertLess(required_win_chance(RAIDER), required_win_chance(PRESETS["miner"]))
        self.assertLess(required_win_chance(RAIDER, 1.0), required_win_chance(RAIDER, 0.4))
        self.assertLessEqual(required_win_chance(replace(RAIDER, risk_ratio=0.0)), 0.95)

    def test_report_keeps_the_composition(self):
        report = {"resources": {"metal": 1000, "crystal": 0, "deuterium": 0}, "ships": {"light_fighter": 2},
                  "defense": {"rocket_launcher": 3}, "research": {"armor_technology": 4}, "target": {"owner_id": 9}}
        intel = summarize_report(report)
        self.assertTrue(intel.composition_known)
        self.assertEqual(intel.all_defenders(), {"light_fighter": 2, "rocket_launcher": 3})
        self.assertEqual(intel.defenders, 5)
        self.assertFalse(summarize_report({"resources": {}, "target": {}}).composition_known)


if __name__ == "__main__":
    unittest.main()


class StrikeChoiceTests(unittest.TestCase):
    STATS = {"light_fighter": {"cargo_capacity": 50}, "small_cargo": {"cargo_capacity": 5000}}

    def test_does_not_send_a_huge_fleet_for_a_small_loot(self):
        # the 11:14 disaster: 475 fighters (1.9M) sent for a loot of 98k against a defence it believed weak
        from swarm.military import choose_strike
        intel = Intel(98_532, 155, 3, ships={}, defense={"rocket_launcher": 155})
        strike, go, why, kind = choose_strike(RAIDER, random.Random(1), {"light_fighter": 530, "small_cargo": 40}, self.STATS, intel, (0, 0, 0))
        if go:
            self.assertLessEqual(strike["light_fighter"] * 4000, 4 * 98_532)
            self.assertLess(strike["light_fighter"], 100)
        self.assertEqual(kind, "fight")

    def test_sends_the_smallest_fleet_that_is_sure_to_win(self):
        from swarm.military import choose_strike
        intel = Intel(40_000, 4, 3, ships={}, defense={"rocket_launcher": 4})
        strike, go, why, kind = choose_strike(RAIDER, random.Random(1), {"light_fighter": 100, "small_cargo": 20}, self.STATS, intel, (0, 0, 0))
        self.assertTrue(go, why)
        self.assertLess(strike["light_fighter"], 60)

    def test_wants_cargo_when_nothing_can_carry_the_loot(self):
        from swarm.military import choose_strike
        intel = Intel(500_000, 1, 3, ships={}, defense={"rocket_launcher": 1})
        _, go, _, kind = choose_strike(RAIDER, random.Random(1), {"light_fighter": 10}, self.STATS, intel, (0, 0, 0))
        self.assertFalse(go)
        self.assertEqual(kind, "cargo")

    def test_assumes_the_defender_kept_building(self):
        # exactly as many launchers as the report showed would be a coin flip for 60 fighters: with the margin it is not sent
        intel = Intel(20_000, 40, 3, ships={}, defense={"rocket_launcher": 40})
        go, why, _ = judge_fight(RAIDER, random.Random(1), {"light_fighter": 45}, intel, (0, 0, 0), 20_000)
        self.assertFalse(go, why)
