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

    def test_unknown_defenders_means_nothing_to_simulate(self):
        go, why, est = judge_fight(RAIDER, random.Random(1), {"light_fighter": 5}, Intel(5000, None, 3), None, 1000)
        self.assertEqual((go, why, est), (True, "", None))

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
