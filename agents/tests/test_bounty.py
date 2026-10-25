import json
import random
import time
import unittest
from dataclasses import replace

from swarm import bounty as B
from swarm import social as S
from swarm.military import Intel, Memory, pick_attack, pick_target
from swarm.boardclient import BoardClient
from swarm.personas import PRESETS
from tests.test_social import LISTENER, TRADER, WATCHER, World

HUNTER = replace(PRESETS["raider"], randomness=0.0, aggression=1.0, scout_interest=1.0, army_share=0.0, probes_wanted=0, expansion=0.0,
                 expedition_interest=0.0, caution=0.0, spy_cooldown_s=0.0, attack_cooldown_s=0.0, gamble=1.0, min_loot=100)
STOCK = {"metal": 40_000, "crystal": 10_000, "deuterium": 5_000}      # worth 70,000


def call(id=11, agent="agent0003", raider="1:1:5", rid=2, victim="1:5:2", loot=20_000, t=1000.0, **extra):
    data = {"raider_at": raider, "raider_id": rid, "victim_at": victim, "loot": loot, **extra}
    return {"id": id, "t": t, "agent": agent, "kind": "REVENGE", "text": "x", "data": data}


def bounty_post(id=21, agent="agent0003", pid=2, at="1:1:5", reward=None, expires=10 ** 10, poster_id=3, poster_at="1:5:2", t=1000.0):
    return {"id": id, "t": t, "agent": agent, "kind": "BOUNTY", "text": "x",
            "data": {"target_player_id": pid, "target_at": at, "reward": reward if reward is not None else {"metal": 4000}, "expires": expires,
                     "poster_id": poster_id, "poster_at": poster_at}}


# ---- pure rules ---------------------------------------------------------------------------------------------------------
class WhoTests(unittest.TestCase):
    def test_fighters_hunt_peaceful_personalities_do_not(self):
        self.assertTrue(B.is_hunter(PRESETS["raider"]))
        self.assertTrue(B.is_hunter(PRESETS["balanced"]))
        self.assertTrue(B.is_hunter(replace(PRESETS["balanced"], aggression=0.1)))        # general class with some aggression
        for name in ("miner", "scientist", "turtle", "trader"):
            self.assertFalse(B.is_hunter(PRESETS[name]), name)
        self.assertFalse(B.is_hunter(replace(PRESETS["raider"], aggression=0.0)))

    def test_friends_are_partners_who_paid_open_deals_and_joined_revenges(self):
        st = S.new_state()
        st["rep"] = {"a1": {"ok": 2, "cheated": 0}, "a2": {"ok": 1, "cheated": 1}, "a3": {"ok": 0, "cheated": 0}}
        st["trades"] = {"a4": {"partner": "a4", "status": "open"}}
        B.add_friend(st, "a5")
        self.assertEqual(B.friend_names(st), {"a1", "a4", "a5"})

    def test_state_is_backwards_compatible(self):
        old = {"cursor": 5, "read_at": 1.0, "posted": [1.0], "offer": None, "trades": {}, "rep": {"a": {"ok": 1}}}
        st = S.clean_state(old)
        self.assertEqual(st["cursor"], 5)
        for key in ("revenge", "friends", "bounties", "bounty_out", "claims", "owed", "pay", "bounty_done"):
            self.assertIn(key, st)
        junk = S.clean_state({"revenge": "x", "friends": [1, "a"], "bounties": [], "owed": {"a": 1}, "pay": {"x": {"y": 1}}})
        self.assertEqual((junk["revenge"], junk["friends"], junk["bounties"], junk["owed"], junk["pay"]), (None, ["a"], {}, {}, {"x": {"y": 1}}))
        m = Memory()
        m.social["revenge"] = {"post": 1, "until": 5.0}
        m.social["bounties"]["7"] = {"id": 7}
        back = Memory.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual((back.social["revenge"], back.social["bounties"]), ({"post": 1, "until": 5.0}, {"7": {"id": 7}}))


