import json
import random
import unittest
from dataclasses import replace

from swarm import pricing as P
from swarm import social as S
from swarm.military import Memory
from swarm.personas import PRESETS
from tests.test_social import TRADER, TradeTests, World

STATS = {"small_cargo": {"cargo_capacity": 5000}, "large_cargo": {"cargo_capacity": 25000}}
UNITS_OK = [{"machine_name": "small_cargo", "requirements_met": True, "max_buildable": 8, "cost_per_unit": {"metal": 500, "crystal": 500},
             "amount": 0}]


class AskingTests(unittest.TestCase):
    def test_unanswered_offers_get_cheaper_down_to_the_floor(self):
        pr = {}
        old, new = P.on_unanswered(pr, "metal", "crystal")
        self.assertAlmostEqual((old, new), (1.03, 1.03 * 0.92))
        for _ in range(40):
            P.on_unanswered(pr, "metal", "crystal")
        self.assertEqual(pr["metal>crystal"]["r"], P.MIN_RATIO)
        self.assertEqual(pr["metal>crystal"]["unanswered"], 41)
        self.assertEqual(pr["crystal>metal"]["r"] if "crystal>metal" in pr else 1.03, 1.03)    # pairs are independent

    def test_answered_and_completed_offers_raise_the_price_up_to_the_cap(self):
        pr = {}
        old, new = P.on_answered(pr, "metal", "crystal", 1.03)
        self.assertAlmostEqual(new, 1.03 * 1.03)
        self.assertEqual(pr["metal>crystal"]["best"], 1.03)
        for _ in range(40):
            P.on_done(pr, "metal", "crystal")
        self.assertEqual(pr["metal>crystal"]["r"], P.MAX_RATIO)
        self.assertEqual(pr["metal>crystal"]["done"], 40)

    def test_after_a_failure_above_the_best_known_price_it_falls_back_to_it(self):
        pr = {}
        P.on_answered(pr, "metal", "crystal", 0.95)             # sold at 0.95; r is now 1.03 * ... (max of old and posted) * 1.03
        e = pr["metal>crystal"]
        e["r"] = 1.20
        P.on_unanswered(pr, "metal", "crystal")
        self.assertAlmostEqual(e["r"], max(0.95, 1.20 * 0.92))
        e["r"] = 0.9
        P.on_unanswered(pr, "metal", "crystal")
        self.assertAlmostEqual(e["r"], 0.9 * 0.92)                     # at or below best: the normal 8% step

    def test_exploration_is_small_bounded_and_uses_only_the_given_stream(self):
        pr = {"metal>crystal": dict(P.entry({}, "metal", "crystal"), r=P.MAX_RATIO)}
        rng = random.Random(5)
        seen = {P.ask_ratio(pr, "metal", "crystal", rng) for _ in range(200)}
        self.assertTrue(all(P.MAX_RATIO * 0.97 - 1e-9 <= r <= P.MAX_RATIO + 1e-9 for r in seen))
        self.assertGreater(len(seen), 5)
        self.assertEqual(P.ask_ratio(pr, "metal", "crystal", None), P.MAX_RATIO)       # no stream, no nudge
        other = random.Random(9)
        before = other.getstate()
        P.ask_ratio(pr, "metal", "crystal", rng)
        self.assertEqual(other.getstate(), before)

    def test_make_offer_uses_the_learned_markup(self):
        stock = {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}
        self.assertEqual(S.make_offer(stock, (1, 1, 1), 1)["data"]["want"], {"crystal": int(20_000 * 1.03 / 1.5)})
        pr = {"metal>crystal": dict(P.entry({}, "metal", "crystal"), r=0.8)}
        o = S.make_offer(stock, (1, 1, 1), 1, pr)
        self.assertEqual(o["data"]["want"], {"crystal": int(20_000 * 0.8 / 1.5)})
        self.assertEqual((o["pair"], o["ratio"]), (["metal", "crystal"], 0.8))


