import json
import tempfile
import unittest
from pathlib import Path

import summarize_logs as sl


def ev(t, agent, event, **f):
    return {"t": t, "agent": agent, "event": event, **f}


class SummarizeTests(unittest.TestCase):
    def test_empty_gives_a_helpful_message(self):
        self.assertIn("No events", sl.summarize([]))

    def test_report_covers_battles_defence_merchant_and_errors(self):
        t = 1_700_000_000
        events = [
            ev(t, "a1", "battle", role="attacker", my_loss=0, their_loss=1000, loot=25000, verdict="good"),
            ev(t + 1, "a1", "battle", role="attacker", my_loss=60000, their_loss=2000, loot=0, verdict="bad"),
            ev(t + 2, "a2", "battle", role="defender", my_loss=0, their_loss=0, loot=25000, rounds=0),
            ev(t + 3, "a1", "attack_skipped", at="1:3:4", why="simulated fight: win chance 0%, expected loss 48,000 for loot 25,350; wants at least 57%"),
            ev(t + 4, "a1", "attack_skipped", at="1:4:4", why="fleet cannot carry enough of the loot, wants cargo ships"),
            ev(t + 5, "a2", "threat", kind="attack", mission=1, **{"from": "1:3:7"}, arrives_in=40),
            ev(t + 6, "a2", "defend", what="rocket_launcher", amount=30, why="attack on its way"),
            ev(t + 7, "a2", "trade", gave={"metal": 100}, received={"crystal": 90}),
            ev(t + 8, "a1", "lesson", why="lost 60000 for 2000 gained"),
            ev(t + 9, "a1", "api_error", status=502, code="bad_response", msg="Not JSON", request="GET /status"),
        ]
        text = sl.summarize(events, {"a1": "(raider)"})
        self.assertIn("a1 (raider)", text)
        self.assertIn("2 attacks, lost 60,000, destroyed 3,000, looted 25,000", text)
        self.assertIn("net -32,000", text)
        self.assertIn("robbed of 25,000", text)
        self.assertIn("simulated fight too risky 1", text)
        self.assertIn("cannot carry the loot 1", text)
        self.assertIn("1 threats (attack 1), 1 defence orders", text)
        self.assertIn("built 30 rocket_launcher", text)
        self.assertIn("trade", text)
        self.assertIn("lost 60000 for 2000 gained", text)
        self.assertIn("1x api_error status=502", text)

    def test_reads_log_files_and_ignores_broken_lines(self):
        d = Path(tempfile.mkdtemp())
        (d / "agent1.jsonl").write_text(json.dumps(ev(100, "agent1", "build", what="metal_mine")) + "\n{broken\n\n" +
                                        json.dumps(ev(50, "agent1", "wait")) + "\n", encoding="utf-8")
        events = sl.read_events(d)
        self.assertEqual([e["t"] for e in events], [50, 100])          # sorted
        self.assertEqual(len(sl.read_events(d, since=80)), 1)          # --hours filter
        self.assertEqual(sl.personas_from_state(d / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
