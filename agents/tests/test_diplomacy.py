import json
import random
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import board as BD
import llm_page
import observatory
import summarize_logs as sl
from swarm import diplomacy as D
from swarm import llm as L
from swarm.agent import Agent, TokenStore
from swarm.defense import Threat
from swarm.military import Memory
from swarm.personas import PRESETS

T0 = 1_000_000.0
QUIET = replace(PRESETS["miner"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, sociability=0.8)
RAIDER = replace(PRESETS["raider"], aggression=1.0, min_loot=100, sociability=0.15)


def row(uid, system=3, score=100, galaxy=1, position=4, **extra):
    return {"user_id": uid, "username": f"user{uid}", "score": score, "rank": uid, "is_me": False,
            "main_planet": {"galaxy": galaxy, "system": system, "position": position}, **extra}


class SharedBoard:
    """The galaxy board as every agent sees it: one list of posts, each agent has its own handle."""
    enabled = True

    def __init__(self, posts=None, agent="x"):
        self.posts = posts if posts is not None else []
        self.agent = agent
        self.clock = T0

    def handle(self, agent):
        b = SharedBoard(self.posts, agent)
        return b

    def post(self, kind, text, data=None, ttl=None):
        p = {"id": 1000 + len(self.posts), "t": self.clock, "agent": self.agent, "kind": kind, "text": text, "data": data or {}}
        self.posts.append(p)
        return p

    def read(self, since=0, kind=None, limit=50):
        kinds = set(kind) if kind else None
        found = [p for p in self.posts if p["id"] > since and (kinds is None or p["kind"] in kinds)][:limit]
        return {"ok": True, "now": self.clock, "last_id": since, "posts": found}


class StubLLM:
    """A model that answers at once, per purpose, from lists (a missing answer = a failure)."""
    enabled = True

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []

    def submit(self, agent, purpose, system, prompt, callback, priority=5, max_wait=None):
        self.calls.append((agent, purpose, priority, prompt))
        queue = self.answers.get(purpose) or []
        callback(queue.pop(0) if queue else None)
        return True

    def asked(self, purpose):
        return [c for c in self.calls if c[1] == purpose]


class World(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = TokenStore(str(self.tmp / "s.json"))
        for i in (1, 2, 3):
            self.store.put(f"agent000{i}", {"user_id": 10 + i, "token": "t"})
        self.board = SharedBoard()
        self.agents = {}

    def agent(self, i=1, persona=QUIET, llm=None):
        name = f"agent000{i}"
        a = Agent(name, persona, "http://x", self.store, random.Random(i), log_dir=str(self.tmp / "logs"), client=object())
        a.user_id = 10 + i
        a.now = T0
        a.board = self.board.handle(name)
        a.llm = llm
        a._diplo_coords = [(1, 3, 8)]
        self.agents[name] = a
        return a

    def events(self, a, kind=None):
        p = a.log_path
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p and p.exists() else []
        return [e for e in rows if kind is None or e["event"] == kind]

    def run_round(self, a, at=None, threats=()):
        if at is not None:
            a.now = at
        planets = [{"id": 1, "coordinates": {"galaxy": 1, "system": 3, "position": 8}}]
        a._plan_diplomacy({}, planets, list(threats))

    def meet(self, a, b_uid, how="spy report"):
        a._diplo_contact(b_uid, how)
        return a.memory.diplomacy["rel"][str(b_uid)]


# ---- the rules -------------------------------------------------------------------------------------------------------------
class ContactTests(World):
    def test_neighbours_within_two_systems_of_one_of_our_planets(self):
        rows = [row(12, system=5), row(13, system=6), row(14, system=3, galaxy=2), row(15, system=1), {"user_id": "x"}, {"broken": True},
                {**row(11, system=3), "is_me": True}]
        got = [r["user_id"] for r in D.neighbours([(1, 3, 8)], rows, 11, None)]
        self.assertEqual(got, [12, 15])                   # 2 systems away yes, 3 no, other galaxy no, ourselves no, junk skipped
        self.assertEqual([r["user_id"] for r in D.neighbours([(1, 3, 8)], rows, 11, {15})], [15])
        self.assertEqual([r["user_id"] for r in D.neighbours([(1, 3, 8), (1, 6, 1)], rows, 11, None)], [12, 13, 15])

    def test_a_spy_report_a_battle_a_board_post_and_a_neighbour_make_contact(self):
        a = self.agent(1)
        a._players = (T0, [row(12), row(13, system=40)], 100)
        self.assertEqual(D.step_of(a.memory.diplomacy, 12), "unknown")
        rel = self.meet(a, 12, "spy report")
        self.assertEqual((rel["step"], rel["how"], rel["name"]), ("contact", "spy report", "agent0002"))
        a._diplo_battle({"role": "attacker", "their_id": 13, "probes_only": False, "loot": 0, "my_loss": 0})
        self.assertEqual(D.step_of(a.memory.diplomacy, 13), "contact")
        self.board.agent = "agent0003"
        self.board.post("OFFER", "x", {})
        a.memory.diplomacy["rel"].pop("13")
        a._diplo_read()
        self.assertEqual(D.step_of(a.memory.diplomacy, 13), "contact")
        self.assertEqual([e["how"] for e in self.events(a, "contact")], ["spy report", "battle", "board post"])
        b = self.agent(2)
        b._players = (T0, [row(11, system=4)], 100)
        self.run_round(b)
        self.assertEqual(b.memory.diplomacy["rel"]["11"]["how"], "neighbour")

    def test_humans_and_ourselves_never_become_contacts(self):
        a = self.agent(1)
        a._diplo_contact(999, "spy report")        # not in agents_state.json: a human
        a._diplo_contact(11, "spy report")         # ourselves
        a._diplo_contact(None, "spy report")
        a._diplo_contact("junk", "spy report")
        self.assertEqual(a.memory.diplomacy["rel"], {})
        self.assertEqual(self.events(a, "contact"), [])

    def test_a_probe_seen_flying_to_us_is_contact_and_counted(self):
        a = self.agent(1)
        a._players = (T0, [row(12, system=5, position=7)], 100)
        spy, attack = Threat(1, "espionage", 60, (1, 5, 7)), Threat(2, "attack", 60, (1, 5, 7))
        self.run_round(a, threats=[spy, spy])
        self.assertEqual(a.memory.diplomacy["rel"]["12"]["spied_us"], 1)       # the same mission is counted once
        self.run_round(a, threats=[attack])
        self.assertEqual(a.memory.diplomacy["rel"]["12"]["spied_us"], 1)


class LadderTests(World):
    def test_a_step_cannot_be_skipped(self):
        rel = D.new_rel(T0)
        rel["trust"] = 100.0
        for step in ("friendly", "pact", "alliance", "contact", "unknown"):
            self.assertIn("skip", D.hard_check(rel, step, T0, QUIET), step)    # from contact only `neutral` is next
        self.assertIsNone(D.hard_check(rel, "neutral", T0, QUIET))
        rel["step"] = "friendly"
        self.assertIn("skip", D.hard_check(rel, "alliance", T0, QUIET))
        self.assertIsNone(D.hard_check(rel, "pact", T0, QUIET))
        self.assertEqual([D.next_step(s) for s in D.STEPS], ["contact", "neutral", "friendly", "pact", "alliance", None])

    def test_trust_needed_for_each_step(self):
        rel = {**D.new_rel(T0), "step": "friendly", "trust": 39.0}
        self.assertIn("below", D.hard_check(rel, "pact", T0, QUIET))
        rel["trust"] = D.PACT_AT
        self.assertIsNone(D.hard_check(rel, "pact", T0, QUIET))
        rel.update(step="pact", trust=D.ALLIANCE_AT - 1)
        self.assertIn("below", D.hard_check(rel, "alliance", T0, QUIET))
        rel["trust"] = D.ALLIANCE_AT
        self.assertIsNone(D.hard_check(rel, "alliance", T0, QUIET))
        self.assertIn("sociable", D.hard_check(rel, "alliance", T0, RAIDER))

    def test_settle_moves_one_step_at_a_time(self):
        rel = {**D.new_rel(T0), "step": "neutral", "trust": 100.0}
        self.assertEqual(D.settle(rel, T0), [("neutral", "friendly")])
        self.assertEqual(D.settle(rel, T0), [])                 # trust alone never buys a pact: that needs a proposal
        rel.update(step="alliance", trust=0.0)
        self.assertEqual(D.settle(rel, T0), [("alliance", "pact")])
        self.assertEqual(D.settle(rel, T0), [("pact", "friendly")])
        self.assertEqual(D.settle(rel, T0), [("friendly", "neutral")])
        self.assertEqual(D.settle(rel, T0), [])

    def test_what_may_be_offered_next(self):
        r = D.new_rel(T0)
        self.assertEqual(D.offer_step(r, False), "neutral")
        r.update(step="neutral", trust=100.0)
        self.assertIsNone(D.offer_step(r, True))                # neutral -> friendly is automatic, not a proposal
        r.update(step="friendly", trust=D.PACT_AT)
        self.assertEqual(D.offer_step(r, False), "pact")
        r.update(step="pact", trust=D.ALLIANCE_AT)
        self.assertIsNone(D.offer_step(r, False))               # an alliance needs a shared threat
        self.assertEqual(D.offer_step(r, True), "alliance")

    def test_a_hello_that_would_skip_a_step_is_ignored(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        post = {"id": 5, "t": T0, "agent": "agent0002", "kind": "HELLO", "data": {"to_id": 11, "step": "pact"}}
        a._diplo_incoming(post, 12, rel, T0)
        self.assertEqual(rel["step"], "contact")
        self.assertEqual(self.board.posts, [])                  # no reply, no change
        self.assertIn(5, a.memory.diplomacy["handled"])


class TrustTests(World):
    def test_peace_pays_a_little_and_is_capped_per_day(self):
        day = 86400.0 * 20
        rel = D.new_rel(day)
        gained = sum(D.peace_tick(rel, day + 3600 * h) for h in range(1, 13))
        self.assertEqual(gained, D.DAY_CAP)                     # 12 quiet hours, but at most DAY_CAP in one day
        self.assertEqual(rel["trust"], D.DAY_CAP)
        later = day + 86400 * 3
        self.assertGreater(D.peace_tick(rel, later), 0)         # a new day pays again

    def test_gains_and_losses_stay_between_0_and_100(self):
        rel = D.new_rel(T0)
        D.bump(rel, 500, T0)
        self.assertEqual(rel["trust"], 100.0)
        D.bump(rel, -500, T0)
        self.assertEqual(rel["trust"], 0.0)

    def test_trade_cheat_and_raid_move_trust(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        a._diplo_trade("agent0002", "done")
        self.assertEqual((rel["trust"], rel["trades"]), (D.GAIN["trade"], 1))
        a._diplo_trade("agent0002", "cheated")
        self.assertEqual(rel["cheats"], 1)
        self.assertEqual(rel["trust"], 0.0)
        rel.update(trust=50.0, step="friendly")
        a._diplo_battle({"role": "defender", "their_id": 12, "probes_only": False, "loot": 4000, "my_loss": 0})
        self.assertEqual((rel["trust"], rel["step"], rel["hit_us"]), (10.0, "neutral", 1))       # -40 and one step down
        self.assertGreater(rel["robbed_at"], 0)
        self.assertLess(rel["opinion"], 0)

    def test_a_probe_shot_down_is_a_spy_not_a_raid(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        a._diplo_battle({"role": "defender", "their_id": 12, "probes_only": True, "loot": 0, "my_loss": 0})
        self.assertEqual((rel["spied_us"], rel["hit_us"]), (1, 0))

    def test_a_betrayal_is_remembered_for_48_hours(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=70.0, opinion=30.0)
        a._diplo_battle({"role": "defender", "their_id": 12, "probes_only": False, "loot": 0, "my_loss": 5000})
        self.assertEqual((rel["step"], rel["trust"], rel["betrayals"]), ("contact", 0.0, 1))
        self.assertLessEqual(rel["opinion"], -60)
        step = self.events(a, "diplo_step")[-1]
        self.assertTrue(step["betrayal"])
        self.assertEqual((step["from"], step["to"]), ("pact", "contact"))
        rel["trust"] = 100.0
        rel["last_hit_at"] = 0.0                                # even without the 12 h grudge ...
        rel["robbed_at"] = 0.0
        a.now = T0 + D.BETRAY_COOL - 10
        self.assertIn("betrayed", a._diplo_hard(12, rel, "neutral"))
        a.now = T0 + D.BETRAY_COOL + 10
        self.assertIsNone(a._diplo_hard(12, rel, "neutral"))    # ... and forgotten after 48 hours
        self.assertEqual(D.peace_tick({**D.new_rel(T0), "betrayed_at": T0}, T0 + 7200), 0.0)    # no peace bonus while the betrayal is fresh

    def test_trust_log_is_quiet_for_small_changes(self):
        rel = D.new_rel(T0)
        self.assertTrue(D.loggable(rel, 8.0, T0))
        self.assertFalse(D.loggable(rel, 1.0, T0 + 60))
        self.assertTrue(D.loggable(rel, 1.0, T0 + D.LOG_EVERY + 1))
        self.assertFalse(D.loggable(rel, 0.0, T0 + 10 * D.LOG_EVERY))


class HardRuleTests(World):
    def test_an_attacker_is_never_accepted(self):
        rel = {**D.new_rel(T0), "last_hit_at": T0 - 3600, "trust": 90.0}
        self.assertIn("attacked", D.hard_check(rel, "neutral", T0, QUIET))
        self.assertIsNone(D.hard_check({**rel, "last_hit_at": T0 - D.HIT_GRUDGE - 1}, "neutral", T0, QUIET))
        self.assertIn("attacked", D.hard_check({**rel, "last_hit_at": 0.0}, "neutral", T0, QUIET, aggressor_recent=True))
        self.assertIn("robbed", D.hard_check({**rel, "last_hit_at": 0.0, "robbed_at": T0 - 3 * 86400 + 1000}, "neutral", T0 + 1, QUIET) or "robbed")
        self.assertIn("revenge", D.hard_check({**rel, "last_hit_at": 0.0}, "neutral", T0, QUIET, revenge_open=True))

    def test_the_agents_own_memory_feeds_the_rules(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        rel["coord"] = "1:5:7"
        self.assertIsNone(a._diplo_hard(12, rel, "neutral"))
        a.memory.aggressors[(1, 5, 7)] = T0 - 3600                          # one of their planets attacked us an hour ago
        self.assertIn("attacked", a._diplo_hard(12, rel, "neutral"))
        a.memory.aggressors.clear()
        a.memory.opponents[(1, 5, 7)] = {"robbed_at": T0 - 3600}           # social_agent notes who robbed us there
        self.assertIn("robbed", a._diplo_hard(12, rel, "neutral"))
        a.memory.opponents.clear()
        rel["robbed_at"] = 0.0
        a.memory.social["revenge"] = {"post": 1, "agent": "x", "coord": "1:5:7", "pid": 12, "joined": T0, "until": T0 + 3600}
        self.assertIn("revenge", a._diplo_hard(12, rel, "neutral"))
        a.memory.social["revenge"] = None
        a.memory.social["bounty_out"]["9"] = {"id": 9, "pid": 12, "target_at": "1:5:7", "expires": time.time() + 3600}
        self.assertIn("revenge", a._diplo_hard(12, rel, "neutral"))

    def test_the_model_cannot_override_a_hard_rule(self):
        llm = StubLLM(answer=["ACCEPT\nSure, friends!"])
        a = self.agent(1, llm=llm)
        rel = self.meet(a, 12)
        a.memory.aggressors[(1, 5, 7)] = T0 - 60
        rel["coord"] = "1:5:7"
        post = {"id": 7, "t": T0, "agent": "agent0002", "kind": "HELLO", "data": {"to_id": 11, "step": "neutral"}}
        a._diplo_incoming(post, 12, rel, T0)
        self.assertEqual(llm.asked("answer"), [])                             # refused in code: the model is not even asked
        self.assertEqual(rel["step"], "contact")
        reply = self.board.posts[-1]
        self.assertEqual((reply["kind"], reply["data"]["decision"]), ("REPLY", "REFUSE"))
        # an ACCEPT that arrives after the situation changed is also turned into a REFUSE
        a2 = self.agent(3)
        rel2 = self.meet(a2, 12)
        a2.memory.aggressors[(1, 5, 7)] = T0 - 60
        rel2["coord"] = "1:5:7"
        a2._diplo_finish(8, 12, "neutral", "ACCEPT", "fine", "llm", 0)
        self.assertEqual(rel2["step"], "contact")
        self.assertEqual(self.events(a2, "diplo_answer")[-1]["decision"], "REFUSE")
        self.assertEqual(self.events(a2, "diplo_answer")[-1]["source"], "hard rule")

    def test_a_refusal_means_six_hours_of_silence(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        self.assertEqual(a._diplo_pick()[2], "neutral")
        a._diplo_finish(3, 12, "neutral", "REFUSE", "no", "rule", 0)
        self.assertEqual(rel["cool_until"], T0 + D.REFUSAL_COOL)
        self.assertIsNone(a._diplo_pick())
        a.now = T0 + D.REFUSAL_COOL + 1
        self.assertEqual(a._diplo_pick()[2], "neutral")


class ImpressionTests(unittest.TestCase):
    def test_stronger_weaker_and_peer(self):
        rows = [row(2, score=5000), row(3, score=1000), row(4, score=100)]
        strong = D.first_impression(QUIET, 1000, rows[0], rows)
        peer = D.first_impression(QUIET, 1000, rows[1], rows)
        weak = D.first_impression(QUIET, 1000, rows[2], rows)
        self.assertEqual((strong["role"], peer["role"], weak["role"]), ("stronger", "peer", "weaker"))
        self.assertGreater(strong["opinion"], peer["opinion"])
        self.assertGreater(peer["opinion"], weak["opinion"])
        self.assertEqual((strong["rank"], strong["my_rank"]), (2, 2))

    def test_personality_colours_the_first_impression(self):
        rows = [row(2, score=5000), row(4, score=100)]
        cautious = replace(QUIET, caution=0.9, aggression=0.0)
        bold = replace(QUIET, caution=0.1, aggression=0.9, sociability=0.1)
        self.assertGreater(D.first_impression(cautious, 1000, rows[0], rows)["opinion"], D.first_impression(bold, 1000, rows[0], rows)["opinion"])
        self.assertLess(D.first_impression(bold, 1000, rows[1], rows)["opinion"], -10)       # a raider sees a nuisance or a target
        self.assertGreater(D.first_impression(cautious, 1000, rows[1], rows)["opinion"], -10)

    def test_missing_data_is_tolerated(self):
        self.assertIsNone(D.first_impression(QUIET, 1000, None))
        self.assertIsNone(D.first_impression(QUIET, 1000, "junk"))
        no_score = {"user_id": 2}
        imp = D.first_impression(QUIET, 1000, no_score)
        self.assertEqual(imp["role"], "unknown")
        self.assertEqual(D.first_impression(QUIET, None, row(2))["role"], "unknown")
        self.assertEqual(D.first_impression(QUIET, 0, row(2, score=0))["role"], "peer")       # day one: everybody has ~0 points
        self.assertEqual(D.categories(None), {})
        self.assertEqual(D.categories({"economy": "x", "military_score": 5, "scores": "junk"}), {"military": 5.0})

    def test_category_scores_are_used_when_the_api_has_them(self):
        r = row(2, score=1000, economy_score=100, military=900, total_ships=40)
        self.assertEqual(D.categories(r), {"economy": 100.0, "military": 900.0, "ships": 40.0})
        wary = D.first_impression(replace(QUIET, caution=1.0), 1000, r)
        plain = D.first_impression(replace(QUIET, caution=1.0), 1000, row(2, score=1000))
        self.assertLess(wary["opinion"], plain["opinion"])

    def test_the_cached_ranking_sets_the_opinion_at_contact(self):
        w = World()
        w.setUp()
        a = w.agent(1)
        a._players = (T0, [row(12, score=9000), {**row(11), "is_me": True, "score": 1000}], 1000)
        rel = w.meet(a, 12)
        self.assertEqual((rel["role"], rel["imp"], rel["coord"]), ("stronger", True, "1:3:4"))
        b = w.agent(2)                          # no ranking cached yet: the impression is filled in at the next scan
        rel = w.meet(b, 11)
        self.assertFalse(rel["imp"])
        b._players = (T0, [row(11, score=10)], 1000)
        b.memory.diplomacy["scan_at"] = 0.0
        w.run_round(b)
        self.assertTrue(b.memory.diplomacy["rel"]["11"]["imp"])


class ParsingTests(unittest.TestCase):
    def test_decisions(self):
        cases = {"ACCEPT": ("ACCEPT", ""), "accept": ("ACCEPT", ""), "Refuse.": ("REFUSE", ""), "WAIT": ("WAIT", ""),
                 "ACCEPT\nThey traded fairly.": ("ACCEPT", "They traded fairly"), "refuse: they robbed me": ("REFUSE", "they robbed me"),
                 "**ACCEPT** - good deal": ("ACCEPT", "good deal"), "  \"Wait\"\n\nlet me think": ("WAIT", "let me think"),
                 "ACCEPTED": ("WAIT", ""), "I think we should accept": ("WAIT", ""), "": ("WAIT", ""), "   ": ("WAIT", ""),
                 "???": ("WAIT", ""), "maybe": ("WAIT", ""), None: ("WAIT", ""), 42: ("WAIT", ""), "Acceptable": ("WAIT", "")}
        for text, want in cases.items():
            self.assertEqual(D.parse_decision(text), want, repr(text))

    def test_reason_is_short_and_clean(self):
        decision, reason = D.parse_decision("REFUSE\n" + "x" * 500)
        self.assertEqual(len(reason), 120)
        reason = D.parse_decision("ACCEPT <b>yes</b> [link](http://evil)")[1]
        for bad in ("<", ">", "[", "]", "://"):
            self.assertNotIn(bad, reason)

    def test_prompts_carry_the_facts(self):
        rel = {**D.new_rel(T0), "trust": 45.0, "opinion": 12.0, "hit_us": 0, "spied_us": 3, "trades": 2}
        f = D.build_facts(rel, "pact", T0, standing_={"ratio": 1.4, "role": "peer", "rank": 4, "my_rank": 7}, threats=1, proposer="agent0002", answerer="agent0001")
        text = D.answer_prompt(f)
        for needle in ("TASK: answer", "FROM: agent0002", "TO: agent0001", "TRUST: 45", "OPINION: 12", "SPIED_ON_US: 3", "ATTACKED_US: 0", "2 completed", "pact"):
            self.assertIn(needle, text)
        self.assertIn("trust=45", D.facts_digest(f))


class FakeModelTests(unittest.TestCase):
    def test_the_canned_decision_is_repeatable(self):
        sheet = {"name": "Ada Vance", "handle": "agent0001", "backstory": "b", "traits": ["peaceful"]}
        f = D.build_facts({**D.new_rel(T0), "trust": 90.0}, "pact", T0, proposer="agent0002", answerer="agent0001")
        prompt = D.answer_prompt(f)
        first = L.fake_answer(D.answer_system(sheet), prompt)
        for _ in range(3):
            self.assertEqual(L.fake_answer(D.answer_system(sheet), prompt), first)
        self.assertEqual(D.parse_decision(first)[0], "ACCEPT")
        low = D.answer_prompt({**f, "trust": 0})
        self.assertIn(D.parse_decision(L.fake_answer(D.answer_system(sheet), low))[0], ("REFUSE", "WAIT"))
        self.assertNotEqual(first, L.fake_answer(D.answer_system(sheet), low))
        # the names matter (a hash), so different pairs can differ at the borderline
        outcomes = {D.fake_decision(f"agent{i:04d}", "agent0001", 35).split("\n")[0] for i in range(40)}
        self.assertGreater(len(outcomes), 1)
        self.assertEqual(D.fake_decision("a", "b", 35), D.fake_decision("a", "b", 35))

    def test_hello_and_announce_come_back_in_the_agents_words_with_names_intact(self):
        sheet = {"name": "Ada Vance", "handle": "agent0001", "backstory": "b", "traits": ["peaceful"]}
        from swarm import voice as V
        template = D.hello_template("agent0001", "agent0002", "neutral")
        out = L.fake_answer(V.system_prompt(sheet), D.hello_prompt(template, "neutral"))
        self.assertEqual(V.check(out, template), out)
        self.assertIn("agent0002", out)
        t2 = D.announce_template("agent0001", "agent0002", "pact")
        out2 = L.fake_answer(V.system_prompt(sheet), D.announce_prompt(t2))
        self.assertEqual(V.check(out2, t2), out2)


# ---- the whole conversation ----------------------------------------------------------------------------------------------------------
class ConversationTests(World):
    def converse(self, a, b, rounds=4, step=130.0):
        for i in range(rounds):
            for x in (a, b):
                self.run_round(x, at=x.now + step)

    def test_without_a_model_a_rule_answers_and_both_become_neutral(self):
        a, b = self.agent(1), self.agent(2)
        self.meet(a, 12)
        self.meet(b, 11)
        self.run_round(a)
        hello = [p for p in self.board.posts if p["kind"] == "HELLO"]
        self.assertEqual(len(hello), 1)
        self.assertEqual(hello[0]["data"]["to_id"], 12)
        self.assertEqual((hello[0]["data"]["step"], hello[0]["data"]["from_id"]), ("neutral", 11))
        self.assertEqual(a.memory.diplomacy["open"]["to"], 12)
        self.converse(a, b)
        self.assertEqual(D.step_of(a.memory.diplomacy, 12), "neutral")
        self.assertEqual(D.step_of(b.memory.diplomacy, 11), "neutral")
        self.assertIsNone(a.memory.diplomacy["open"])
        replies = [p for p in self.board.posts if p["kind"] == "REPLY"]
        self.assertEqual((len(replies), replies[0]["data"]["decision"], replies[0]["data"]["reply_to"]), (1, "ACCEPT", hello[0]["id"]))
        answer = self.events(b, "diplo_answer")[0]
        self.assertEqual((answer["decision"], answer["source"], answer["side"]), ("ACCEPT", "rule", "answerer"))
        self.assertEqual(self.events(a, "diplo_answer")[0]["side"], "proposer")
        self.assertTrue(self.events(a, "diplo_hello"))
        steps = self.events(a, "diplo_step") + self.events(b, "diplo_step")
        self.assertEqual({(e["from"], e["to"]) for e in steps}, {("contact", "neutral")})

    def test_one_proposal_open_at_a_time_and_one_per_three_hours(self):
        a = self.agent(1)
        self.meet(a, 12)
        self.meet(a, 13)
        self.run_round(a)
        self.run_round(a, at=T0 + 600)
        self.assertEqual(len([p for p in self.board.posts if p["kind"] == "HELLO"]), 1)     # the first one is still open
        a.memory.diplomacy["open"] = None                                                   # answered, but the 3 hours are not over
        self.run_round(a, at=T0 + 1200)
        self.assertEqual(len([p for p in self.board.posts if p["kind"] == "HELLO"]), 1)
        self.run_round(a, at=T0 + 4 * 3600)
        self.assertEqual(len([p for p in self.board.posts if p["kind"] == "HELLO"]), 2)

    def test_an_unanswered_proposal_times_out_into_a_cool_off(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        self.run_round(a)
        self.assertIsNotNone(a.memory.diplomacy["open"])
        a.memory.diplomacy["scan_at"] = 0.0
        self.run_round(a, at=T0 + D.OPEN_TIMEOUT + 10)
        self.assertIsNone(a.memory.diplomacy["open"])
        self.assertGreater(rel["cool_until"], a.now)
        self.assertEqual(self.events(a, "diplo_answer")[-1]["decision"], "TIMEOUT")

    def test_a_pact_removes_the_partner_from_raiding(self):
        a, b = self.agent(1, persona=RAIDER), self.agent(2)
        ra = self.meet(a, 12)
        rb = self.meet(b, 11)
        for rel in (ra, rb):
            rel.update(step="friendly", trust=60.0, opinion=20.0)
        ra["coord"] = "1:5:7"
        a.memory.intel[(1, 5, 7)] = {"at": T0, "loot": 5000, "defenders": 1, "owner_id": 12, "ships": {}, "defense": {}}
        a.memory.intel[(1, 6, 6)] = {"at": T0, "loot": 5000, "defenders": 1, "owner_id": 13, "ships": {}, "defense": {}}
        a._players = (T0, [row(12, system=5, position=7), row(13), row(14)], 100)
        self.assertEqual(a._without_allies({12, 13, 14}), {12, 13, 14})
        self.run_round(a)                                     # a proposes the pact
        self.assertEqual(self.board.posts[-1]["data"]["step"], "pact")
        self.converse(a, b)
        self.assertEqual((ra["step"], rb["step"]), ("pact", "pact"))
        self.assertEqual(a._without_allies({12, 13, 14}), {13, 14})
        self.assertEqual(a._without_allies(None), {13, 14})
        self.assertNotIn((1, 5, 7), a.memory.intel)           # the spy report on the partner is forgotten
        self.assertIn((1, 6, 6), a.memory.intel)
        self.assertTrue(a._diplo_protected((1, 5, 7), None))
        self.assertTrue(a._diplo_protected(None, 12))
        self.assertFalse(a._diplo_protected((1, 6, 6), 13))
        # a gamble or a stale report can still put the partner in front of the attack code: it is stopped there
        del a.memory.intel[(1, 6, 6)]
        a.memory.intel[(1, 5, 7)] = {"at": a.now, "loot": 5000, "defenders": 1, "owner_id": 12, "ships": {}, "defense": {}}
        self.assertFalse(a._try_attack({"id": 1}, a.now, {"light_fighter": 500}))
        self.assertNotIn((1, 5, 7), a.memory.intel)
        self.assertEqual(self.events(a, "diplo_blocked")[0]["at"], "1:5:7")
        # the announcement went on the board
        ann = [p for p in self.board.posts if p["kind"] == "ALLIANCE"]
        self.assertEqual(ann[0]["data"]["announce"], "pact")
        self.assertIn("pact", ann[0]["text"])

    def test_breaking_the_pact_ends_it_and_the_partner_may_be_hit_again_only_after_a_betrayal(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=80.0)
        self.assertEqual(a._diplo_peace_ids(), {12})
        a._diplo_battle({"role": "defender", "their_id": 12, "probes_only": False, "loot": 0, "my_loss": 3000})
        self.assertEqual(a._diplo_peace_ids(), set())
        self.assertFalse(a._diplo_protected((1, 5, 7), 12))

    def test_alliance_partner_is_preferred_when_joining_an_alliance(self):
        a = self.agent(1)
        rel = self.meet(a, 12)
        rel.update(step="alliance")
        self.assertEqual(a._diplo_alliance_ids(), {12})
        self.assertEqual(a._diplo_peace_ids(), {12})
        rel["step"] = "pact"
        self.assertEqual(a._diplo_alliance_ids(), set())

    def test_the_model_decides_but_only_after_the_rules(self):
        llm = StubLLM(answer=["ACCEPT\nThey look honest."])
        a, b = self.agent(1, llm=StubLLM()), self.agent(2, llm=llm)
        self.meet(a, 12)
        self.meet(b, 11)
        self.run_round(a)
        self.run_round(a, at=a.now + 130)    # a's hello: the model fails -> plain template (posted on the round after it was asked)
        self.assertEqual(self.board.posts[0]["text"], D.hello_template("agent0001", "agent0002", "neutral"))
        self.assertEqual(a.llm.calls[0][1:3], ("hello", 5))          # NORMAL priority
        self.run_round(b)                    # b reads it; the model is asked (HIGH) and says ACCEPT
        self.assertEqual(self.events(b, "diplo_answer"), [])          # the answer is picked up on a later round: nothing waits for the model
        self.assertEqual(llm.asked("answer")[0][1:3], ("answer", 0))
        self.assertIn("TRUST: 0", llm.asked("answer")[0][3])
        self.run_round(b, at=b.now + 130)
        self.assertEqual(self.events(b, "diplo_answer")[0]["source"], "llm")
        self.assertEqual(self.events(b, "diplo_answer")[0]["reason"], "They look honest")
        self.assertEqual(D.step_of(b.memory.diplomacy, 11), "neutral")

    def test_garbage_is_wait_then_a_refusal_after_the_retries(self):
        llm = StubLLM(answer=["I would love to!", "hmm", "no idea"])
        b = self.agent(2, llm=llm)
        rel = self.meet(b, 11)
        self.board.agent = "agent0001"
        self.board.post("HELLO", "hi", {"to_id": 12, "step": "neutral"})
        self.run_round(b)
        self.run_round(b, at=T0 + 10)
        self.assertEqual(self.events(b, "diplo_answer")[-1]["decision"], "WAIT")
        self.assertEqual(len([p for p in self.board.posts if p["kind"] == "REPLY"]), 0)     # waiting is silent
        pid = list(b.memory.diplomacy["later"])[0]
        self.assertEqual(rel["step"], "contact")
        self.run_round(b, at=T0 + 100)
        self.assertEqual(len(llm.asked("answer")), 1)                                       # not asked again yet
        for i in range(1, 9):                          # each retry takes two rounds: ask, then pick up the answer
            self.run_round(b, at=T0 + i * (D.LATER_RETRY + 2))
        self.assertEqual(len(llm.asked("answer")), 3)
        self.assertEqual(b.memory.diplomacy["later"], {})
        self.assertGreater(rel["cool_until"], b.now)                                        # undecided for too long: a refusal
        self.assertIn(int(pid), b.memory.diplomacy["handled"])
        self.assertEqual(rel["step"], "contact")

    def test_a_model_that_is_down_falls_back_to_the_rule(self):
        b = self.agent(2, llm=StubLLM())
        rel = self.meet(b, 11)
        self.board.agent = "agent0001"
        self.board.post("HELLO", "hi", {"to_id": 12, "step": "neutral"})
        self.run_round(b)
        self.run_round(b, at=T0 + 10)
        self.assertEqual(rel["step"], "neutral")
        self.assertEqual(self.events(b, "diplo_answer")[0]["source"], "rule (no model answer)")

    def test_the_rule_refuses_someone_it_does_not_think_much_of(self):
        b = self.agent(2)
        rel = self.meet(b, 11)
        rel["opinion"] = -40.0
        self.board.agent = "agent0001"
        self.board.post("HELLO", "hi", {"to_id": 12, "step": "neutral"})
        self.run_round(b)
        self.assertEqual(rel["step"], "contact")
        self.assertEqual([p["data"]["decision"] for p in self.board.posts if p["kind"] == "REPLY"], ["REFUSE"])
        self.assertGreater(rel["cool_until"], b.now)

    def test_a_reply_that_is_not_for_us_or_not_from_them_is_ignored(self):
        a = self.agent(1)
        self.meet(a, 12)
        self.meet(a, 13)
        self.run_round(a)
        hello = self.board.posts[0]
        self.board.agent = "agent0003"                         # somebody else answers our hello
        self.board.post("REPLY", "ok", {"reply_to": hello["id"], "decision": "ACCEPT", "from": 13, "to_id": 11, "step": "neutral"})
        self.board.agent = "agent0002"
        self.board.post("REPLY", "ok", {"reply_to": 99999, "decision": "ACCEPT", "from": 12, "to_id": 11, "step": "neutral"})
        a.memory.diplomacy["read_at"] = 0.0
        self.run_round(a, at=T0 + 200)
        self.assertEqual(D.step_of(a.memory.diplomacy, 12), "contact")
        self.assertIsNotNone(a.memory.diplomacy["open"])

    def test_no_board_means_no_proposals_but_contact_still_works(self):
        a = self.agent(1)
        a.board = None
        self.meet(a, 12)
        self.run_round(a)
        self.assertEqual(self.board.posts, [])
        self.assertIsNone(a.memory.diplomacy["open"])

    def test_an_exception_in_diplomacy_never_stops_the_round(self):
        class Broken(SharedBoard):
            def read(self, *a, **k):
                raise RuntimeError("boom")
        a = self.agent(1)
        a.board = Broken()
        self.meet(a, 12)
        self.run_round(a)                                       # must not raise
        self.assertTrue(self.events(a, "diplo_error"))
        a._diplo_battle({"role": "defender", "their_id": "junk"})         # hooks are safe on junk, too
        a._diplo_trade(None, "done")
        self.assertFalse(a._diplo_protected("not a coord", "x"))


# ---- saving, the board, the reports ---------------------------------------------------------------------------------------------------
class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        m = Memory()
        D.add_contact(m.diplomacy, 12, T0, "battle", "agent0002")
        m.diplomacy["rel"]["12"].update(step="pact", trust=55.5, betrayed_at=T0)
        m.diplomacy["open"] = {"to": 12, "to_name": "agent0002", "step": "alliance", "post": 5, "at": T0}
        back = Memory.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.diplomacy["rel"]["12"]["step"], "pact")
        self.assertEqual(back.diplomacy["rel"]["12"]["trust"], 55.5)
        self.assertEqual(back.diplomacy["open"]["step"], "alliance")

    def test_an_old_file_without_the_field_gets_a_blank_state(self):
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).diplomacy, D.new_state())
        self.assertNotIn("diplomacy", {"risk_scale": 1})
        self.assertIn("diplomacy", Memory().to_dict())

    def test_garbage_is_cleaned(self):
        for junk in ("junk", 5, [1], {"rel": "x"}, {"rel": {"12": "x", "zz": {}, "13": {"step": "king", "trust": "lots", "opinion": 500, "hit_us": -3}}},
                     {"open": {"to": "x"}}, {"handled": ["a", 3.5, 4], "later": {"1": {"a": [1]}, "x": {}}, "cursor": "7", "read_at": float("nan")}):
            st = D.clean_state(junk)
            self.assertEqual(set(st), set(D.new_state()))
            self.assertIsInstance(Memory.from_dict({"diplomacy": junk}).diplomacy, dict)
        st = D.clean_state({"rel": {"13": {"step": "king", "trust": "lots", "opinion": 500, "hit_us": -3, "ratio": True}}, "handled": ["a", 4]})
        rel = st["rel"]["13"]
        self.assertEqual((rel["step"], rel["trust"], rel["opinion"], rel["hit_us"], rel["ratio"]), ("contact", 0.0, 100.0, 0, None))
        self.assertEqual(st["handled"], [4])
        self.assertEqual(D.clean_state({"rel": {"12": {"step": "unknown"}}})["rel"]["12"]["step"], "contact")


class BoardKindTests(unittest.TestCase):
    def test_the_board_accepts_hello_and_reply(self):
        b = BD.Board(None)
        hello = b.post("agent0001", "hello", "Greetings", {"to_id": 12, "step": "neutral"})
        reply = b.post("agent0002", "REPLY", "Agreed.", {"reply_to": hello["id"], "decision": "ACCEPT", "from": 12})
        self.assertEqual((hello["kind"], reply["kind"]), ("HELLO", "REPLY"))
        self.assertEqual(hello["expires"] - hello["t"], 3 * 3600)
        self.assertEqual(reply["expires"] - reply["t"], 6 * 3600)
        self.assertEqual([p["kind"] for p in b.read(kinds=["HELLO", "REPLY"])], ["HELLO", "REPLY"])
        with self.assertRaises(BD.PostRefused):
            b.post("agent0001", "NONSENSE", "x")

    def test_pages_know_the_new_kinds(self):
        import board_page
        for page in (board_page.PAGE, observatory.PAGE):
            self.assertIn('HELLO:"var(--hello)"', page)
            self.assertIn('REPLY:"var(--reply)"', page)
        self.assertIn('id="diplo"', observatory.PAGE)


class ReportTests(unittest.TestCase):
    EVENTS = [
        {"t": 1000, "agent": "agent0001", "event": "contact", "with": "agent0002", "uid": 12, "how": "neighbour"},
        {"t": 1001, "agent": "agent0002", "event": "contact", "with": "agent0001", "uid": 11, "how": "board post"},
        {"t": 1002, "agent": "agent0001", "event": "diplo_hello", "with": "agent0002", "step": "neutral", "text": "hi"},
        {"t": 1003, "agent": "agent0002", "event": "diplo_answer", "side": "answerer", "decision": "ACCEPT", "source": "llm", "with": "agent0001", "raw": "ACCEPT"},
        {"t": 1004, "agent": "agent0003", "event": "diplo_answer", "side": "answerer", "decision": "REFUSE", "source": "hard rule", "with": "agent0001"},
        {"t": 1005, "agent": "agent0003", "event": "diplo_answer", "side": "answerer", "decision": "WAIT", "source": "llm", "with": "agent0001"},
        {"t": 1006, "agent": "agent0001", "event": "diplo_step", "with": "agent0002", "from": "contact", "to": "neutral", "trust": 6},
        {"t": 1007, "agent": "agent0001", "event": "diplo_step", "with": "agent0002", "from": "friendly", "to": "pact", "trust": 50},
        {"t": 1008, "agent": "agent0001", "event": "diplo_trust", "with": "agent0002", "delta": 8, "why": "completed trade", "trust": 58, "step": "pact"},
        {"t": 1009, "agent": "agent0003", "event": "diplo_step", "with": "agent0001", "from": "pact", "to": "contact", "trust": 0, "betrayal": True},
        {"t": 1010, "agent": "agent0001", "event": "diplo_blocked", "at": "1:5:7"},
    ]

    def test_summary_section(self):
        text = "\n".join(sl.diplomacy_section(self.EVENTS))
        for needle in ("DIPLOMACY", "contacts: 2", "1 neighbour", "proposals posted: 1", "1 accepted, 1 refused, 1 waited", "by llm", "by hard",
                       "steps reached:", "betrayals: 1", "agent0003 was attacked by agent0001 under a pact", "raids stopped", "strongest relationships",
                       "agent0001 -> agent0002: pact, trust 58"):
            self.assertIn(needle, text)
        full = sl.summarize([{"t": 1000, "agent": "a", "event": "wait"}, *self.EVENTS])
        self.assertIn("DIPLOMACY", full)

    def test_top_five_by_trust_and_empty(self):
        events = [{"t": i, "agent": "a", "event": "diplo_trust", "with": f"p{i}", "trust": i * 10, "step": "friendly"} for i in range(1, 9)]
        top = "\n".join(sl.diplomacy_section(events)).split("strongest relationships (by trust):")[1]
        self.assertEqual(top.count(" -> "), 5)
        self.assertIn("a -> p8", top)
        self.assertNotIn("a -> p3", top)
        self.assertIn("none", "\n".join(sl.diplomacy_section([{"t": 1, "agent": "a", "event": "wait"}])))

    def test_relations_from_events_survive_junk(self):
        rows = D.relations_from_events(self.EVENTS + [{"event": "diplo_step"}, {"event": "contact", "agent": "z"}, "junk", None, {"event": "diplo_trust", "agent": "q", "with": 5, "trust": "x", "step": "king"}])
        pair = next(r for r in rows if (r["agent"], r["with"]) == ("agent0001", "agent0002"))
        self.assertEqual((pair["step"], pair["trust"]), ("pact", 58.0))
        self.assertEqual(rows[0], pair)                         # highest trust first

    def test_observatory_view_is_fail_soft(self):
        view = observatory.diplomacy_view(self.EVENTS)
        self.assertEqual(view["rows"][0]["step"], "pact")
        self.assertEqual(observatory.diplomacy_view([object()]), {"rows": []})
        self.assertIn("met agent0002", observatory.describe(self.EVENTS[0]))
        self.assertIn("pact", observatory.describe(self.EVENTS[7]))


class LlmLogTests(unittest.TestCase):
    def test_diplomacy_calls_are_marked_used_and_the_page_names_them(self):
        folder = Path(tempfile.mkdtemp())
        calls = [{"t": 1, "kind": "llm_call", "agent": "agent0001", "purpose": "hello", "prompt": "p", "answer": "Hi agent0002.", "ok": True},
                 {"t": 2, "kind": "llm_call", "agent": "agent0002", "purpose": "answer", "prompt": "p", "answer": "ACCEPT\nFine.", "ok": True},
                 {"t": 3, "kind": "llm_call", "agent": "agent0002", "purpose": "announce", "prompt": "p", "answer": "We are allies.", "ok": True},
                 {"t": 4, "kind": "llm_call", "agent": "agent0002", "purpose": "answer", "prompt": "p", "answer": "WAIT", "ok": True}]
        (folder / "llm.jsonl").write_text("\n".join(json.dumps(c) for c in calls))
        (folder / "agent0001.jsonl").write_text(json.dumps({"event": "diplo_hello", "agent": "agent0001", "text": "Hi agent0002."}))
        (folder / "agent0002.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"event": "diplo_answer", "agent": "agent0002", "raw": "ACCEPT\nFine.", "decision": "ACCEPT"},
            {"event": "diplo_announce", "agent": "agent0002", "text": "We are allies."}]))
        view = llm_page.llm_view(folder)
        used = {c["purpose"] + c["answer"][:3]: c["used"] for c in view["calls"]}
        self.assertEqual(used, {"helloHi ": True, "answerACC": True, "announceWe ": True, "answerWAI": False})
        for word in ("hello (diplomacy)", "answer (diplomacy)", "announcement (diplomacy)"):
            self.assertIn(word, llm_page.PAGE)


class RealFlowTests(World):
    """Two agents, the fake model of --llm fake, a real worker thread."""

    def test_fake_model_end_to_end(self):
        client = L.make_llm("fake", log_dir=str(self.tmp / "logs"))
        self.addCleanup(client.close)
        a, b = self.agent(1, llm=client), self.agent(2, llm=client)
        ra, rb = self.meet(a, 12), self.meet(b, 11)
        for rel in (ra, rb):
            rel.update(step="friendly", trust=90.0, opinion=30.0)
        self.run_round(a)                                          # the greeting is being written by the model
        end = time.time() + 10
        while time.time() < end and D.step_of(b.memory.diplomacy, 11) != "pact":
            time.sleep(0.05)
            for x in (a, b):
                self.run_round(x, at=x.now + 130)
        self.assertEqual(D.step_of(b.memory.diplomacy, 11), "pact")
        while time.time() < end and D.step_of(a.memory.diplomacy, 12) != "pact":
            time.sleep(0.05)
            for x in (a, b):
                self.run_round(x, at=x.now + 130)
        self.assertEqual(D.step_of(a.memory.diplomacy, 12), "pact")
        calls = [json.loads(x) for x in (self.tmp / "logs" / "llm.jsonl").read_text().splitlines()]
        purposes = {c["purpose"] for c in calls}
        self.assertTrue({"hello", "answer"} <= purposes)
        self.assertEqual(next(c for c in calls if c["purpose"] == "answer")["priority"], L.HIGH)
        self.assertEqual(next(c for c in calls if c["purpose"] == "hello")["priority"], L.NORMAL)
        self.assertEqual(self.events(b, "diplo_answer")[0]["source"], "llm")


if __name__ == "__main__":
    unittest.main()