class RevengeTests(unittest.TestCase):
    def pick(self, posts, persona=HUNTER, strength=100_000.0, opponents=None, friend_ids=(), allowed=None, board_now=1100.0, my_id=1):
        return B.pick_revenge(persona, posts, board_now, "agent0001", (1, 3, 8), my_id, strength, opponents or {}, set(friend_ids), allowed)

    def test_a_hunter_joins_the_biggest_call(self):
        got = self.pick([call(id=11, loot=5_000), call(id=12, loot=30_000, raider="1:2:2", rid=5)])
        self.assertEqual((got["id"], got["raider"], got["raider_id"]), (12, (1, 2, 2), 5))

    def test_peaceful_personalities_ignore_it(self):
        for persona in (LISTENER, WATCHER, TRADER, PRESETS["scientist"]):
            self.assertIsNone(self.pick([call()], persona=persona))

    def test_replies_own_old_and_malformed_calls_are_skipped(self):
        self.assertIsNone(self.pick([call(reply_to=3)]))
        self.assertIsNone(self.pick([call(agent="agent0001")]))
        self.assertIsNone(self.pick([call(t=1000.0)], board_now=1000.0 + S.WARNING_MAX_AGE + 1))
        self.assertIsNone(self.pick([call(raider="nonsense")]))
        self.assertIsNone(self.pick([{"id": 1, "kind": "WARNING", "agent": "x", "t": 1000.0, "data": {"raider_at": "1:1:5"}}]))

    def test_never_ourselves_a_friend_or_a_human(self):
        self.assertIsNone(self.pick([call(rid=1)]))                                    # the raider is me
        self.assertIsNone(self.pick([call(raider="1:3:8")]))                           # my own planet
        self.assertIsNone(self.pick([call()], friend_ids={2}))                         # the raider is a friend of mine: still no
        self.assertIsNotNone(self.pick([call(agent="agent0007")]))                     # (a call by a friend is fine: the RAIDER must not be one)
        self.assertIsNone(self.pick([call()], allowed={7, 8}))                         # the raider is not one of our agents
        self.assertIsNone(self.pick([call(rid=None)], allowed={7, 8}))
        self.assertIsNotNone(self.pick([call()], allowed={2}))

    def test_never_someone_far_stronger(self):
        self.assertIsNone(self.pick([call(strength=400_000)], strength=100_000))               # the poster's figure
        self.assertIsNone(self.pick([call()], strength=100_000, opponents={(1, 1, 5): {"strength": 400_000}}))   # our own memory wins
        self.assertIsNotNone(self.pick([call(strength=400_000)], strength=100_000, opponents={(1, 1, 5): {"strength": 90_000}}))
        self.assertIsNotNone(self.pick([call(strength=140_000)], strength=100_000))            # a bit stronger is fine
        self.assertIsNone(self.pick([call()], strength=0.0))                                   # no fleet, nothing to avenge with

    def test_one_revenge_at_a_time_and_it_expires(self):
        st = S.new_state()
        self.assertIsNone(B.revenge_active(st, 0.0))
        st["revenge"] = B.revenge_state(B.parse_revenge(call()), 100.0)
        self.assertEqual(st["revenge"]["until"], 100.0 + 2 * 3600)
        self.assertTrue(B.revenge_active(st, 100.0 + 7199))
        self.assertIsNone(B.revenge_active(st, 100.0 + 7201))

    def test_the_reply_points_at_the_call(self):
        reply = B.revenge_reply(B.parse_revenge(call()), (1, 3, 8))
        self.assertEqual(reply["kind"], "REVENGE")
        self.assertEqual((reply["data"]["reply_to"], reply["data"]["raider_at"], reply["data"]["raider_id"]), (11, "1:1:5", 2))
        self.assertLessEqual(len(reply["text"]), 200)
        self.assertIsNone(B.parse_revenge({"kind": "REVENGE", "id": 5, "data": dict(reply["data"])}))     # a reply is not a call


