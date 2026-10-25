import dataclasses
import json
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

import board as B
from swarm import social as S
from swarm.agent import Agent, TokenStore
from swarm.boardclient import BoardClient, NullBoard
from swarm.military import Memory
from swarm.personas import PRESETS, ROTATION, make_persona
from tests.fakegame import FakeGame

AGENTS_DIR = Path(__file__).resolve().parent.parent
TRADER = replace(PRESETS["trader"], trade_interest=1.0, scout_interest=0.0, probes_wanted=0, expansion=0.0, expedition_interest=0.0,
                 caution=0.0)
LISTENER = replace(PRESETS["turtle"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, expansion=0.0, caution=0.9)
WATCHER = replace(PRESETS["miner"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=1, expansion=0.0,
                  expedition_interest=0.0, caution=0.6)


# ---- pure rules -----------------------------------------------------------------------------------------------------
class StateTests(unittest.TestCase):
    def test_clean_state_is_tolerant(self):
        for raw in (None, "x", [], {"cursor": "a", "trades": [1], "rep": {"a": 1}, "posted": ["x", 3]}):
            st = S.clean_state(raw)
            self.assertEqual(set(st), set(S.new_state()))
        st = S.clean_state({"cursor": 5, "trades": {"a": {"x": 1}}, "posted": [1, "x", 2.5]})
        self.assertEqual((st["cursor"], st["posted"], st["trades"]), (5, [1, 2.5], {"a": {"x": 1}}))

    def test_old_memory_files_have_no_social_part_and_new_ones_round_trip(self):
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).social, S.new_state())
        m = Memory()
        m.social["cursor"] = 77
        m.social["rep"]["a2"] = {"ok": 1}
        back = Memory.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual((back.social["cursor"], back.social["rep"]), (77, {"a2": {"ok": 1}}))

    def test_post_budget_per_hour(self):
        st = S.new_state()
        for i in range(S.POSTS_PER_HOUR):
            self.assertTrue(S.can_post(st, 100.0 + i))
            S.note_post(st, 100.0 + i)
        self.assertFalse(S.can_post(st, 500.0))
        self.assertTrue(S.can_post(st, 100.0 + 3601))


class PersonaTests(unittest.TestCase):
    def test_trader_exists_but_is_not_in_the_default_rotation(self):
        self.assertIn("trader", PRESETS)
        self.assertNotIn("trader", ROTATION)
        self.assertEqual([make_persona(i, random.Random(i)).name for i in range(7)],
                         ["miner", "scientist", "turtle", "raider", "balanced", "miner", "scientist"])
        self.assertGreater(PRESETS["trader"].trade_interest, 0)
        self.assertTrue(all(PRESETS[n].trade_interest == 0 for n in ROTATION))

    def test_existing_agents_keep_their_random_draws(self):
        """Golden values taken from the code before the trader existed: the stream after make_persona must be the same."""
        golden = [0.9011520429873923, 0.4229670321705845, 0.3329814924114096, 0.4747024910574612, 0.9233980854758448]
        for i, want in enumerate(golden):
            rng = random.Random(100 + i)
            p = make_persona(i, rng)
            rng.random()          # (the golden values below are the second number after make_persona)
            self.assertEqual(rng.random(), want, p.name)
            self.assertEqual(p.trade_interest, 0.0)

    def test_the_trader_draws_its_extra_value_last(self):
        rng = random.Random(3)
        p = make_persona(0, rng, ["trader"])
        self.assertTrue(0.2 <= p.trade_interest <= 1.0)
        self.assertIn(p.character_class, ("collector",))


class RobberyTests(unittest.TestCase):
    B = {"role": "defender", "loot": 8000, "my_loss": 300, "their_id": 7}

    def test_robbed_agent_posts_warning_and_revenge_with_raider_id_and_coordinates(self):
        st = S.new_state()
        posts = S.robbery_posts(self.B, {(1, 1, 5): 900.0, (1, 2, 2): 100.0}, {(1, 1, 5): {"strength": 5000}}, 1000.0, (1, 3, 8), 4, st)
        self.assertEqual([p[0] for p in posts], ["WARNING", "REVENGE"])
        d = posts[0][2]
        self.assertEqual((d["raider_at"], d["raider_id"], d["victim_at"], d["victim_id"], d["loot"], d["strength"]),
                         ("1:1:5", 7, "1:3:8", 4, 8000, 5000))
        self.assertTrue(all(len(p[1]) <= 200 for p in posts))

    def test_unknown_raider_position_still_names_the_player(self):
        posts = S.robbery_posts(self.B, {}, {}, 1000.0, (1, 3, 8), 4, S.new_state())
        self.assertNotIn("raider_at", posts[0][2])
        self.assertEqual(posts[0][2]["raider_id"], 7)

    def test_a_fleet_seen_long_ago_is_not_blamed(self):
        posts = S.robbery_posts(self.B, {(1, 1, 5): 0.0}, {}, 10 * 3600.0, (1, 3, 8), 4, S.new_state())
        self.assertNotIn("raider_at", posts[0][2])

    def test_small_raids_attackers_and_repeats_are_silent(self):
        st = S.new_state()
        self.assertEqual(S.robbery_posts({**self.B, "loot": 10, "my_loss": 5}, {}, {}, 5.0, (1, 3, 8), 4, st), [])
        self.assertEqual(S.robbery_posts({**self.B, "role": "attacker"}, {}, {}, 5.0, (1, 3, 8), 4, st), [])
        st["robbery_at"] = 900.0
        self.assertEqual(S.robbery_posts(self.B, {}, {}, 1000.0, (1, 3, 8), 4, st), [])
        self.assertTrue(S.robbery_posts(self.B, {}, {}, 900.0 + S.ROBBERY_COOLDOWN + 1, (1, 3, 8), 4, st))