class AcceptingTests(unittest.TestCase):
    def post(self, give_amt, want_amt):
        return {"id": 7, "agent": "p", "t": 100.0, "kind": "OFFER", "data": {"give": {"crystal": give_amt}, "want": {"metal": want_amt},
                                                                              "at": "1:5:2", "player_id": 2}}

    def test_pick_match_uses_the_agents_own_acceptable_markup_and_it_loosens_with_need(self):
        stock = {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}
        dear = self.post(10_000, 20_000)                       # asks 20,000 metal for 15,000 worth of crystal: markup 1.333
        slightly = self.post(13_000, 20_000)                   # markup 1.026: within the old 5% tolerance
        pr = {}
        self.assertIsNone(S.pick_match(stock, [dear], "me", 100.0, {}, 100.0, set(), [], pr))
        self.assertIsNotNone(S.pick_match(stock, [slightly], "me", 100.0, {}, 100.0, set(), [], pr))
        pr = {"crystal>metal": dict(P.entry({}, "crystal", "metal"), accept=1.25)}
        slightly = self.post(11_000, 20_000)                    # markup ~1.21
        self.assertIsNotNone(S.pick_match(stock, [slightly], "me", 100.0, {}, 100.0, set(), [], pr))
        self.assertIsNone(S.pick_match(stock, [slightly], "me", 100.0, {}, 100.0, set(), [], {}))

    def test_many_unanswered_hours_of_need_make_the_agent_less_picky_and_a_deal_makes_it_stricter_again(self):
        pr = {}
        self.assertIsNone(P.on_need_unmet(pr, "crystal", "metal", 0.0 + 1))
        self.assertIsNone(P.on_need_unmet(pr, "crystal", "metal", 1800.0))
        old, new = P.on_need_unmet(pr, "crystal", "metal", 3700.0)
        self.assertAlmostEqual(new - old, P.ACCEPT_STEP)
        for h in range(2, 40):
            P.on_need_unmet(pr, "crystal", "metal", 3700.0 + h * 3600)
        self.assertEqual(P.accept_ratio(pr, "crystal", "metal"), P.ACCEPT_MAX)
        P.on_need_met(pr, "crystal", "metal")
        self.assertEqual(pr["crystal>metal"]["need_since"], 0.0)
        old, new = P.on_accept_done(pr, "crystal", "metal")
        self.assertAlmostEqual(old - new, P.ACCEPT_EASE)


class StateTests(unittest.TestCase):
    def test_prices_are_saved_and_old_files_still_load(self):
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).social["prices"], {})
        m = Memory()
        P.on_unanswered(m.social["prices"], "metal", "crystal")
        back = Memory.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.social["prices"], m.social["prices"])
        for junk in (None, "x", {"a": 1}, {"metal>crystal": {"r": "nan", "best": "x", "answered": "?"}}, {"metal>crystal": {"r": 99}}):
            st = S.clean_state({"prices": junk})
            for e in st["prices"].values():
                self.assertTrue(P.MIN_RATIO <= e["r"] <= P.MAX_RATIO)


class DeliveryTests(unittest.TestCase):
    def test_enough_ships_at_home(self):
        self.assertTrue(P.deliverable(PRESETS["raider"], [], {}, {"small_cargo": 2}, STATS, 20_000))      # 10,000 >= half
        self.assertFalse(P.deliverable(PRESETS["raider"], [], {}, {"small_cargo": 1}, STATS, 20_000))
        self.assertTrue(P.deliverable(PRESETS["raider"], [], {}, {"large_cargo": 1}, STATS, 40_000))

    def test_no_ships_and_not_a_builder_cannot_deliver(self):
        stock = {"metal": 500_000, "crystal": 500_000, "deuterium": 100_000}
        self.assertFalse(P.deliverable(PRESETS["raider"], UNITS_OK, stock, {}, STATS, 20_000))
        self.assertFalse(P.deliverable(PRESETS["balanced"], UNITS_OK, stock, {}, STATS, 20_000))

    def test_a_builder_with_stock_can_but_not_when_it_could_not_pay(self):
        rich = {"metal": 500_000, "crystal": 500_000, "deuterium": 100_000}
        poor = {"metal": 20_000, "crystal": 1_000, "deuterium": 5_000}
        self.assertTrue(P.deliverable(PRESETS["trader"], UNITS_OK, rich, {}, STATS, 20_000))
        self.assertTrue(P.deliverable(PRESETS["miner"], UNITS_OK, rich, {}, STATS, 20_000))
        self.assertFalse(P.deliverable(PRESETS["trader"], UNITS_OK, poor, {}, STATS, 40_000))     # 10% of a poor stock buys 2 cargos: 10,000 < half
        self.assertFalse(P.deliverable(PRESETS["trader"], [], rich, {}, STATS, 20_000))             # shipyard missing: nothing buildable
        self.assertFalse(P.deliverable(PRESETS["trader"], UNITS_OK, rich, {}, STATS, 500_000))      # 5 small cargos are far too few