class BountyPostingTests(unittest.TestCase):
    ROB = {"raider_at": "1:1:5", "raider_id": 2}

    def offer(self, loot=20_000, stock=None, rob=None, my_id=3):
        return B.bounty_offer(loot, rob if rob is not None else self.ROB, STOCK if stock is None else stock, (1, 5, 2), my_id, 5000.0)

    def test_a_big_robbery_of_a_rich_victim_posts_a_capped_reward(self):
        o = self.offer()
        d = o["data"]
        self.assertEqual((o["kind"], d["target_player_id"], d["target_at"], d["poster_id"], d["poster_at"]), ("BOUNTY", 2, "1:1:5", 3, "1:5:2"))
        (res, amount), = d["reward"].items()
        self.assertEqual(res, "metal")
        self.assertLessEqual(amount, 0.10 * STOCK["metal"])
        self.assertLessEqual(S.value_of(d["reward"]), 0.10 * S.stock_value(STOCK))
        self.assertGreater(d["expires"], 5000 + 5 * 3600)
        self.assertLessEqual(len(o["text"]), 200)
        self.assertLessEqual(len(json.dumps(d)), 600)

    def test_the_reward_is_capped_at_ten_percent_of_the_stock_whatever_the_loot(self):
        d = self.offer(loot=10 ** 7, stock={"metal": 500_000, "crystal": 0, "deuterium": 0})["data"]
        self.assertEqual(d["reward"], {"metal": 50_000})

    def test_no_bounty_for_small_loot_small_share_poor_posters_or_unknown_raiders(self):
        self.assertIsNone(self.offer(loot=4_000))
        self.assertIsNone(self.offer(loot=6_000))                                       # 6,000 of 76,000 is not "a lot"
        self.assertIsNone(self.offer(stock={"metal": 10_000, "crystal": 0, "deuterium": 0}))
        self.assertIsNone(self.offer(rob={"raider_at": "1:1:5"}))
        self.assertIsNone(self.offer(rob={"raider_id": 2}))
        self.assertIsNone(self.offer(my_id=None))
        self.assertIsNone(B.bounty_offer(20_000, self.ROB, None, (1, 5, 2), 3, 5000.0))

    def test_one_open_bounty_at_a_time(self):
        st = S.new_state()
        st["bounty_out"]["1"] = {"expires": 9000.0, "paid": False}
        self.assertIsNotNone(B.open_bounty(st, 5000.0))
        self.assertIsNone(B.open_bounty(st, 9001.0))
        st["bounty_out"]["1"]["paid"] = True
        self.assertIsNone(B.open_bounty(st, 5000.0))


