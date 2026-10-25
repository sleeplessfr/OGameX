import unittest

from swarm.brain import payback_factors, payback_hours


def cost(base, factor, level):
    return {"metal": int(base[0] * factor ** level), "crystal": int(base[1] * factor ** level), "deuterium": 0}


def planet_at(level):
    return {"production_per_hour": {"metal": int(30 + 30 * level * 1.1 ** level), "crystal": int(15 + 20 * level * 1.1 ** level),
                                    "deuterium": int(10 * level * 1.1 ** level)}}


def mines(level):
    return [{"machine_name": "metal_mine", "level": level, "cost": cost((60, 15), 1.5, level)},
            {"machine_name": "crystal_mine", "level": level, "cost": cost((48, 24), 1.6, level)},
            {"machine_name": "deuterium_synthesizer", "level": level, "cost": cost((225, 75), 1.5, level)}]


class PaybackTests(unittest.TestCase):
    def test_deeper_levels_take_longer_to_pay_back(self):
        early, late = (payback_hours(mines(lv)[0], planet_at(lv)) for lv in (3, 15))
        self.assertLess(early, late)

    def test_metal_mine_level_10_pays_back_in_about_two_days_at_normal_speed(self):
        hours = payback_hours(mines(10)[0], planet_at(10), {"level": 10, "cost": cost((75, 30), 1.5, 10)})
        self.assertTrue(30 < hours < 80, hours)

    def test_the_universe_speed_does_not_change_the_answer(self):
        slow, fast = planet_at(8), planet_at(8)
        fast["production_per_hour"] = {r: v * 100 for r, v in fast["production_per_hour"].items()}
        self.assertAlmostEqual(payback_hours(mines(8)[0], slow), payback_hours(mines(8)[0], fast) * 100, delta=1.0)

    def test_extra_power_makes_a_mine_slower_to_pay_back(self):
        solar = {"level": 8, "cost": cost((75, 30), 1.5, 8)}
        without = payback_hours(mines(8)[2], planet_at(8))
        with_power = payback_hours(mines(8)[2], planet_at(8), solar)
        self.assertGreater(with_power, without)

    def test_not_a_mine_or_no_production_means_no_estimate(self):
        self.assertIsNone(payback_hours({"machine_name": "shipyard", "level": 2, "cost": {"metal": 400}}, planet_at(5)))
        self.assertIsNone(payback_hours(mines(0)[2], planet_at(0)))  # no deuterium output yet

    def test_the_fastest_payback_is_favoured_and_factors_stay_bounded(self):
        factors = payback_factors(mines(10), planet_at(10), None, 0.8)
        self.assertGreater(factors["metal_mine"], factors["crystal_mine"])
        self.assertGreater(factors["crystal_mine"], factors["deuterium_synthesizer"])
        for f in factors.values():
            self.assertTrue(0.5 <= f <= 2.0)

    def test_a_less_patient_agent_cares_less(self):
        keen = payback_factors(mines(10), planet_at(10), None, 0.9)["metal_mine"]
        casual = payback_factors(mines(10), planet_at(10), None, 0.3)["metal_mine"]
        self.assertGreater(keen, casual)
        self.assertGreater(casual, 1.0)

    def test_too_few_candidates_means_no_opinion(self):
        self.assertEqual(payback_factors(mines(5)[:1], planet_at(5), None, 0.8), {})


if __name__ == "__main__":
    unittest.main()
