import itertools
import random
import re
import tempfile
import unittest
from pathlib import Path

import summarize_logs as sl
from swarm import alliance as A
from swarm import llm as L
from swarm.personas import PRESETS, Persona
from tests import test_alliance as TA

PURPOSES = tuple(A.PURPOSE_WORDS)


class Held:
    """A model that keeps every request until the test answers it by hand."""
    enabled = True

    def __init__(self, auto=None):
        self.calls, self.waiting, self.auto = [], [], auto

    def submit(self, agent, purpose, system, prompt, callback, priority=5, max_wait=None):
        self.calls.append((agent, purpose, priority, prompt))
        if self.auto is not None:
            callback(self.auto(purpose) if callable(self.auto) else self.auto)
        else:
            self.waiting.append(callback)
        return True

    def answer(self, text):
        self.waiting.pop(0)(text)

    def asked(self, purpose):
        return [c for c in self.calls if c[1] == purpose]


class AdBoard(TA.Board):
    def read(self, since=0, kind=None, limit=50):
        posts = [{"id": i + 1, "kind": k, "text": t, "data": d, "agent": "x", "t": 0} for i, (k, t, d) in enumerate(self.posts)
                 if not kind or k in kind]
        return {"ok": True, "now": 0.0, "last_id": len(posts), "posts": posts}


class NameRuleTests(unittest.TestCase):
    def test_enough_tones_and_every_cell_has_material(self):
        self.assertGreaterEqual(len(A.TONES), 10)
        for purpose in PURPOSES:
            nouns, things, deeds = A.NOUNS[purpose]
            self.assertTrue(len(nouns) >= 6 and len(things) >= 6 and len(deeds) >= 6, purpose)
            self.assertGreaterEqual(len(A.AIMS[purpose]), 3)
        for tone in A.TONES:
            adjectives, patterns = A.TONE_STYLE[tone]
            self.assertTrue(len(adjectives) >= 6 and len(patterns) >= 6, tone)
            self.assertIn(tone, A.CALLS)
            self.assertIn(tone, A.TONE_WORDS)

    def test_purpose_follows_the_persona(self):
        for name, persona in PRESETS.items():
            self.assertIn(A.purpose_for(persona), PURPOSES, name)
        self.assertEqual({n: A.purpose_for(n) for n in A.GROUPS}, A.PURPOSE_OF)
        self.assertEqual([A.purpose_for(PRESETS[n]) for n in ("miner", "turtle", "raider", "scientist", "trader", "balanced")],
                         ["mining", "defense", "raiding", "research", "trade", "mixed"])
        odd = Persona("odd", "", {}, {}, research_share=0.5)
        self.assertEqual(A.purpose_for(odd), "mixed")
        self.assertEqual(A.purpose_for(Persona("odd", "", {}, {}, aggression=0.8)), "raiding")
        self.assertEqual(A.purpose_for(Persona("odd", "", {}, {}, trade_interest=0.9)), "trade")
        self.assertEqual(A.purpose_for(Persona("odd", "", {}, {}, caution=0.9, research_share=0.5)), "defense")

    def test_tone_is_deterministic_and_varied(self):
        self.assertEqual(A.pick_tone("agent0007", 0), A.pick_tone("agent0007", 0))
        seen = {A.pick_tone(f"agent{i:04d}", 0) for i in range(300)}
        self.assertEqual(seen, set(A.TONES))
        self.assertNotEqual({A.pick_tone("agent0007", n) for n in range(30)}, {A.pick_tone("agent0007", 0)})

    def test_deterministic(self):
        for purpose, tone in itertools.product(PURPOSES, A.TONES):
            first = A.fallback_identity(purpose, tone, "agent0042", ["ABC"], ["Some Name"], 1)
            self.assertEqual(first, A.fallback_identity(purpose, tone, "agent0042", ["ABC"], ["Some Name"], 1))
            self.assertEqual(A.fallback_charter(purpose, tone, "s", 2), A.fallback_charter(purpose, tone, "s", 2))

    def test_limits_and_uniqueness_over_many_cells(self):
        taken_tags, taken_names = [], []
        for purpose, tone in itertools.product(PURPOSES, A.TONES):
            for seed in range(12):
                tag, name = A.fallback_identity(purpose, tone, f"agent{seed:04d}", taken_tags, taken_names, seed % 3)
                self.assertTrue(3 <= len(name) <= 30, name)
                self.assertTrue(A.NAME_OK.match(name), name)
                self.assertRegex(tag, r"^[A-Z0-9]{3,8}$")
                self.assertNotIn(tag.lower(), [t.lower() for t in taken_tags])
                self.assertNotIn(name.lower(), [n.lower() for n in taken_names])
                taken_tags.append(tag)
                taken_names.append(name)
        self.assertEqual(len(taken_names), 6 * 12 * 12)

    def test_unique_even_when_everything_the_generator_makes_is_taken(self):
        taken_tags, taken_names = [], []
        for _ in range(200):       # the same cell again and again: the generator must find free names
            tag, name = A.fallback_identity("mining", "laconic", "agent0001", taken_tags, taken_names, 0)
            self.assertNotIn(name.lower(), [n.lower() for n in taken_names])
            self.assertNotIn(tag.lower(), [t.lower() for t in taken_tags])
            self.assertTrue(3 <= len(name) <= 30 and 3 <= len(tag) <= 8)
            taken_tags.append(tag)
            taken_names.append(name)

    def test_long_bureaucratic_names_are_cut_to_30(self):
        self.assertEqual(A.fit_name("Interstellar Exchange Committee Dept 7 Annex"), "Interstellar Exchange")
        self.assertLessEqual(len(A.fit_name("X" * 80)), 30)
        for purpose in PURPOSES:
            for seed in range(40):
                self.assertLessEqual(len(A.make_name(purpose, "bureaucratic", f"s{seed}")), 30)

    def test_tags_come_from_the_name(self):
        self.assertEqual(A.make_tag("Lanterns of Quiet Reason"), "LQR")
        tag = A.make_tag("The Long Wall", [])
        self.assertTrue(3 <= len(tag) <= 8)
        self.assertNotEqual(A.make_tag("Lanterns of Quiet Reason", ["lqr"]).lower(), "lqr")
        for name in ("A", "Go", "7", "Red Tide Reavers", "!!!"):
            self.assertRegex(A.make_tag(name, []), r"^[A-Z0-9]{3,8}$")