class BountyReadingTests(unittest.TestCase):
    def test_parse(self):
        b = B.parse_bounty(bounty_post())
        self.assertEqual((b["pid"], b["at"], b["reward"], b["poster"], b["value"], b["poster_at"]), (2, "1:1:5", {"metal": 4000}, "agent0003", 4000.0, "1:5:2"))
        self.assertEqual(B.parse_bounty(bounty_post(reward={"metal": 1000, "deuterium": 100}))["value"], 1300.0)
        for bad in (dict(reward={"gold": 5}), dict(reward={"metal": -5}), dict(reward={}), dict(at="x"), dict(pid=None), dict(poster_at="1:2")):
            post = bounty_post(**bad)
            if bad.get("pid", 0) is None:
                post["data"]["target_player_id"] = None
            self.assertIsNone(B.parse_bounty(post), bad)
        self.assertIsNone(B.parse_bounty({"kind": "WARNING", "id": 1, "data": {}}))

    def test_remember_skips_own_expired_and_shunned_posters_and_forgets(self):
        st = S.new_state()
        S.record_cheat(st["rep"], {"partner": "agent0009", "sent_value": 1.0, "recv_value": 0.0}, 100.0)
        posts = [bounty_post(id=1), bounty_post(id=2, agent="agent0001"), bounty_post(id=3, expires=100), bounty_post(id=4, agent="agent0009")]
        new = B.remember_bounties(st, posts, "agent0001", 5000.0, 200.0)
        self.assertEqual([b["id"] for b in new], [1])
        self.assertEqual(list(st["bounties"]), ["1"])
        B.remember_bounties(st, [], "agent0001", 10 ** 11, 200.0)                       # time passes: it expired
        self.assertEqual(st["bounties"], {})

    def test_only_so_many_are_remembered(self):
        st = S.new_state()
        B.remember_bounties(st, [bounty_post(id=i, t=float(i)) for i in range(1, 40)], "me", 5000.0, 0.0)
        self.assertEqual(len(st["bounties"]), B.BOUNTY_KEEP)
        self.assertIn("39", st["bounties"])


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.st = S.new_state()
        self.rng = random.Random(1)

    def focus(self, persona=HUNTER, friend_ids=(), allowed=None, now=0.0, wall=5000.0):
        return B.build_focus(self.st, persona, now, wall, 1, set(friend_ids), allowed)

    def intel(self, loot, owner):
        return {"at": 100.0, "loot": loot, "defenders": 0, "owner_id": owner, "ships": {}, "defense": {}, "research": {}}

    def test_the_reward_is_added_to_the_target_score(self):
        mem = Memory()
        mem.intel = {(1, 1, 5): self.intel(1000, 2), (1, 2, 2): self.intel(1500, 5)}
        pick = lambda f: pick_attack(HUNTER, self.rng, mem, 200.0, {"light_fighter": 10}, focus=f)[0]
        self.assertEqual(pick(None), (1, 2, 2))                                        # more loot wins without a bounty
        B.remember_bounties(self.st, [bounty_post(reward={"metal": 4000})], "me", 5000.0, 0.0)
        self.assertEqual(pick(self.focus()), (1, 1, 5))                                # 1,000 + 4,000 beats 1,500
        B.remember_bounties(self.st, [bounty_post(id=22, pid=5, at="1:2:2", reward={"deuterium": 2000})], "me", 5000.0, 0.0)
        self.assertEqual(pick(self.focus()), (1, 2, 2))                                # 1,500 + 6,000 beats 5,000

    def test_the_reward_value_uses_the_reference_prices(self):
        B.remember_bounties(self.st, [bounty_post(reward={"deuterium": 1000, "crystal": 1000})], "me", 5000.0, 0.0)
        self.assertEqual(self.focus()["ids"][2], (1.0, 4500.0))

    def test_a_joined_revenge_multiplies_the_worth_and_attracts_the_spy(self):
        self.st["revenge"] = B.revenge_state(B.parse_revenge(call()), 0.0)
        f = self.focus()
        self.assertEqual(f["coords"][(1, 1, 5)], (B.REVENGE_BONUS, 0.0))
        mem = Memory()
        mem.intel = {(1, 1, 5): self.intel(1000, 2), (1, 2, 2): self.intel(1500, 5)}
        self.assertEqual(pick_attack(HUNTER, self.rng, mem, 200.0, {"light_fighter": 10}, focus=f)[0], (1, 1, 5))
        players = [{"user_id": 2, "username": "p2", "score": 0, "main_planet": {"galaxy": 1, "system": 90, "position": 5}},
                   {"user_id": 5, "username": "p5", "score": 0, "main_planet": {"galaxy": 1, "system": 2, "position": 2}}]
        st = S.new_state()
        far = B.new_focus()
        far["ids"][2] = (2.0, 0.0)
        kw = dict(me=(1, 3, 8), my_score=100, players=players, memory=Memory(), now=0.0, allowed_ids=None, cooldown=0.0)
        self.assertEqual(pick_target(HUNTER, self.rng, focus=far, **kw)["user_id"], 2)       # the wanted one, though it lives farther away
        self.assertIn(pick_target(HUNTER, random.Random(3), focus=None, **kw)["user_id"], (2, 5))

    def test_revenge_expires_and_peaceful_personalities_have_no_focus(self):
        self.st["revenge"] = B.revenge_state(B.parse_revenge(call()), 0.0)
        self.assertIsNone(self.focus(now=B.REVENGE_SECONDS + 1))
        B.remember_bounties(self.st, [bounty_post()], "me", 5000.0, 0.0)
        self.assertIsNone(self.focus(persona=WATCHER))

    def test_never_a_friend_ourselves_a_human_or_an_expired_bounty(self):
        B.remember_bounties(self.st, [bounty_post(), bounty_post(id=22, pid=1, at="1:3:8")], "me", 5000.0, 0.0)
        self.assertIsNone(self.focus(friend_ids={2}, allowed={1, 2}))
        self.assertIsNone(self.focus(allowed={7}))
        self.assertIsNone(self.focus(wall=10 ** 11))
        self.assertEqual(set(self.focus()["ids"]), {2})                              # (player 1 is us: no bounty on ourselves)

    def test_a_bounty_we_already_claimed_is_not_hunted_again(self):
        B.remember_bounties(self.st, [bounty_post()], "me", 5000.0, 0.0)
        self.st["claims"]["21"] = {"claim": 9}
        self.assertIsNone(self.focus())

    def test_no_focus_changes_nothing(self):
        mem = Memory()
        mem.intel = {(1, 2, 2): self.intel(1500, 5)}
        a = pick_attack(HUNTER, random.Random(1), mem, 200.0, {"light_fighter": 10})
        b = pick_attack(HUNTER, random.Random(1), mem, 200.0, {"light_fighter": 10}, focus=None)
        self.assertEqual(a[0], b[0])