class LearningInTheGame(TradeTests):
    def test_an_offer_nobody_takes_gets_cheaper_and_is_logged(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK, ships={"small_cargo": 10})
        a = self.agent("agent0001", 1, ga, TRADER)
        self.play([(a, ga)], 70)
        learned = [e for e in self.events(a, "price_learned") if e["kind"] == "ask"]
        self.assertTrue(learned)
        first = learned[0]
        self.assertEqual((first["pair"], first["kind"], first["why"]), ("metal>crystal", "ask", "unanswered"))
        self.assertAlmostEqual(first["new"], first["old"] * 0.92, places=3)
        self.assertEqual(first["start"], 1.03)
        posted = [e["ratio"] for e in self.events(a, "trade_offer") if e["action"] == "posted"]
        self.assertGreater(posted[0], posted[-1])                         # each repost asks less
        self.assertEqual(a.memory.social["prices"]["metal>crystal"]["unanswered"], len(learned))
        self.assertNotEqual(a._price_rng().getstate(), a.rng.getstate())    # its own stream, not the agent's dice

    def test_a_poster_that_is_answered_and_paid_learns_to_ask_more(self):
        a, ga, b, gb = self.pair()
        done = lambda: any(e["action"] == "done" for e in self.events(b, "trade_offer"))
        self.play([(a, ga), (b, gb)], 120, until=done)
        self.assertTrue(done())
        why = [e["why"] for e in self.events(b, "price_learned")]
        self.assertIn("answered", why)
        self.assertIn("completed", why)
        self.assertGreater(b.memory.social["prices"]["crystal>metal"]["r"], 1.0)
        self.assertEqual(b.memory.social["prices"]["crystal>metal"]["done"], 1)

    def test_no_cargo_ships_and_no_right_to_build_them_means_no_offer(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK)
        ga.levels["shipyard"] = 1
        a = self.agent("agent0001", 1, ga, replace(TRADER, name="balanced"))        # not in logistics.BUILDERS
        self.play([(a, ga)], 20)
        self.assertEqual(self.board.read(agent="agent0001"), [])
        skipped = [e for e in self.events(a, "trade_offer") if e["action"] == "skipped"]
        self.assertTrue(skipped)
        self.assertIn("cargo", skipped[0]["why"])
        self.assertLessEqual(len(skipped), 6)                                       # looks again every ten minutes, not every round

    def test_a_builder_with_a_shipyard_still_posts(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK)
        ga.levels["shipyard"] = 1
        a = self.agent("agent0001", 1, ga, TRADER)
        self.play([(a, ga)], 5)
        self.assertEqual(len(self.board.read(agent="agent0001")), 1)

    def test_an_answerer_without_cargo_does_not_answer(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK)
        gb = self.game((1, 5, 2), 2, self.B_STOCK)
        self.introduce((ga, "Alpha", 1), (gb, "Beta", 2))
        a = self.agent("agent0001", 1, ga, replace(TRADER, name="balanced"))
        self.store.put("agent0002", {"email": "b@x", "password": "p", "token": "t", "persona": "trader", "user_id": 2})
        self.board.post("agent0002", "OFFER", "o", {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 2})
        self.play([(a, ga)], 10)
        self.assertEqual([p for p in self.board.read(agent="agent0001") if p["data"].get("reply_to")], [])
        self.assertEqual(a.memory.social["trades"], {})


if __name__ == "__main__":
    unittest.main()
