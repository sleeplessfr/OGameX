import random
import tempfile
import unittest
from pathlib import Path

import summarize_logs as sl
from swarm.agent import Agent, TokenStore
from swarm.bandit import DEFAULT_MARGIN, MARGINS, Bandit, outcome_of
from swarm.military import Memory
from swarm.personas import PRESETS
from tests.fakegame import FakeGame


def picks(bandit, n, seed=1):
    rng = random.Random(seed)
    return [bandit.choose(rng) for _ in range(n)]


class BanditTests(unittest.TestCase):
    def test_arms_never_go_below_the_safe_floor(self):
        self.assertGreaterEqual(min(MARGINS), 1.2)
        self.assertTrue(all(m >= 1.2 for m in picks(Bandit(), 300)))

    def test_without_data_it_favours_the_current_margin(self):
        chosen = picks(Bandit(), 600)
        self.assertEqual(max(set(chosen), key=chosen.count), DEFAULT_MARGIN)

    def test_it_learns_to_leave_arms_that_keep_losing(self):
        b = Bandit()
        for _ in range(25):
            b.update(1.2, "bad")
            b.update(1.5, "bad")
            b.update(2.0, "win")
        chosen = picks(b, 300, seed=4)
        self.assertGreater(chosen.count(2.0), 200)
        self.assertLess(chosen.count(1.2), 20)

    def test_a_bold_arm_that_keeps_winning_is_kept(self):
        b = Bandit()
        for _ in range(40):
            b.update(1.2, "win")
            b.update(3.0, "win")
        chosen = picks(b, 300, seed=5)
        self.assertGreater(chosen.count(1.2), chosen.count(3.0))      # same record, but 1.2 gives up fewer fights

    def test_old_evidence_fades(self):
        b = Bandit()
        for _ in range(30):
            b.update(2.0, "bad")
        low = b.rates()[2.0]
        for _ in range(200):
            b.update(1.5, "win")
        self.assertGreater(b.rates()[2.0], low)

    def test_a_clear_failure_counts_double(self):
        a, c = Bandit(), Bandit()
        a.update(1.5, "bad")
        c.update(1.5, "loss")
        self.assertLess(a.rates()[1.5], c.rates()[1.5])

    def test_unknown_margin_is_ignored(self):
        b = Bandit()
        before = b.to_dict()
        b.update(9.9, "win")
        self.assertEqual(b.to_dict(), before)

    def test_outcome_rules(self):
        self.assertEqual(outcome_of("bad", 10, 1000), "bad")
        self.assertEqual(outcome_of("neutral", 500, 100), "win")
        self.assertEqual(outcome_of("neutral", 100, 500), "loss")

    def test_round_trip_and_garbage(self):
        b = Bandit()
        b.update(2.0, "win")
        picks(b, 5)
        self.assertEqual(Bandit.from_dict(b.to_dict()), b)
        for junk in (None, 5, "x", {"a": "bad", "b": [1], "pulls": "?"}, {"a": {"1.5": "nan?", "9": 3}}):
            self.assertEqual(Bandit.from_dict(junk).arms, MARGINS)
        self.assertEqual(Bandit.from_dict({"a": {"2.0": -4}}).a[2.0], Bandit().a[2.0])

    def test_memory_keeps_the_bandit_across_a_restart(self):
        m = Memory()
        m.bandit.update(3.0, "win")
        m.arm_for[(1, 2, 3)] = 2.0
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.bandit, m.bandit)
        self.assertEqual(back.arm_for, {(1, 2, 3): 2.0})


class AgentUsesItTests(unittest.TestCase):
    def test_attacks_are_tagged_and_the_result_is_learned(self):
        game = FakeGame()
        events = []
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        store.put("victim", {"user_id": 2})
        agent = Agent("sim", PRESETS["raider"], "x", store, random.Random(1), client=game)
        agent.log = lambda e, **k: events.append((e, k))
        agent.user_id = 1
        elapsed = 0.0
        while elapsed < 60 * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        attacks = [k for e, k in events if e == "attack"]
        battles = [k for e, k in events if e == "battle" and k.get("outcome")]
        self.assertTrue(attacks)
        self.assertTrue(all(a["margin"] in MARGINS for a in attacks))
        self.assertTrue(battles)
        self.assertTrue(all(b["margin"] in MARGINS for b in battles))
        learned = sum(agent.memory.bandit.a[m] + agent.memory.bandit.b[m] for m in MARGINS) - sum(
            Bandit().a[m] + Bandit().b[m] for m in MARGINS)
        self.assertGreater(learned, 0)

    def test_report_section(self):
        events = [
            {"t": 1.0, "agent": "agent0004", "event": "battle", "margin": 1.5, "outcome": "win"},
            {"t": 2.0, "agent": "agent0004", "event": "battle", "margin": 1.5, "outcome": "bad"},
            {"t": 3.0, "agent": "agent0004", "event": "battle", "margin": 2.0, "outcome": "win"},
        ]
        text = sl.summarize(events)
        self.assertIn("LEARNED FIGHT MARGIN", text)
        self.assertIn("x1.5: 2 attacks, 1 profit, 1 bad; x2: 1 attacks, 1 profit, 0 bad", text)
        self.assertIn("none yet", sl.summarize([{"t": 1.0, "agent": "a", "event": "build"}]))


if __name__ == "__main__":
    unittest.main()