class ClaimTests(unittest.TestCase):
    BATTLE = {"role": "attacker", "probes_only": False, "loot": 9_000, "at": "1:1:5", "their_id": 2, "report_id": 77}

    def setUp(self):
        self.st = S.new_state()
        B.remember_bounties(self.st, [bounty_post()], "agent0001", 5000.0, 0.0)

    def claims(self, b=None, name="agent0001", my_id=1):
        return B.claims_for(b or self.BATTLE, self.st, name, my_id, (1, 3, 8), 5000.0, 0.0)

    def test_a_win_with_loot_on_the_target_is_claimed(self):
        (bounty, post), = self.claims()
        d = post["data"]
        self.assertEqual((post["kind"], d["bounty_id"], d["report"], d["at"], d["claimant_id"], d["claimant_at"], d["loot"]),
                         ("CLAIM", 21, 77, "1:1:5", 1, "1:3:8", 9000))
        self.assertLessEqual(len(json.dumps(d)), 600)

    def test_matched_by_coordinates_when_the_player_id_is_unknown(self):
        self.assertEqual(len(self.claims(dict(self.BATTLE, their_id=None))), 1)

    def test_no_claim_without_loot_on_other_targets_as_defender_or_for_own_bounty(self):
        self.assertEqual(self.claims(dict(self.BATTLE, loot=0)), [])
        self.assertEqual(self.claims(dict(self.BATTLE, their_id=9, at="1:9:9")), [])
        self.assertEqual(self.claims(dict(self.BATTLE, role="defender")), [])
        self.assertEqual(self.claims(dict(self.BATTLE, probes_only=True)), [])
        self.assertEqual(self.claims(my_id=3), [])                                    # the poster's own id
        self.assertEqual(self.claims(name="agent0003"), [])

    def test_a_bounty_is_claimed_once(self):
        self.st["claims"]["21"] = {"claim": 5}
        self.assertEqual(self.claims(), [])


class VerdictAndPaymentTests(unittest.TestCase):
    def verdict(self, agent=B.REFEREE, **kw):
        d = {"bounty_id": 21, "claim_id": 31, "ok": True, "why": "fine", "claimant": "agent0001", "claimant_id": 1, "claimant_at": "1:3:8"}
        d.update(kw)
        return {"id": 40, "kind": "VERDICT", "agent": agent, "data": d}

    def test_only_the_referee_counts(self):
        self.assertEqual(B.parse_verdict(self.verdict())["claimant_at"], "1:3:8")
        self.assertIsNone(B.parse_verdict(self.verdict(agent="agent0001")))
        self.assertIsNone(B.parse_verdict(self.verdict(ok="yes")))
        self.assertIsNone(B.parse_verdict({"kind": "VERDICT", "agent": B.REFEREE, "data": {}}))

    def test_payment_is_the_promise_capped_by_the_stock_and_fuel(self):
        self.assertEqual(B.payment_amount({"metal": 4000}, {"metal": 40_000}, 10), {"metal": 4000})
        self.assertEqual(B.payment_amount({"metal": 4000}, {"metal": 10_000}, 10), {"metal": 2000})        # 20% of what is left
        self.assertEqual(B.payment_amount({"deuterium": 900}, {"deuterium": 1000}, 100), {"deuterium": 180})
        self.assertEqual(B.payment_amount({"metal": 4000}, {"metal": 0}, 10), {})

    def test_a_delivery_from_the_poster_pays_a_bounty_only_when_it_covers_it(self):
        owed = {"21": {"poster_at": "1:5:2", "reward": {"metal": 4000}}}
        self.assertEqual(B.owed_for_delivery(owed, (1, 5, 2), {"metal": 4000}), "21")
        self.assertEqual(B.owed_for_delivery(owed, (1, 5, 2), {"metal": 2000}), "21")
        self.assertIsNone(B.owed_for_delivery(owed, (1, 5, 2), {"metal": 500}))
        self.assertIsNone(B.owed_for_delivery(owed, (1, 5, 2), {"crystal": 4000}))
        self.assertIsNone(B.owed_for_delivery(owed, (1, 9, 9), {"metal": 4000}))

    def test_a_poster_that_does_not_pay_is_shunned_like_a_cheat_for_longer_each_time(self):
        rep = {}
        B.record_unpaid(rep, "agent0003", 1000.0)
        self.assertTrue(S.is_shunned(rep, "agent0003", 1000.0 + S.SHUN_SECONDS - 1))
        self.assertFalse(S.is_shunned(rep, "agent0003", 1000.0 + S.SHUN_SECONDS + 1))
        B.record_unpaid(rep, "agent0003", 1000.0)
        self.assertGreater(rep["agent0003"]["shun_until"], 1000.0 + 1.9 * S.SHUN_SECONDS)
        B.record_paid(rep, "agent0004", 4000.0, 5.0)
        self.assertEqual(rep["agent0004"]["ok"], 1)


