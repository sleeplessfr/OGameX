import unittest

import expand_planner as ep


class ExpandPlannerTests(unittest.TestCase):
    def test_terraformer_fields_follow_the_game(self):
        self.assertEqual([ep.fields_from_terraformer(n) for n in (0, 1, 2, 3, 4, 10)], [0, 5, 11, 16, 22, 55])

    def test_one_colony_needs_astrophysics_one_and_two_need_three(self):
        self.assertEqual(ep.astro_needed(1), 1)   # the game's round(1 / 2) is 1
        self.assertEqual(ep.astro_needed(2), 3)
        self.assertEqual(ep.max_colonies(0), 0)
        self.assertEqual(ep.max_colonies(4), 2)

    def test_prices_grow_by_the_factor_per_level(self):
        self.assertEqual(ep.level_cost("terraformer", 1), (0, 50_000, 100_000))
        self.assertEqual(ep.level_cost("terraformer", 3), (0, 200_000, 400_000))
        self.assertEqual(ep.climb("energy_technology", 11, 12), ep.level_cost("energy_technology", 12))
        self.assertEqual(ep.climb("energy_technology", 12, 12), (0, 0, 0))

    def test_a_colony_costs_far_less_per_field_than_a_terraformer(self):
        colony = ep.value(ep.add(ep.COLONY_SHIP, ep.level_cost("astrophysics", 1))) / 200
        terra = ep.value(ep.level_cost("terraformer", 1)) / 5
        self.assertLess(colony * 10, terra)


if __name__ == "__main__":
    unittest.main()


class PositionTests(unittest.TestCase):
    def test_position_8_beats_the_hot_edge_and_cold_positions_make_more_deuterium(self):
        self.assertGreater(ep.position_income(8), ep.position_income(1))
        self.assertGreater(ep.position_income(8), ep.position_income(4))
        d = lambda pos: 1.44 - 0.004 * sum(ep.TEMPERATURE[pos]) / 2
        self.assertGreater(d(15), d(8))
        self.assertGreater(d(8), d(1))