class WarningTests(unittest.TestCase):
    def post(self, agent="agent0009", kind="WARNING", raider="1:1:5", victim="1:4:6", t=1000.0):
        return {"id": 1, "t": t, "agent": agent, "kind": kind, "data": {"raider_at": raider, "victim_at": victim}}

    def test_careful_agents_listen_to_a_neighbours_warning(self):
        got = S.warned_raiders(LISTENER, (1, 3, 8), [self.post()], 1100.0, "me")
        self.assertEqual(got, [(1, 1, 5)])
        self.assertEqual(S.warned_raiders(WATCHER, (1, 3, 8), [self.post(kind="REVENGE")], 1100.0, "me"), [(1, 1, 5)])

    def test_raiders_and_traders_with_no_caution_do_not(self):
        for p in (PRESETS["raider"], PRESETS["balanced"], TRADER):
            self.assertEqual(S.warned_raiders(p, (1, 3, 8), [self.post()], 1100.0, "me"), [], p.name)

    def test_far_old_own_and_other_kinds_are_ignored(self):
        far = self.post(raider="1:300:5", victim="1:310:6")
        other_galaxy = self.post(raider="2:1:5", victim="2:4:6")
        old = self.post(t=0.0)
        mine = self.post(agent="me")
        taunt = self.post(kind="TAUNT")
        junk = {"id": 2, "t": 1000.0, "agent": "x", "kind": "WARNING", "data": "text"}
        self.assertEqual(S.warned_raiders(LISTENER, (1, 3, 8), [far, other_galaxy, old, mine, taunt, junk], 1000.0 + S.WARNING_MAX_AGE + 1, "me"), [])

    def test_a_raider_living_next_door_counts_even_if_the_victim_is_far(self):
        p = self.post(raider="1:5:5", victim="1:300:6")
        self.assertEqual(S.warned_raiders(LISTENER, (1, 3, 8), [p], 1100.0, "me"), [(1, 5, 5)])

    def test_guard_lasts_less_than_after_a_real_raid(self):
        from swarm.defense import RECENT_RAID
        now = 50_000.0
        left = RECENT_RAID - (now - S.guard_stamp(now))
        self.assertTrue(0 < left < RECENT_RAID)


class OfferTests(unittest.TestCase):
    RICH_IN_METAL = {"metal": 300_000, "crystal": 5_000, "deuterium": 20_000}

    def test_pair_is_surplus_for_need(self):
        self.assertEqual(S.pick_pair(self.RICH_IN_METAL)[:2], ("metal", "crystal"))
        self.assertIsNone(S.pick_pair({"metal": 50_000, "crystal": 20_000, "deuterium": 10_000}))    # balanced
        self.assertIsNone(S.pick_pair({"metal": 1000, "crystal": 100, "deuterium": 0}))            # too poor to trade

    def test_offer_uses_the_3_2_1_reference_prices_and_a_tenth_of_the_stock(self):
        o = S.make_offer(self.RICH_IN_METAL, (1, 3, 8), 4)
        self.assertEqual(o["kind"], "OFFER")
        self.assertEqual(o["data"]["give"], {"metal": 30_000})            # 10% of 300,000
        # 1 crystal = 1.5 metal, and the poster asks 3% more than fair
        self.assertEqual(o["data"]["want"], {"crystal": int(30_000 * 1.03 / 1.5)})
        self.assertEqual((o["data"]["at"], o["data"]["player_id"]), ("1:3:8", 4))
        self.assertLessEqual(len(o["text"]), 200)

    def test_a_need_led_stock_posts_a_want(self):
        o = S.make_offer({"metal": 1_000, "crystal": 100_000, "deuterium": 20_000}, (1, 3, 8), 4)
        self.assertEqual(o["kind"], "WANT")
        self.assertEqual(next(iter(o["data"]["want"])), "metal")

    def test_deuterium_is_worth_three_metal(self):
        o = S.make_offer({"metal": 1_000, "crystal": 5_000, "deuterium": 300_000}, (1, 3, 8), 4)
        self.assertEqual(o["data"]["give"], {"deuterium": 30_000})
        self.assertEqual(o["data"]["want"], {"metal": int(30_000 * 3 * 1.03)})

    def test_parse_trade_post_rejects_garbage(self):
        good = {"id": 1, "t": 1.0, "agent": "a", "kind": "OFFER",
                "data": {"give": {"metal": 100}, "want": {"crystal": 60}, "at": "1:2:3", "player_id": 5}}
        self.assertEqual(S.parse_trade_post(good)["give"], "metal")
        bad = [dict(good, kind="TAUNT"), dict(good, data="x"), dict(good, data=dict(good["data"], give={"gold": 5})),
               dict(good, data=dict(good["data"], give={"metal": 5, "crystal": 5})), dict(good, data=dict(good["data"], want={"metal": 1})),
               dict(good, data=dict(good["data"], at="nope")), dict(good, data=dict(good["data"], give={"metal": -5})),
               dict(good, data=dict(good["data"], give={"metal": 10 ** 12})), dict(good, data=dict(good["data"], player_id="x"))]
        for p in bad:
            self.assertIsNone(S.parse_trade_post(p), p)


