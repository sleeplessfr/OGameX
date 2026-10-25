import random
import unittest

import summarize_logs as sl
from swarm.military import Intel, Memory, pick_attack
from swarm.opponents import record_result, record_spy, strength, target_factor
from swarm.personas import PRESETS


class OpponentRecordTests(unittest.TestCase):
    def test_strangers_count_normally(self):
        self.assertEqual(target_factor(None, 0), 1.0)
        self.assertEqual(target_factor({"attacks": 0}, 0), 1.0)

    def test_wins_raise_and_losses_lower_the_value_of_a_target(self):
        opp = {}
        for _ in range(4):
            record_result(opp, (1, 2, 3), 100, "good", 50_000, 1_000)
        easy = target_factor(opp[(1, 2, 3)], 100)
        opp2 = {}
        for _ in range(2):
            record_result(opp2, (1, 2, 4), 100, "bad", 0, 90_000)
        hard = target_factor(opp2[(1, 2, 4)], 100)
        self.assertGreater(easy, 1.0)
        self.assertLess(hard, 1.0)
        self.assertGreaterEqual(hard, 0.25)
        self.assertLessEqual(easy, 1.5)
        self.assertEqual(opp[(1, 2, 3)]["net"], 4 * 49_000)

    def test_old_bad_news_fades(self):
        opp = {}
        record_result(opp, (1, 2, 4), 0, "bad", 0, 90_000)
        fresh = target_factor(opp[(1, 2, 4)], 60)
        old = target_factor(opp[(1, 2, 4)], 13 * 3600)
        self.assertLess(fresh, old)
        self.assertLess(old, 1.0)

    def test_spy_records_strength_and_keeps_history(self):
        opp = {}
        record_result(opp, (1, 2, 3), 10, "good", 1000, 0)
        record_spy(opp, (1, 2, 3), 20, 5000, {"light_fighter": 10}, {"rocket_launcher": 5})
        rec = opp[(1, 2, 3)]
        self.assertEqual(rec["attacks"], 1)
        self.assertEqual(rec["loot"], 5000)
        self.assertEqual(rec["strength"], strength({"light_fighter": 10}, {"rocket_launcher": 5}))
        self.assertGreater(rec["strength"], 0)
        self.assertEqual(strength(None, None), 0)


class TargetChoiceTests(unittest.TestCase):
    def memory(self):
        m = Memory()
        for c, loot in (((1, 2, 3), 100_000), ((1, 2, 4), 120_000)):
            m.intel[c] = {"at": 1000, "loot": loot, "defenders": 0, "owner_id": 9, "ships": {}, "defense": {}, "research": {}}
        return m

    def test_a_target_that_beat_us_loses_to_a_poorer_but_easy_one(self):
        persona = PRESETS["raider"]
        m = self.memory()
        pick = pick_attack(persona, random.Random(1), m, 1100, {"light_fighter": 200, "small_cargo": 50})
        self.assertEqual(pick[0], (1, 2, 4))   # strangers: the richer one
        for _ in range(2):
            record_result(m.opponents, (1, 2, 4), 1000, "bad", 0, 90_000)
        pick = pick_attack(persona, random.Random(1), m, 1100, {"light_fighter": 200, "small_cargo": 50})
        self.assertEqual(pick[0], (1, 2, 3))

    def test_memory_survives_a_restart(self):
        m = self.memory()
        record_result(m.opponents, (1, 2, 4), 1000, "good", 5000, 100)
        again = Memory.from_dict(m.to_dict())
        self.assertEqual(again.opponents[(1, 2, 4)]["wins"], 1)
        self.assertEqual(again.opponents[(1, 2, 4)]["net"], 4900)


class ReportSectionTests(unittest.TestCase):
    def test_opponents_known_section(self):
        t = 1_700_000_000
        events = [
            {"t": t, "agent": "a1", "event": "report", "at": "1:3:4", "loot": 5000, "defenders": 2, "strength": 42000},
            {"t": t + 1, "agent": "a1", "event": "opponent", "at": "1:3:4", "attacks": 1, "wins": 0, "losses": 1, "net": -60000,
             "strength": 42000, "loot": 5000},
            {"t": t + 2, "agent": "a1", "event": "opponent", "at": "1:3:4", "attacks": 2, "wins": 0, "losses": 2, "net": -90000,
             "strength": 42000, "loot": 5000},
        ]
        text = sl.summarize(events)
        self.assertIn("OPPONENTS KNOWN", text)
        self.assertIn("a1 on 1:3:4: 2 attacks, 0 won, 2 lost, net -90,000; last seen strength 42000", text)

    def test_section_says_none_without_events(self):
        self.assertIn("none yet", sl.summarize([{"t": 1_700_000_000, "agent": "a1", "event": "build"}]))


if __name__ == "__main__":
    unittest.main()