class CharterTests(unittest.TestCase):
    def test_fallback_charters_fit_and_differ(self):
        seen = set()
        for purpose, tone in itertools.product(PURPOSES, A.TONES):
            for n in range(4):
                text = A.fallback_charter(purpose, tone, f"a{n}", n)
                self.assertTrue(A.CHARTER_MIN <= len(text) <= A.CHARTER_MAX, text)
                self.assertIsNotNone(A.clean_charter(text), text)
                seen.add(text)
        self.assertGreater(len(seen), 100)

    def test_clean_charter(self):
        self.assertEqual(A.clean_charter('  "We want peace and walls."  '), "We want peace and walls.")
        for bad in (None, 5, "", "short", "x" * 151, "Visit https://evil.example now", "We **dig** hard always", "agent0007 wants ore", "a | b is long"):
            self.assertIsNone(A.clean_charter(bad), bad)


class ParseTests(unittest.TestCase):
    def parse(self, text, tags=(), names=()):
        return A.parse_name_answer(text, tags, names)

    def test_good_answers(self):
        self.assertEqual(self.parse("Pickaxe Anonymous | PICK"), ("PICK", "Pickaxe Anonymous", None))
        got = self.parse("Red Tide Reavers | RTR | We hunt loot together and share every target we find.")
        self.assertEqual(got, ("RTR", "Red Tide Reavers", "We hunt loot together and share every target we find."))

    def test_tolerant_of_extra_text_labels_and_case(self):
        text = "Sure! Here you go:\n\nName: The Long Wall | Tag: tlw | Charter: We hold the wall and keep the peace.\nHope it helps."
        self.assertEqual(self.parse(text), ("TLW", "The Long Wall", "We hold the wall and keep the peace."))
        self.assertEqual(self.parse('"Quiet Hands" | qh1'), ("QH1", "Quiet Hands", None))
        self.assertEqual(self.parse("**Quiet Hands** | **QHA**"), ("QHA", "Quiet Hands", None))
        self.assertEqual(self.parse("bad line | !!\nQuiet Hands | QHA"), ("QHA", "Quiet Hands", None))      # the first valid line wins

    def test_rejected(self):
        for bad in ("Quiet <b>Hands</b> | QHA", "Quiet#Hands | QHA", "Quiet Hands, Ltd | QHA", "x" * 31 + " | ABC", "ab | ABC", "Quiet Hands | AB",
                    "Quiet Hands | ABCDEFGHI", "Quiet Hands | A-B", "Quiet Hands", "Quiet Hands |", "just some text", "", None, 7,
                    "Agent 7 Alliance | AGT", "Agent0007 | AGT", "Bot Squad | BOT1", "1234 | ABC"):
            self.assertIsNone(self.parse(bad), bad)

    def test_taken_names_and_tags_are_refused_case_insensitively(self):
        self.assertIsNone(self.parse("Quiet Hands | QHA", names=["quiet hands"]))
        self.assertIsNone(self.parse("Quiet Hands | QHA", tags=["qha"]))
        self.assertIsNone(self.parse("Quiet Hands | qha", tags=["QHA"]))
        self.assertEqual(self.parse("Quiet Hands | QHB", tags=["QHA"], names=["Other"]), ("QHB", "Quiet Hands", None))

    def test_a_bad_charter_only_loses_the_charter(self):
        self.assertEqual(self.parse("Quiet Hands | QHA | <b>x</b> see http://x.example"), ("QHA", "Quiet Hands", None))
        self.assertEqual(self.parse("Quiet Hands | QHA | too short"), ("QHA", "Quiet Hands", None))

    def test_prompt_has_purpose_tone_and_taken_but_no_founder(self):
        prompt = A.name_prompt("raiding", "menacing", ["RTR"], ["Red Tide Reavers"])
        self.assertIn("raiders who want loot", prompt)
        self.assertIn("menacing", prompt)
        self.assertIn("Red Tide Reavers", prompt)
        self.assertIn("NAME | TAG | CHARTER", prompt)
        self.assertNotRegex(prompt, r"agent\d")

    def test_fake_model_is_valid_and_repeatable(self):
        for purpose, tone in itertools.product(PURPOSES, A.TONES):
            prompt = A.name_prompt(purpose, tone, ["ABC"], ["Taken One"])
            answer = L.fake_answer("sys", prompt)
            self.assertEqual(answer, L.fake_answer("sys", prompt))
            got = A.parse_name_answer(answer, ["ABC"], ["Taken One"])
            self.assertIsNotNone(got, answer)
            self.assertIsNotNone(got[2], answer)

    def test_choice_parsing(self):
        self.assertEqual(A.parse_choice("ACCEPT\nThe charter suits me."), ("ACCEPT", "The charter suits me"))
        self.assertEqual(A.parse_choice("refuse - walls are boring")[0], "REFUSE")
        self.assertEqual(A.parse_choice("wait")[0], "WAIT")
        for junk in ("", None, "I think maybe accept", "yes"):
            self.assertEqual(A.parse_choice(junk)[0], "WAIT", junk)
        facts = A.choice_facts(name="N", tag="T", charter="We dig.", purpose="mining", members=3, fit=0.7, traits=["peaceful"], persona_name="miner")
        prompt = A.choice_prompt(facts)
        self.assertIn("CHARTER: We dig.", prompt)
        self.assertIn(L.fake_answer("sys", prompt).split()[0], ("ACCEPT", "REFUSE"))
        self.assertEqual(L.fake_answer("sys", prompt), L.fake_answer("sys", prompt))