# ---- agents against a fake game and a real board ---------------------------------------------------------------------------------------------
class Story(World):
    """P (agent0003) is robbed by T (player 2, 1:1:5). H (agent0001, a raider) hunts."""
    P_STOCK = {"metal": 40_000, "crystal": 10_000, "deuterium": 5_000}

    def setUp(self):
        super().setUp()
        self.store.put("agent0002", {"email": "t@x", "password": "p", "token": "t", "persona": "raider", "user_id": 2})

    def poster(self, persona=WATCHER, stock=None, ships=None):
        gp = self.game((1, 5, 2), 3, stock or self.P_STOCK, ships=ships if ships is not None else {"small_cargo": 5})
        gp.others = []
        p = self.agent("agent0003", 3, gp, persona)
        return p, gp

    def rob(self, p, gp, loot=20_000, raider_id=2):
        gp.hostile = [{"id": 1, "mission": "attack", "is_mine": False, "is_return": False, "arrive_at": 30,
                       "from": {"galaxy": 1, "system": 1, "position": 5}, "to": {"planet_id": 10, "galaxy": 1, "system": 5, "position": 2}}]
        p.tick()
        gp.hostile = []
        gp.advance(100)
        gp._add_message("combat_report", {
            "target": {"galaxy": 1, "system": 5, "position": 2}, "attacker": {"player_id": raider_id, "resource_loss": 0, "units": {"light_fighter": 5}},
            "defender": {"player_id": 3, "resource_loss": 300}, "loot": {"metal": loot, "crystal": 0, "deuterium": 0}, "debris": {}, "rounds": 3})
        p.tick()

    def hunter(self, persona=HUNTER, fighters=30, target_stock=None, uid=1, name="agent0001"):
        gh = self.game((1, 3, 8), uid, {"metal": 5_000, "crystal": 5_000, "deuterium": 5_000},
                       ships={"light_fighter": fighters, "small_cargo": 10, "espionage_probe": 5}, busy=True)
        gh.others = [{"user_id": 2, "username": "Target", "coord": (1, 1, 5), "score": 0,
                      "resources": target_stock or {"metal": 40_000, "crystal": 20_000, "deuterium": 10_000}, "ships": {}, "defense": {}}]
        return self.agent(name, uid, gh, persona), gh

    def posts(self, kind):
        return self.board.read(kinds=[kind])