class MatchTests(unittest.TestCase):
    STOCK = {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}

    def offer(self, id=1, agent="a2", give=("crystal", 13_000), want=("metal", 20_000), t=1000.0, **extra):
        return {"id": id, "t": t, "agent": agent, "kind": "OFFER",
                "data": {"give": {give[0]: give[1]}, "want": {want[0]: want[1]}, "at": "1:5:2", "player_id": 2, **extra}}

    def match(self, posts, rep=None, busy=(), answered=(), now=2000.0, board_now=1100.0):
        return S.pick_match(self.STOCK, posts, "me", board_now, rep or {}, now, set(busy), list(answered))

    def test_a_fair_offer_that_gives_what_we_miss_is_taken(self):
        m = self.match([self.offer()])
        self.assertEqual(m["terms"]["agent"], "a2")
        self.assertEqual(m["total_value"], 20_000)                    # a tenth of our metal
        self.assertAlmostEqual(m["r"], 20_000 / (13_000 * 1.5), places=6)

    def test_unfair_wrong_old_busy_shunned_and_answered_offers_are_skipped(self):
        self.assertIsNone(self.match([self.offer(give=("crystal", 10_000))]))                 # asks over 5% more than fair
        self.assertIsNone(self.match([self.offer(give=("deuterium", 5_000))]))                # not what we miss
        self.assertIsNone(self.match([self.offer(want=("deuterium", 20_000))]))               # wants what we do not have in surplus
        self.assertIsNone(self.match([self.offer(t=1.0)], board_now=1.0 + S.POST_MAX_AGE + 1))                                 # an old post (board time)
        self.assertIsNone(self.match([self.offer()], busy=["a2"]))
        self.assertIsNone(self.match([self.offer()], answered=[1]))
        self.assertIsNone(self.match([self.offer(agent="me")]))
        self.assertIsNone(self.match([self.offer(reply_to=5)]))                               # a reply, not an offer
        self.assertIsNone(self.match([self.offer()], rep={"a2": {"shun_until": 99_999.0}}))

    def test_the_better_price_and_the_better_reputation_win(self):
        cheap = self.offer(id=1, agent="a2", give=("crystal", 13_500))
        dear = self.offer(id=2, agent="a3", give=("crystal", 13_100))
        self.assertEqual(self.match([dear, cheap])["terms"]["agent"], "a2")
        self.assertEqual(self.match([dear, cheap], rep={"a3": {"ok": 3}})["terms"]["agent"], "a3")

    def test_small_stock_means_no_deal(self):
        m = S.pick_match({"metal": 25_000, "crystal": 0, "deuterium": 0}, [self.offer()], "me", 1100.0, {}, 2000.0, set(), [])
        self.assertIsNone(m)       # a tenth of 25,000 is below the minimum deal

    def test_replies_to_our_offer(self):
        r = self.offer(id=9, agent="a2", give=("crystal", 13_000), want=("metal", 20_000), reply_to=4)
        self.assertEqual([t["agent"] for t in S.replies_to([r], 4, "me", {}, 1.0, set())], ["a2"])
        self.assertEqual(S.replies_to([r], 5, "me", {}, 1.0, set()), [])
        self.assertEqual(S.replies_to([r], 4, "me", {}, 1.0, {"a2"}), [])
        self.assertEqual(S.replies_to([r], 4, "me", {"a2": {"shun_until": 10.0}}, 1.0, set()), [])