class FlowTests(unittest.TestCase):
    """The agents: founding with a held model, adverts, and the choice before applying."""

    def setUp(self):
        self.h = TA.AgentAllianceTests("test_no_sociability_no_alliance_calls")
        self.h.setUp()
        self.clock = [1000.0]

    def agent(self, *args, llm=None, **kw):
        a = self.h.add(*args, **kw)
        a.llm = llm
        a.alli_clock = lambda: self.clock[0]
        return a

    def found_with(self, llm, steps=60):
        a = self.agent("agent0001", 1, (1, 3, 8), born=TA.T0, llm=llm)
        for n in (3, 4, 5):
            self.h.store.put(f"agent000{n}", {"user_id": n, "persona": "miner", "token": "x"})
        return a

    def test_no_model_names_with_the_rules_and_logs_it(self):
        self.h.board = AdBoard()
        a = self.found_with(None)
        for _ in range(60):
            self.h.step(130.0, "agent0001")
            if self.h.world.alliances:
                break
        (named,) = self.h.events(a, "alliance_named")
        (founded,) = self.h.events(a, "alliance_founded")
        self.assertEqual((named["source"], named["charter_source"], named["purpose"]), ("rule", "rule", "mining"))
        self.assertIn(named["tone"], A.TONES)
        self.assertEqual((named["name"], named["tag"]), (founded["name"], founded["alliance"]))
        self.assertEqual(a.memory.alliance["charter"], named["charter"])
        self.h.step(130.0, "agent0001")
        (ad,) = [p for p in self.h.board.posts if p[0] == "ALLIANCE"]
        self.assertLessEqual(len(ad[1]), 200)
        self.assertEqual(ad[2]["charter"], named["charter"])
        self.assertIn(ad[2]["charter"][:20], ad[1])
        self.assertEqual((ad[2]["purpose"], ad[2]["tone"]), ("mining", named["tone"]))
        self.assertLessEqual(len(str(ad[2]).encode()), 600)

    def test_the_model_names_it_on_a_later_look_without_blocking_or_double_founding(self):
        llm = Held()
        a = self.found_with(llm)
        for _ in range(60):
            self.h.step(130.0, "agent0001")
            if llm.calls:
                break
        self.assertEqual(len(llm.asked("alliance_name")), 1)
        self.assertEqual(llm.calls[0][2], L.NORMAL)
        prompt = llm.calls[0][3]
        self.assertNotIn("agent0001", prompt)
        self.assertNotRegex(prompt, r"agent\d")
        self.assertIn("steady production", prompt)
        for _ in range(8):                       # the model is slow: nothing founded, nothing asked twice
            self.h.step(130.0, "agent0001")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(self.h.world.alliances, {})
        llm.answer("Pickaxe Anonymous | PICK | We dig for steady ore and welcome every miner who shares the load.")
        self.h.step(130.0, "agent0001")
        self.h.step(130.0, "agent0001")
        (aid,) = self.h.world.alliances
        self.assertEqual((self.h.world.alliances[aid]["name"], self.h.world.alliances[aid]["tag"]), ("Pickaxe Anonymous", "PICK"))
        (named,) = self.h.events(a, "alliance_named")
        self.assertEqual((named["source"], named["charter_source"]), ("llm", "llm"))
        self.assertEqual(len(self.h.events(a, "alliance_founded")), 1)
        for _ in range(5):
            self.h.step(130.0, "agent0001")
        self.assertEqual(len(self.h.world.alliances), 1)
        self.assertEqual(len(llm.calls), 1)

    def test_a_bad_taken_or_missing_answer_falls_back_to_the_rules(self):
        for answer in ("garbage with no bar", None, "Quiet <b>Hands</b> | QHA", "Taken Name | TKN | We dig for steady ore and welcome miners."):
            self.setUp()
            llm = Held()
            a = self.found_with(llm)
            for _ in range(60):
                self.h.step(130.0, "agent0001")
                if llm.calls:
                    break
            if answer == "Taken Name | TKN | We dig for steady ore and welcome miners.":
                self.h.world.alliances[50] = {"id": 50, "tag": "TKN", "name": "Taken Name", "open": False, "founder": 77, "members": [77]}
            llm.answer(answer)
            for _ in range(6):
                self.h.step(130.0, "agent0001")
            (named,) = self.h.events(a, "alliance_named")
            self.assertEqual(named["source"], "rule", answer)
            self.assertTrue(3 <= len(named["name"]) <= 30 and 3 <= len(named["tag"]) <= 8)
            self.assertTrue(a.memory.alliance["founder"], answer)

    def test_a_model_that_never_answers_times_out(self):
        llm = Held()
        a = self.found_with(llm)
        for _ in range(60):
            self.h.step(130.0, "agent0001")
            if llm.calls:
                break
        self.clock[0] += 500.0
        for _ in range(4):
            self.h.step(130.0, "agent0001")
        (named,) = self.h.events(a, "alliance_named")
        self.assertEqual(named["source"], "rule")
        self.assertTrue(a.memory.alliance["founder"])

    def test_fake_model_founds_with_a_valid_llm_name(self):
        a = self.found_with(L.LLMClient("fake", log_dir=None))
        for _ in range(80):
            self.h.step(130.0, "agent0001")
            if self.h.world.alliances:
                break
            import time
            time.sleep(0.02)
        (named,) = self.h.events(a, "alliance_named")
        self.assertEqual(named["source"], "llm")
        self.assertTrue(A.TAG_OK.match(named["tag"]))

    # -- the choice --
    def recruit(self, applicant_llm, answer=None):
        self.h.board = AdBoard()
        a = self.agent("agent0001", 1, (1, 3, 8), others=[(2, (1, 5, 6))])
        aid = self.h.found(a)
        a.memory.alliance.update(charter="We hold the wall and keep the peace. Walls are cheap.", purpose="defense", tone="solemn")
        self.h.step(130.0, "agent0001")                                # the advert goes up
        b = self.agent("agent0002", 2, (1, 5, 6), llm=applicant_llm, others=[(1, (1, 3, 8))])
        return a, b, aid

    def applied(self, b):
        return [p for p, _ in b.client.post_log if p.endswith("/apply")]

    def test_advert_carries_the_charter(self):
        a, b, aid = self.recruit(None)
        (ad,) = [p for p in self.h.board.posts if p[0] == "ALLIANCE"]
        self.assertEqual(ad[2]["charter"], "We hold the wall and keep the peace. Walls are cheap.")
        self.assertEqual((ad[2]["alliance_id"], ad[2]["purpose"]), (aid, "defense"))
        self.assertIn("MINE01", ad[1])
        self.assertIn("We hold the wall", ad[1])

    def test_no_model_the_rules_choose_as_before(self):
        a, b, aid = self.recruit(None)
        self.h.step(130.0, "agent0002")
        self.assertEqual(len(self.applied(b)), 1)
        (choice,) = self.h.events(b, "alliance_choice") if self.h.events(b, "alliance_choice") else (None,)
        self.assertIsNone(choice)                 # nobody was asked, nothing is logged

    def test_the_model_reads_the_charter_and_may_accept(self):
        llm = Held()
        a, b, aid = self.recruit(llm)
        self.h.step(130.0, "agent0002")
        self.assertEqual(self.applied(b), [])                          # not yet: waiting for the model
        (call,) = llm.asked("alliance_choice")
        self.assertIn("We hold the wall and keep the peace", call[3])
        self.assertIn("CHARTER", call[3])
        self.assertNotIn("agent0001", call[3])
        for _ in range(3):
            self.h.step(130.0, "agent0002")
        self.assertEqual(len(llm.asked("alliance_choice")), 1)
        llm.answer("ACCEPT\nWalls suit a peaceful miner.")
        self.h.step(130.0, "agent0002")
        self.h.step(130.0, "agent0002")
        self.assertEqual(len(self.applied(b)), 1)
        (choice,) = self.h.events(b, "alliance_choice")
        self.assertEqual((choice["decision"], choice["source"], choice["alliance_id"]), ("ACCEPT", "llm", aid))
        self.assertIn("Walls suit", choice["reason"])

    def test_the_model_may_refuse_and_then_the_agent_does_not_apply(self):
        llm = Held()
        a, b, aid = self.recruit(llm)
        self.h.step(130.0, "agent0002")
        llm.answer("REFUSE\nToo many walls for my taste.")
        for _ in range(4):
            self.h.step(130.0, "agent0002")
        self.assertEqual(self.applied(b), [])
        self.assertEqual(self.h.events(b, "alliance_choice")[0]["decision"], "REFUSE")
        self.assertEqual(len(llm.asked("alliance_choice")), 1)       # and it is not asked about the same alliance again

    def test_wait_twice_is_a_refusal(self):
        llm = Held(auto="WAIT\nlet me think")
        a, b, aid = self.recruit(llm)
        for _ in range(8):
            self.h.step(400.0, "agent0002")
        self.assertEqual(self.applied(b), [])
        decisions = [c["decision"] for c in self.h.events(b, "alliance_choice")]
        self.assertEqual(decisions, ["WAIT", "REFUSE"])

    def test_no_answer_or_garbage_falls_back_to_the_rules(self):
        for answer in (None, "hmm hard to say"):
            self.setUp()
            llm = Held(auto=lambda purpose, answer=answer: answer)
            a, b, aid = self.recruit(llm)
            for _ in range(4):
                self.h.step(400.0, "agent0002")
            if answer is None:
                self.assertEqual(len(self.applied(b)), 1)
                self.assertEqual(self.h.events(b, "alliance_choice")[0]["source"], "rule")
            else:
                self.assertEqual(self.applied(b), [])                    # WAIT x2 = refused: only a clear model answer is taken seriously

    def test_hard_rules_cannot_be_overridden_by_the_model(self):
        llm = Held(auto="ACCEPT\nSure!")
        a, b, aid = self.recruit(llm)
        b.memory.aggressors[(1, 3, 8)] = TA.T0 - 10       # the founder attacked b a moment ago
        for _ in range(4):
            self.h.step(130.0, "agent0002")
        self.assertEqual(self.applied(b), [])
        self.assertEqual(llm.asked("alliance_choice"), [])           # the model is not even asked

    def test_the_leader_welcomes_with_the_charter(self):
        a, b, aid = self.recruit(None)
        self.h.step(130.0, "agent0002")
        self.h.step(130.0, "agent0001")
        sent = [body for path, body in a.client.post_log if path == "/chat/send"]
        self.assertTrue(any("We hold the wall" in (b_ or {}).get("text", "") for b_ in sent))