class PostingTests(Story):
    def test_a_robbed_rich_agent_posts_a_capped_bounty_and_remembers_it(self):
        p, gp = self.poster()
        self.rob(p, gp)
        (b,) = self.posts("BOUNTY")
        d = b["data"]
        self.assertEqual((d["target_player_id"], d["target_at"], d["poster_id"], d["poster_at"]), (2, "1:1:5", 3, "1:5:2"))
        self.assertEqual(d["reward"], {"metal": 4000})                       # 10% of its 40,000 metal
        self.assertAlmostEqual(d["expires"], time.time() + B.BOUNTY_TTL, delta=60)
        self.assertEqual(p.memory.social["bounty_out"][str(b["id"])]["reward"], {"metal": 4000})
        (e,) = self.events(p, "bounty_post")
        self.assertEqual((e["bounty"], e["target_id"], e["reward"], e["value"]), (b["id"], 2, {"metal": 4000}, 4000))
        self.assertEqual(len(self.posts("WARNING")) + len(self.posts("REVENGE")), 2)

    def test_a_second_robbery_while_the_bounty_runs_posts_no_second_one(self):
        p, gp = self.poster()
        self.rob(p, gp)
        p.memory.social["robbery_at"] = 0.0
        gp._add_message("combat_report", {
            "target": {"galaxy": 1, "system": 5, "position": 2}, "attacker": {"player_id": 2, "units": {}},
            "defender": {"player_id": 3, "resource_loss": 100}, "loot": {"metal": 21_000}, "debris": {}, "rounds": 3})
        gp.advance(5000)
        p.tick()
        self.assertEqual(len(self.posts("BOUNTY")), 1)

    def test_small_loot_and_a_poor_victim_post_nothing(self):
        p, gp = self.poster()
        self.rob(p, gp, loot=3_000)
        self.assertEqual(self.posts("BOUNTY"), [])
        self.assertEqual(len(self.posts("WARNING")), 1)                       # (the old behaviour is untouched)
        q, gq = self.poster(stock={"metal": 9_000, "crystal": 1_000, "deuterium": 500})
        self.rob(q, gq)
        self.assertEqual(self.posts("BOUNTY"), [])

    def test_without_a_board_nothing_is_posted_or_remembered(self):
        gp = self.game((1, 5, 2), 3, self.P_STOCK)
        gp.others = []
        p = self.agent("agent0003", 3, gp, WATCHER, board=False)
        self.rob(p, gp)
        self.assertEqual(self.board.read(), [])
        self.assertEqual(p.memory.social, S.new_state())
        self.assertIsNone(p._bounty_focus())


class RevengeJoinTests(Story):
    def call_on_board(self, **kw):
        self.calls = getattr(self, "calls", 0) + 1
        data = {"raider_at": "1:1:5", "raider_id": 2, "victim_at": "1:5:2", "victim_id": 3, "loot": 20_000 + self.calls}
        data.update(kw)
        return self.board.post("agent0003", "REVENGE", "Robbed by player 2 from 1:1:5. Who joins me?", data)

    def test_a_raider_joins_and_answers_the_call(self):
        h, gh = self.hunter()
        post = self.call_on_board()
        h.tick()
        r = h.memory.social["revenge"]
        self.assertEqual((r["post"], r["coord"], r["pid"], r["agent"]), (post["id"], "1:1:5", 2, "agent0003"))
        self.assertAlmostEqual(r["until"] - r["joined"], 7200)
        (e,) = self.events(h, "revenge_join")
        self.assertEqual((e["post"], e["victim"], e["raider_at"], e["raider_id"], e["replied"]), (post["id"], "agent0003", "1:1:5", 2, True))
        (reply,) = [p for p in self.posts("REVENGE") if p["agent"] == "agent0001"]
        self.assertEqual((reply["data"]["reply_to"], reply["data"]["raider_at"]), (post["id"], "1:1:5"))
        self.assertIn("agent0003", h.memory.social["friends"])
        self.assertEqual(h._bounty_focus()["coords"][(1, 1, 5)], (B.REVENGE_BONUS, 0.0))

    def test_peaceful_personalities_ignore_it(self):
        for i, persona in enumerate((LISTENER, WATCHER, TRADER)):
            g = self.game((1, 3, 8), 1, {"metal": 5_000, "crystal": 5_000, "deuterium": 500}, ships={"light_fighter": 30})
            a = self.agent(f"agent000{i + 4}", 1, g, persona)
            self.call_on_board()
            a.tick()
            self.assertIsNone(a.memory.social["revenge"])
            self.assertEqual(self.events(a, "revenge_join"), [])
        self.assertEqual([p for p in self.posts("REVENGE") if p["data"].get("reply_to")], [])

    def test_one_at_a_time_then_another_after_two_hours(self):
        for uid in (5, 6):
            self.store.put(f"agent000{uid}", {"email": "x@x", "password": "p", "token": "t", "persona": "raider", "user_id": uid})
        h, gh = self.hunter()
        first = self.call_on_board()
        h.tick()
        second = self.call_on_board(raider_at="1:2:2", raider_id=5, loot=90_000)
        gh.advance(2000)
        h.tick()
        h.tick()
        self.assertEqual(h.memory.social["revenge"]["post"], first["id"])
        self.assertEqual(len(self.events(h, "revenge_join")), 1)
        gh.advance(7300)
        third = self.call_on_board(raider_at="1:2:3", raider_id=6, loot=1_000)
        h.tick()
        self.assertEqual(h.memory.social["revenge"]["post"], third["id"])        # the first is over; the second was read while busy: gone
        self.assertEqual(len(self.events(h, "revenge_join")), 2)

    def test_not_against_a_far_stronger_raider(self):
        h, gh = self.hunter(fighters=2)
        h.memory.opponents[(1, 1, 5)] = {"attacks": 0, "wins": 0, "losses": 0, "net": 0, "strength": 10 ** 8}
        self.call_on_board()
        h.tick()
        self.assertIsNone(h.memory.social["revenge"])
        self.assertEqual(self.events(h, "revenge_join"), [])

    def test_not_against_a_human(self):
        h, gh = self.hunter()
        self.call_on_board(raider_id=7)                                              # not an agent of ours (no account): a human
        h.tick()
        self.assertIsNone(h.memory.social["revenge"])

    def test_not_against_a_friend(self):
        h, gh = self.hunter()
        h.memory.social["rep"]["agent0002"] = {"ok": 3, "cheated": 0, "paid": 0.0, "given": 0.0, "shun_until": 0.0, "last": 0.0}
        self.call_on_board()
        h.tick()
        self.assertIsNone(h.memory.social["revenge"])                              # agent0002 is on the friend list
        self.assertEqual(self.events(h, "revenge_join"), [])

    def test_a_fresh_spy_report_that_shows_a_strong_garrison_vetoes_the_join(self):
        h, gh = self.hunter(fighters=4)
        h.memory.intel[(1, 1, 5)] = {"at": gh.now, "loot": 5000, "defenders": 300, "owner_id": 2, "ships": {"light_fighter": 300},
                                     "defense": {"rocket_launcher": 0}, "research": {}}
        self.call_on_board()
        h.tick()
        self.assertIsNone(h.memory.social["revenge"])

    def test_the_hunter_spies_on_the_named_raider_first(self):
        self.store.put("agent0006", {"email": "n@x", "password": "p", "token": "t", "persona": "miner", "user_id": 6})
        self.call_on_board()
        for seed in range(1, 7):
            h, gh = self.hunter(name=f"agent01{seed:02d}")
            h.rng = random.Random(seed)
            gh.others.append({"user_id": 6, "username": "Near", "coord": (1, 3, 9), "score": 0,
                              "resources": {"metal": 40_000, "crystal": 0, "deuterium": 0}, "ships": {}, "defense": {}})
            h.tick()
            self.assertEqual(self.events(h, "spy")[0]["at"], "1:1:5", seed)             # the nearer neighbour was never preferred

    def test_joining_ends_when_the_raider_has_been_raided(self):
        h, gh = self.hunter()
        self.call_on_board()
        for _ in range(12):
            h.tick()
            gh.advance(100)
            if self.events(h, "revenge_done"):
                break
        (e,) = self.events(h, "revenge_done")
        self.assertEqual(e["raider_at"], "1:1:5")
        self.assertGreater(e["loot"], 0)
        self.assertIsNone(h.memory.social["revenge"])