class DealTests(unittest.TestCase):
    STOCK = {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}

    def starter(self, total=20_000.0, r=1.03):
        return S.new_trade("a2", 2, (1, 5, 2), "metal", "crystal", total, r, "starter", 0.0)

    POSTER_STOCK = {"metal": 0, "crystal": 200_000, "deuterium": 20_000}

    def poster(self, total=20_000.0, r=0.97):
        return S.new_trade("a2", 2, (1, 5, 2), "crystal", "metal", total, r, "poster", 0.0)

    def test_the_starter_sends_one_small_instalment_on_credit_and_then_waits(self):
        t = self.starter()
        n = S.next_instalment(t, self.STOCK, [t])
        self.assertEqual(n, 5_000)                                  # a quarter of the deal, never more than a tenth of the stock
        S.after_sending(t, n, 100.0, 60)
        self.assertEqual(S.next_instalment(t, self.STOCK, [t]), 0)  # nothing more until something arrives
        S.after_receiving(t, {"crystal": 3_300}, 300.0)             # worth 4,950 metal
        self.assertGreater(S.next_instalment(t, self.STOCK, [t]), 0)

    def test_the_poster_sends_nothing_before_payment_arrives(self):
        t = self.poster()
        self.assertEqual(S.next_instalment(t, self.POSTER_STOCK, [t]), 0)
        S.after_receiving(t, {"metal": 5_000}, 10.0)               # worth 5,000
        n = S.next_instalment(t, self.POSTER_STOCK, [t])
        self.assertAlmostEqual(n * 1.5, 5_000 * 0.97, delta=2)      # exactly what the goods justify, at the agreed rate

    def test_wrong_resource_does_not_count_as_payment(self):
        t = self.poster()
        self.assertEqual(S.after_receiving(t, {"deuterium": 5_000, "crystal": 9}, 1.0), 0)
        self.assertEqual(S.next_instalment(t, self.POSTER_STOCK, [t]), 0)

    def test_nothing_above_a_tenth_of_the_stock_or_the_exposure_cap(self):
        t = self.starter(total=500_000.0)
        t["per"] = 500_000.0                                         # a silly deal
        self.assertLessEqual(S.next_instalment(t, self.STOCK, [t]), 20_000)
        poor = {"metal": 30_000, "crystal": 5_000, "deuterium": 20_000}
        self.assertLessEqual(S.next_instalment(t, poor, [t]), 3_000)
        # goods already out and unpaid use up the exposure allowance (10% of the stock's value)
        o = self.starter()
        o["sent_value"] = 40_000.0
        self.assertEqual(S.next_instalment(self.starter(), self.STOCK, [o]), 0)

    def test_a_full_deal_completes_and_builds_reputation(self):
        a, b = self.starter(), self.poster(total=20_000.0 / 1.03, r=1 / 1.03)
        now = 0.0
        for _ in range(12):
            n = S.next_instalment(a, self.STOCK, [a])
            if n:
                S.after_sending(a, n, now, 60)
                S.after_receiving(b, {"metal": n}, now + 60)
            m = S.next_instalment(b, self.POSTER_STOCK, [b])
            if m:
                S.after_sending(b, m, now + 60, 60)
                S.after_receiving(a, {"crystal": m}, now + 120)
            now += 200
        self.assertEqual(S.judge(a, now), "done")
        self.assertEqual(S.judge(b, now), "done")
        self.assertAlmostEqual(a["sent_value"], 20_000, delta=10)
        rep = {}
        S.record_done(rep, a, now)
        self.assertEqual(rep["a2"]["ok"], 1)
        self.assertFalse(S.is_shunned(rep, "a2", now))

    def test_non_payment_is_judged_a_cheat_only_after_the_deadline(self):
        t = self.starter()
        S.after_sending(t, 5_000, 100.0, 60)
        self.assertEqual(S.judge(t, 200.0), "open")
        self.assertEqual(S.judge(t, 100.0 + 2 * 60 + S.REPLY_SLACK - 1), "open")
        self.assertEqual(S.judge(t, 100.0 + 2 * 60 + S.REPLY_SLACK + 1), "cheated")

    def test_part_payment_within_the_tolerance_is_fine_a_big_shortfall_is_not(self):
        t = self.starter()
        S.after_sending(t, 5_000, 0.0, 60)
        S.after_receiving(t, {"crystal": 3_100}, 100.0)               # 4,650 of the 4,854 owed: within 10%
        self.assertNotEqual(S.judge(t, 10 ** 6), "cheated")
        u = self.starter()
        S.after_sending(u, 5_000, 0.0, 60)
        S.after_receiving(u, {"crystal": 1_000}, 100.0)
        self.assertEqual(S.judge(u, 10 ** 6), "cheated")

    def test_idle_deals_end_quietly(self):
        t = self.poster()
        self.assertEqual(S.judge(t, 10.0), "open")
        self.assertEqual(S.judge(t, S.TRADE_IDLE + 1), "expired")      # nobody ever came: not a cheat

    def test_cheaters_are_shunned_for_longer_each_time(self):
        rep = {}
        t = self.starter()
        S.after_sending(t, 5_000, 0.0, 60)
        S.record_cheat(rep, t, 1000.0)
        first = rep["a2"]["shun_until"] - 1000.0
        self.assertEqual(first, S.SHUN_SECONDS)
        self.assertTrue(S.is_shunned(rep, "a2", 2000.0))
        self.assertFalse(S.is_shunned(rep, "a2", 1000.0 + S.SHUN_SECONDS + 1))
        S.record_cheat(rep, t, 90_000.0)
        self.assertEqual(rep["a2"]["shun_until"] - 90_000.0, 2 * S.SHUN_SECONDS)

    def test_reputation_memory_is_bounded(self):
        rep = {}
        for i in range(S.REP_KEEP + 30):
            S.record_done(rep, {"partner": f"p{i}", "recv_value": 1.0, "sent_value": 1.0}, float(i))
        self.assertLessEqual(len(rep), S.REP_KEEP + 1)

    def test_trusted_partners_get_bigger_instalments(self):
        plain = S.new_trade("a", 1, (1, 1, 1), "metal", "crystal", 12_000, 1.0, "starter", 0.0)
        trusted = S.new_trade("a", 1, (1, 1, 1), "metal", "crystal", 12_000, 1.0, "starter", 0.0, rep={"ok": 2})
        self.assertGreater(trusted["per"], plain["per"])


