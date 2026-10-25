import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm.agent import Agent, TokenStore
from swarm.brain import Choice
from swarm.personas import PRESETS
from swarm.trade import CALL_COST, choose_sale, plan_deal
from tests.fakegame import FakeGame

IMPATIENT = replace(PRESETS["raider"], patience=0.4)
PATIENT = replace(PRESETS["miner"], patience=0.9)
STORAGE = {"metal": 500_000, "crystal": 500_000, "deuterium": 500_000}
GOAL = {"metal": 1000, "crystal": 50_000, "deuterium": 0}       # short of 50,000 crystal
HAVE = {"metal": 200_000, "crystal": 0, "deuterium": 0}         # lots of spare metal


def sale(persona=IMPATIENT, goal=GOAL, have=HAVE, wait=5000.0, dm=8000, now=1000.0, cooldown=0.0, storage=STORAGE):
    return choose_sale(persona, goal, have, storage, wait, dm, now, cooldown)


def merchant(sell="metal", quality=0.9):
    base = {"metal": 3.0, "crystal": 2.0, "deuterium": 1.0}
    return {"type": sell, "receive": {r: {"rate": base[r] * quality, "per_unit_given": base[r] * quality / base[sell]}
                                      for r in base if r != sell}}


class ChooseSaleTests(unittest.TestCase):
    def test_sells_the_spare_resource_when_stuck_with_a_big_shortfall(self):
        self.assertEqual(sale(), "metal")

    def test_needs_dark_matter_and_patient_agents_keep_one_call_in_reserve(self):
        self.assertIsNone(sale(dm=CALL_COST - 1))
        self.assertEqual(sale(persona=IMPATIENT, dm=CALL_COST), "metal")
        self.assertIsNone(sale(persona=PATIENT, dm=CALL_COST))
        self.assertEqual(sale(persona=PATIENT, dm=2 * CALL_COST), "metal")

    def test_not_during_the_cooldown_or_for_a_short_wait_or_small_change(self):
        self.assertIsNone(sale(now=1000.0, cooldown=2000.0))
        self.assertIsNone(sale(wait=300.0))
        self.assertIsNone(sale(goal={"metal": 1000, "crystal": 500, "deuterium": 0}))   # tiny shortfall

    def test_unknown_wait_counts_as_stuck(self):
        self.assertEqual(sale(wait=None), "metal")

    def test_needs_a_surplus_big_enough_to_cover_everything(self):
        self.assertIsNone(sale(have={"metal": 30_000, "crystal": 0, "deuterium": 0}))
        self.assertIsNone(sale(have={"metal": 1000, "crystal": 0, "deuterium": 0}))   # nothing spare

    def test_never_sells_what_the_goal_needs_or_what_it_is_short_of(self):
        goal = {"metal": 150_000, "crystal": 50_000, "deuterium": 0}
        self.assertIsNone(sale(goal=goal))   # only 50,000 metal is spare, not enough for the shortfall

    def test_no_goal_no_trade(self):
        self.assertIsNone(sale(goal=None))

    def test_goal_bigger_than_the_store_is_hopeless(self):
        self.assertIsNone(sale(storage={"metal": 500_000, "crystal": 10_000, "deuterium": 500_000}))


class PlanDealTests(unittest.TestCase):
    def test_asks_for_the_whole_shortfall_and_budgets_enough_to_pay_for_it(self):
        deal = plan_deal(merchant("metal", 0.9), GOAL, HAVE, STORAGE)
        self.assertGreaterEqual(deal.receive["crystal"], 50_000)
        per_unit = 2.0 * 0.9 / 3.0
        self.assertGreaterEqual(deal.give_amount, deal.receive["crystal"] / per_unit)
        self.assertLessEqual(deal.give_amount, HAVE["metal"] - GOAL["metal"])
        self.assertEqual(deal.sell, "metal")

    def test_refuses_when_the_rates_make_it_too_expensive(self):
        poor = {"metal": 60_000, "crystal": 0, "deuterium": 0}   # barely enough at a fair rate, not at 70%
        self.assertIsNone(plan_deal(merchant("metal", 0.5), GOAL, poor, STORAGE))

    def test_cannot_buy_the_resource_the_merchant_buys(self):
        self.assertIsNone(plan_deal(merchant("crystal"), GOAL, HAVE, STORAGE))

    def test_refuses_when_it_will_not_fit_into_the_store(self):
        self.assertIsNone(plan_deal(merchant("metal"), GOAL, HAVE, {**STORAGE, "crystal": 40_000}))


class AgentTradeTests(unittest.TestCase):
    def setUp(self):
        self.game = FakeGame()
        self.game.res = {"metal": 200_000.0, "crystal": 0.0, "deuterium": 0.0}
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        self.agent = Agent("sim", IMPATIENT, "x", store, random.Random(1), client=self.game)
        self.logs = []
        self.agent.log = lambda event, **f: self.logs.append((event, f))
        self.agent.dark_matter, self.agent.now = 8000, 1000.0
        self.planet = {"id": 10, "storage": dict(STORAGE)}
        self.choice = Choice(None, 5000.0, "saving up for research_lab", dict(GOAL))
        self.have = {k: int(v) for k, v in self.game.res.items()}

    def test_trades_once_updates_what_it_has_and_then_waits_out_the_cooldown(self):
        self.assertTrue(self.agent._maybe_trade(self.planet, self.choice, self.have))
        self.assertGreaterEqual(self.have["crystal"], 50_000)
        self.assertEqual(self.agent.dark_matter, 8000 - CALL_COST)
        self.assertEqual(self.agent.stats["trades"], 1)
        self.assertEqual(self.logs[-1][0], "trade")
        self.assertGreater(self.agent.memory.trade_cooldown_until, 1000.0)
        # Even with dark matter left, no second call during the cooldown.
        self.agent.dark_matter = 8000
        self.assertFalse(self.agent._maybe_trade(self.planet, self.choice, dict(HAVE)))
        self.assertEqual(self.game.trades_done, 1)

    def test_dismisses_the_merchant_when_the_rates_are_too_poor(self):
        self.game.merchant_quality = 0.3
        self.assertFalse(self.agent._maybe_trade(self.planet, self.choice, self.have))
        self.assertIsNone(self.game.merchant)
        self.assertEqual(self.logs[-1][0], "merchant_dismissed")
        self.assertEqual(self.game.trades_done, 0)

    def test_uses_a_merchant_that_is_already_waiting(self):
        self.game.merchant = merchant("metal", 0.9)
        self.assertTrue(self.agent._maybe_trade(self.planet, self.choice, self.have))
        self.assertEqual(self.game.dark_matter, 8000)   # no new call was paid

    def test_a_failed_call_never_crashes_the_agent(self):
        self.game.dark_matter = 100   # the agent believes it has 8000
        self.assertFalse(self.agent._maybe_trade(self.planet, self.choice, self.have))
        self.assertEqual(self.logs[-1][0], "trade_failed")


if __name__ == "__main__":
    unittest.main()