class BoardOffTests(Story):
    def test_a_hunter_with_a_dead_board_or_none_plays_exactly_the_same(self):
        results = []
        for mode in ("off", "dead", "up-but-empty"):
            h, gh = self.hunter(name=f"agent01{len(results):02d}")
            if mode == "off":
                h.board = None
            elif mode == "dead":
                h.board = BoardClient("http://127.0.0.1:9", h.name, timeout=0.3)
            h.rng = random.Random(7)
            delays = []
            for _ in range(10):
                delays.append(h.tick())
                gh.advance(100)
            results.append((delays, gh.post_log))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])       # nothing on the board: no bounty, no revenge, nothing changes
        self.assertTrue(any(path.endswith("/fleet") and body.get("mission") == "attack" for path, body in results[0][1]))


class WiringTests(unittest.TestCase):
    def test_the_board_knows_the_new_kinds(self):
        import board as BD
        for kind in ("CLAIM", "VERDICT", "BOUNTY"):
            self.assertIn(kind, BD.KINDS)
            self.assertIn(kind, BD.DEFAULT_TTL)
        b = BD.Board(None)
        self.assertEqual(b.post("referee", "verdict", "VERDICT ok", {"ok": True})["kind"], "VERDICT")
        self.assertEqual(b.post("agent0001", "claim", "claim", {"bounty_id": 1})["kind"], "CLAIM")


if __name__ == "__main__":
    unittest.main()