class InboxTests(unittest.TestCase):
    BODY = "An incoming fleet from [1:3:8] has reached your planet [1:5:2] and delivered its goods:\nMetal: 5,000 Crystal: 0 Deuterium: 1.250"

    def test_parse_received(self):
        came, got = S.parse_received(self.BODY)
        self.assertEqual(came, (1, 3, 8))
        self.assertEqual(got, {"metal": 5000, "deuterium": 1250})
        self.assertEqual(S.parse_received("hello"), (None, {}))
        self.assertEqual(S.parse_received(None), (None, {}))

    def test_delivery_is_matched_by_sender_coordinates(self):
        t1 = S.new_trade("a1", 1, (1, 3, 8), "crystal", "metal", 9000, 1.0, "poster", 0.0)
        t2 = S.new_trade("a2", 2, (1, 9, 9), "crystal", "metal", 9000, 1.0, "poster", 0.0)
        trades = {"a1": t1, "a2": t2}
        self.assertIs(S.trade_for_delivery(trades, (1, 3, 8), {"metal": 5}), t1)
        self.assertIsNone(S.trade_for_delivery(trades, (4, 4, 4), {"metal": 5}))      # a stranger's cargo is not a deal
        self.assertIsNone(S.trade_for_delivery(trades, None, {"metal": 5}))           # two deals: cannot tell
        self.assertIs(S.trade_for_delivery({"a1": t1}, None, {"metal": 5}), t1)
        self.assertIsNone(S.trade_for_delivery(trades, (1, 3, 8), {"crystal": 5}))    # not what the deal is about