class ReportTests(unittest.TestCase):
    def test_report_line(self):
        t = 1_700_000_000
        events = [
            {"t": t, "agent": "a1", "event": "alliance_named", "name": "The Long Wall", "tag": "TLW", "tone": "solemn", "purpose": "defense",
             "source": "llm", "charter": "We hold the wall."},
            {"t": t + 1, "agent": "a2", "event": "alliance_named", "name": "Pickaxe Anonymous", "tag": "PICK", "tone": "funny", "purpose": "mining",
             "source": "rule", "charter": "We dig."},
            {"t": t + 2, "agent": "a3", "event": "alliance_choice", "alliance": "TLW", "decision": "ACCEPT", "source": "llm", "reason": "ok"},
            {"t": t + 3, "agent": "a4", "event": "alliance_choice", "alliance": "TLW", "decision": "REFUSE", "source": "llm", "reason": "no"},
            {"t": t + 4, "agent": "a5", "event": "alliance_choice", "alliance": "PICK", "decision": "ACCEPT", "source": "rule", "reason": "x"},
        ]
        text = "\n".join(sl.alliances_section(events))
        self.assertIn("1 by the model, 1 by the rules", text)
        self.assertIn("3 choices after reading a charter (model: accept 1, refuse 1; rules 1)", text)
        self.assertIn("TLW", text)
        self.assertIn("We hold the wall.", text)


if __name__ == "__main__":
    unittest.main()