# ---- agents against a fake game and a real board ---------------------------------------------------------------------
class World(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = TokenStore(str(self.tmp / "state.json"))
        self.board = B.Board(None)
        self.server = B.serve(self.board, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.agents: list[Agent] = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def game(self, home, uid, res, ships=None, busy=True):
        g = FakeGame()
        g.home, g.uid = home, uid
        g.others = []         # the default fake victim has user id 2: not wanted here
        g.res = {k: float(v) for k, v in res.items()}
        for lvl in ("metal_store", "crystal_store", "deuterium_store"):
            g.levels[lvl] = 5
        g.ships.update(ships or {})
        g.slots_max = 3
        g.fuel_cost = 10
        if busy:     # a long build and research: the economy code stays out of the way of the stock under test
            g.bq = {"name": "metal_mine", "level": 1, "end": 10 ** 9, "dur": 10 ** 9}
            g.rq = {"name": "energy_technology", "level": 1, "end": 10 ** 9, "dur": 10 ** 9}
        return g

    def agent(self, name, uid, game, persona, board=True, seed=1):
        self.store.put(name, {"email": f"{name}@x", "password": "p", "token": "t", "persona": persona.name, "user_id": uid})
        a = Agent(name, persona, "http://x", self.store, random.Random(seed), log_dir=str(self.tmp / "logs"), client=game)
        a.user_id = uid
        if board:
            a.board = BoardClient(self.url, name)
        self.agents.append(a)
        return a

    def introduce(self, *games_and_names):
        """Every game knows the others as players (highscore) and can send transports to their home planet."""
        for g, name, uid in games_and_names:
            for g2, name2, uid2 in games_and_names:
                if g is not g2:
                    g.others.append({"user_id": uid2, "username": name2, "coord": g2.home, "score": 0,
                                     "resources": {"metal": 0, "crystal": 0, "deuterium": 0}, "ships": {}, "defense": {}})
                    g.peers[g2.home] = g2

    def events(self, agent, kind=None):
        path = agent.log_path
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [e for e in rows if kind is None or e["event"] == kind]

    def play(self, pairs, ticks, dt=100.0, until=None):
        for _ in range(ticks):
            for a, g in pairs:
                a.tick()
            for a, g in pairs:
                g.advance(dt)
            if until and until():
                break


class RobberyOnTheBoardTests(World):
    def test_a_robbed_agent_tells_the_board_and_remembers_it(self):
        g = self.game((1, 3, 8), 1, {"metal": 50_000, "crystal": 50_000, "deuterium": 5_000})
        a = self.agent("agent0001", 1, g, WATCHER)
        a.memory.opponents[(1, 1, 5)] = {"attacks": 0, "wins": 0, "losses": 0, "net": 0, "strength": 12_345}
        g.hostile = [{"id": 1, "mission": "attack", "is_mine": False, "is_return": False, "arrive_at": 30,
                      "from": {"galaxy": 1, "system": 1, "position": 5}, "to": {"planet_id": 10, "galaxy": 1, "system": 3, "position": 8}}]
        a.tick()                                                           # sees the fleet coming
        g.hostile = []
        g.advance(100)
        g._add_message("combat_report", {
            "target": {"galaxy": 1, "system": 3, "position": 8}, "attacker": {"player_id": 2, "resource_loss": 0, "units": {"light_fighter": 5}},
            "defender": {"player_id": 1, "resource_loss": 300}, "loot": {"metal": 8000, "crystal": 2000, "deuterium": 0},
            "debris": {}, "rounds": 3})
        a.tick()
        posts = self.board.read()
        self.assertEqual(sorted(p["kind"] for p in posts), ["REVENGE", "WARNING"])
        d = posts[0]["data"]
        self.assertEqual((d["raider_at"], d["raider_id"], d["victim_at"], d["victim_id"], d["loot"], d["strength"]),
                         ("1:1:5", 2, "1:3:8", 1, 10_000, 12_345))
        self.assertEqual(a.memory.opponents[(1, 1, 5)]["robbed"], 1)
        self.assertEqual(len(self.events(a, "board_post")), 2)
        # a second report straight away is not announced again
        g._add_message("combat_report", {
            "target": {"galaxy": 1, "system": 3, "position": 8}, "attacker": {"player_id": 2, "resource_loss": 0, "units": {"light_fighter": 5}},
            "defender": {"player_id": 1, "resource_loss": 0}, "loot": {"metal": 100, "crystal": 0, "deuterium": 0}, "debris": {}, "rounds": 3})
        g.advance(100)
        a.tick()
        self.assertEqual(len(self.board.read()), 2)

    def test_without_a_board_nothing_is_posted_and_nothing_remembered(self):
        g = self.game((1, 3, 8), 1, {"metal": 50_000, "crystal": 50_000, "deuterium": 5_000})
        a = self.agent("agent0001", 1, g, WATCHER, board=False)
        a.tick()
        g._add_message("combat_report", {"target": {"galaxy": 1, "system": 3, "position": 8}, "attacker": {"player_id": 2, "units": {}},
                                         "defender": {"player_id": 1, "resource_loss": 300}, "loot": {"metal": 8000}, "debris": {}, "rounds": 3})
        g.advance(100)
        a.tick()
        self.assertEqual(self.board.read(), [])
        self.assertEqual(a.memory.social, S.new_state())


class WarningOnTheBoardTests(World):
    def warn(self, agent="agent0009", raider="1:1:5", victim="1:4:6"):
        self.board.post(agent, "WARNING", "raider seen", {"raider_at": raider, "victim_at": victim, "raider_id": 2})

    def test_a_careful_agent_raises_its_guard_when_a_neighbour_is_raided(self):
        g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        a = self.agent("agent0001", 1, g, LISTENER)
        self.warn()
        delay = a.tick()
        self.assertIn((1, 1, 5), a.memory.aggressors)
        self.assertTrue(a._on_guard())
        self.assertLess(delay, 100)                       # on guard it looks every minute, at peace every three
        read = self.events(a, "board_read")[0]
        self.assertEqual((read["warnings"], read["guard"]), (1, ["1:1:5"]))
        a.tick()
        self.assertEqual(len(self.events(a, "board_read")), 1)        # the same post is not read twice

    def test_a_raider_ignores_warnings_and_so_does_a_far_neighbour(self):
        g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        raider = self.agent("agent0001", 1, g, replace(PRESETS["raider"], scout_interest=0.0, probes_wanted=0, army_share=0.0, expansion=0.0))
        self.warn()
        raider.tick()
        self.assertEqual(raider.memory.aggressors, {})
        g2 = self.game((1, 3, 8), 3, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        far = self.agent("agent0003", 3, g2, LISTENER)
        self.board.post("agent0009", "WARNING", "far away", {"raider_at": "1:400:5", "victim_at": "1:410:6"})
        far.tick()
        self.assertNotIn((1, 400, 5), far.memory.aggressors)

    def test_own_warnings_do_not_alarm_the_poster(self):
        g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        a = self.agent("agent0001", 1, g, LISTENER)
        self.warn(agent="agent0001")
        a.tick()
        self.assertEqual(a.memory.aggressors, {})


class BoardDownTests(World):
    def test_a_dead_board_changes_nothing_in_what_the_agent_does(self):
        results = []
        for mode in ("off", "dead", "off-trader", "dead-trader"):
            g = self.game((1, 3, 8), 1, {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}, ships={"small_cargo": 3}, busy=False)
            persona = TRADER if "trader" in mode else WATCHER
            a = self.agent("agent0001", 1, g, persona, board=False, seed=5)
            if mode.startswith("dead"):
                a.board = BoardClient("http://127.0.0.1:9", "agent0001", timeout=0.3)
            delays = []
            for _ in range(6):
                delays.append(a.tick())
                g.advance(100)
            results.append((delays, g.post_log, a.memory.to_dict()))
        self.assertEqual(results[0][:2], results[1][:2])
        self.assertEqual(results[2][:2], results[3][:2])
        a_dead = results[3][2]
        self.assertEqual(a_dead["social"]["trades"], {})

    def test_a_board_that_goes_away_midway_is_survived(self):
        g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        a = self.agent("agent0001", 1, g, LISTENER)
        a.tick()
        self.server.shutdown()
        self.server.server_close()
        a.board.timeout = 0.3
        for _ in range(3):
            a.tick()
            g.advance(100)
        self.assertGreater(a.board.down_until, 0)
        self.assertEqual(self.events(a, "api_error"), [])

    def test_a_social_bug_never_costs_the_agent_its_round(self):
        g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500})
        a = self.agent("agent0001", 1, g, LISTENER)
        a._social_read = lambda home: 1 / 0
        a.tick()
        self.assertEqual(self.events(a, "board_error")[0]["type"], "ZeroDivisionError")
        a.tick()
        self.assertEqual(len(self.events(a, "board_error")), 1)            # logged once, not every round


class TradeTests(World):
    A_STOCK = {"metal": 200_000, "crystal": 5_000, "deuterium": 20_000}
    B_STOCK = {"metal": 20_000, "crystal": 200_000, "deuterium": 20_000}

    def pair(self, a_persona=TRADER, b_persona=None):
        ga = self.game((1, 3, 8), 1, self.A_STOCK, ships={"small_cargo": 10})
        gb = self.game((1, 5, 2), 2, self.B_STOCK, ships={"small_cargo": 10})
        self.introduce((ga, "Alpha", 1), (gb, "Beta", 2))
        a = self.agent("agent0001", 1, ga, a_persona, seed=1)
        # Beta only posts and replies (it hardly ever answers an offer itself), so Alpha is the one that starts
        b = self.agent("agent0002", 2, gb, b_persona or replace(TRADER, trade_interest=1e-9), seed=2)
        return a, ga, b, gb

    def test_two_traders_swap_goods_in_instalments_tit_for_tat(self):
        a, ga, b, gb = self.pair()
        a_done = lambda: any(e["action"] == "done" for e in self.events(a, "trade_offer"))
        self.play([(a, ga), (b, gb)], 60, until=a_done)
        self.assertTrue(a_done())
        # nothing was drained: every flight is at most a tenth of the stock of what it carries
        for g, stock in ((ga, self.A_STOCK), (gb, self.B_STOCK)):
            self.assertGreaterEqual(len(g.sent_to_peers), 3)                   # small instalments, not one big shipment
            for sent in g.sent_to_peers:
                for r, v in sent["resources"].items():
                    self.assertLessEqual(v, 0.10 * stock[r], (r, v))
        self.assertEqual({r for s in ga.sent_to_peers for r in s["resources"]}, {"metal"})
        self.assertEqual({r for s in gb.sent_to_peers for r in s["resources"]}, {"crystal"})
        # the one who answered went first; the poster only sent after its first delivery had arrived
        self.assertLess(ga.sent_to_peers[0]["at"], gb.sent_to_peers[0]["at"])
        self.assertGreaterEqual(gb.sent_to_peers[0]["at"], ga.sent_to_peers[0]["at"] + 60)
        # a fair swap at the reference prices: Alpha gave 20,000 metal-worth, Beta gave about the same value in crystal
        a_gave = sum(s["resources"].get("metal", 0) for s in ga.sent_to_peers)
        b_gave = sum(s["resources"].get("crystal", 0) for s in gb.sent_to_peers)
        self.assertAlmostEqual(a_gave, 20_000, delta=10)
        self.assertEqual(self.events(b, "trade_offer")[-1]["action"], "accepted")
        ratio = self.events(b, "trade_offer")[0]["ratio"]               # the posted markup: 1.03 learned start, nudged by up to 3% (exploration)
        self.assertTrue(1.03 * 0.97 - 1e-9 <= ratio <= 1.03 * 1.03 + 1e-9)
        self.assertAlmostEqual(b_gave * 1.5, a_gave / ratio, delta=a_gave * 0.02)
        self.assertGreater(ga.res["crystal"], self.A_STOCK["crystal"] + 12_000)
        self.assertGreater(gb.res["metal"], self.B_STOCK["metal"] + 19_000)
        # the log and the reputation say what happened
        for who in (a, b):
            self.assertTrue(self.events(who, "trade_sent"))
            self.assertTrue(self.events(who, "trade_received"))
            self.assertEqual(self.events(who, "trade_cheated"), [])
        self.assertEqual(a.memory.social["rep"]["agent0002"]["ok"], 1)
        self.assertEqual(a.memory.social["trades"], {})
        # Beta, the poster, has been paid in full and has paid back what it owed; the deal ends once nothing more comes
        t = b.memory.social["trades"]["agent0001"]
        self.assertAlmostEqual(t["recv_value"], 20_000, delta=10)
        self.assertEqual(S.debt(t) < S.DEBT_ABS, True)
        self.play([(a, ga), (b, gb)], 60, until=lambda: any(e["action"] == "done" for e in self.events(b, "trade_offer")))
        self.assertEqual(b.memory.social["rep"]["agent0001"]["ok"], 1)
        self.assertEqual(self.events(a, "trade_offer")[0]["action"], "posted")
        self.assertIn("answered", [e["action"] for e in self.events(a, "trade_offer")])
        self.assertIn("accepted", [e["action"] for e in self.events(b, "trade_offer")])
        self.assertTrue(any(p["data"].get("reply_to") for p in self.board.read(kinds=["OFFER"])))

    def test_a_cheat_gets_one_instalment_and_is_shunned(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK, ships={"small_cargo": 10})
        gb = self.game((1, 5, 2), 2, self.B_STOCK, ships={"small_cargo": 10})
        self.introduce((ga, "Alpha", 1), (gb, "Beta", 2))
        a = self.agent("agent0001", 1, ga, TRADER)
        self.store.put("agent0002", {"email": "b@x", "password": "p", "token": "t", "persona": "trader", "user_id": 2})
        offer = {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 2}
        self.board.post("agent0002", "OFFER", "OFFER 13,000 crystal for 20,000 metal", offer)
        self.play([(a, ga)], 40)                                                # Beta takes the goods and never pays
        self.assertEqual(len(ga.sent_to_peers), 1)                              # one instalment of credit and no more
        cheated = self.events(a, "trade_cheated")
        self.assertEqual(len(cheated), 1)
        self.assertEqual(cheated[0]["partner"], "agent0002")
        self.assertGreater(cheated[0]["owed"], 4_000)
        self.assertTrue(S.is_shunned(a.memory.social["rep"], "agent0002", ga.now))
        # a new, even better, offer from the cheat is ignored
        self.board.post("agent0002", "OFFER", "OFFER 14,000 crystal for 20,000 metal", dict(offer, give={"crystal": 14_000}))
        self.play([(a, ga)], 6)
        self.assertEqual(len(ga.sent_to_peers), 1)
        replies = [p for p in self.board.read(agent="agent0001") if p["data"].get("reply_to")]
        self.assertEqual(len(replies), 1)

    def test_offers_from_strangers_and_humans_are_never_answered(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK, ships={"small_cargo": 10})
        gb = self.game((1, 5, 2), 2, self.B_STOCK)
        self.introduce((ga, "Alpha", 1), (gb, "Beta", 2))
        a = self.agent("agent0001", 1, ga, TRADER)
        # the poster is a player in the ranking but not an agent of ours (no account in agents_state.json): a human
        self.board.post("agent0002", "OFFER", "OFFER crystal", {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 2})
        # an agent that names someone else's planet as its own
        self.store.put("agent0003", {"email": "c@x", "password": "p", "token": "t", "persona": "trader", "user_id": 3})
        self.board.post("agent0003", "OFFER", "OFFER crystal", {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 3})
        self.board.post("agent0001", "OFFER", "my own", {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 2})
        self.play([(a, ga)], 10)
        self.assertEqual(ga.sent_to_peers, [])
        self.assertEqual(a.memory.social["trades"], {})
        self.assertEqual([p for p in self.board.read(agent="agent0001") if p["data"].get("reply_to")], [])

    def test_posts_per_hour_are_capped(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK, ships={"small_cargo": 10})
        a = self.agent("agent0001", 1, ga, TRADER)
        for _ in range(S.POSTS_PER_HOUR + 5):
            a._board_post("TAUNT", "hello there", {"n": random.random()})
        self.assertEqual(len(self.board.read(agent="agent0001")), S.POSTS_PER_HOUR)

    def test_a_trader_without_cargo_ships_builds_a_few_and_never_many(self):
        ga = self.game((1, 3, 8), 1, self.A_STOCK)
        gb = self.game((1, 5, 2), 2, self.B_STOCK)
        ga.levels["shipyard"] = 1
        self.introduce((ga, "Alpha", 1), (gb, "Beta", 2))
        a = self.agent("agent0001", 1, ga, TRADER)
        self.store.put("agent0002", {"email": "b@x", "password": "p", "token": "t", "persona": "trader", "user_id": 2})
        self.board.post("agent0002", "OFFER", "OFFER crystal", {"give": {"crystal": 13_000}, "want": {"metal": 20_000}, "at": "1:5:2", "player_id": 2})
        self.play([(a, ga)], 8, dt=200)
        built = [e for e in self.events(a, "units") if e["what"] == "small_cargo"]
        self.assertTrue(built)
        self.assertLessEqual(sum(e["amount"] for e in built), 5 * len(built))
        self.assertTrue(ga.sent_to_peers)                  # and once built, they carry the first instalment


class WiringTests(unittest.TestCase):
    def run_swarm(self, *args):
        return subprocess.run([sys.executable, "run_swarm.py", *args], cwd=AGENTS_DIR, capture_output=True, text=True, timeout=60)

    def test_board_flag_is_optional_and_refuses_a_remote_address(self):
        r = self.run_swarm("--help")
        self.assertIn("--board URL", r.stdout)
        bad = self.run_swarm("--board", "http://example.com:8765", "--count", "1")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("this computer only", bad.stderr)

    def test_default_agent_has_no_board(self):
        self.assertIsNone(Agent.board)
        self.assertIsInstance(NullBoard(), NullBoard)


if __name__ == "__main__":
    unittest.main()
