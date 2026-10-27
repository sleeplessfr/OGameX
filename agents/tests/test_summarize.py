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
            ev(t + 1, "a1", "battle", at="1:3:4", role="attacker", my_loss=60000, their_loss=2000, loot=0, verdict="bad"),
            ev(t + 2, "a2", "battle", role="defender", my_loss=0, their_loss=0, loot=25000, rounds=0),
            ev(t + 3, "a1", "attack_skipped", at="1:3:4", why="simulated fight: win chance 0%, expected loss 48,000 for loot 25,350; wants at least 57%"),
            ev(t + 4, "a1", "attack_skipped", at="1:4:4", why="fleet cannot carry enough of the loot, wants cargo ships"),
            ev(t + 5, "a2", "threat", kind="attack", mission=1, **{"from": "1:3:7"}, arrives_in=40),
            ev(t + 6, "a2", "defend", what="rocket_launcher", amount=30, why="attack on its way"),
            ev(t + 7, "a2", "trade", gave={"metal": 100}, received={"crystal": 90}),
            ev(t - 5, "a1", "attack", at="1:3:4", why="weak target; simulated fight: win chance 70%", ships={"light_fighter": 20}),
            ev(t + 8, "a1", "lesson", why="lost 60000 for 2000 gained"),
            ev(t + 10, "a2", "wait", why="no free fields on this planet (a terraformer adds some)"),
            ev(t + 11, "a2", "wait", why="only 20 free fields left: waiting for terraformer"),
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
        self.assertIn("a2: 1 waits on the way to the terraformer, 1 waits with a full planet", text)
        self.assertIn("reason: weak target; simulated fight: win chance 70%", text)

    def test_reads_log_files_and_ignores_broken_lines(self):
        d = Path(tempfile.mkdtemp())
        (d / "agent1.jsonl").write_text(json.dumps(ev(100, "agent1", "build", what="metal_mine")) + "\n{broken\n\n" +
                                        json.dumps(ev(50, "agent1", "wait")) + "\n", encoding="utf-8")
        events = sl.read_events(d)
        self.assertEqual([e["t"] for e in events], [50, 100])          # sorted
        self.assertEqual(len(sl.read_events(d, since=80)), 1)          # --hours filter
        self.assertEqual(sl.personas_from_state(d / "missing.json"), {})

class LlmReportTests(unittest.TestCase):
    def test_section_counts_calls_failures_latency_rewrites_and_diary(self):
        t = 1_700_000_000
        events = [ev(t, "a1", "build", what="metal_mine"),
                  ev(t + 1, "a1", "voice_post", kind="WARNING", original="Robbed of 5.", text="Hey, robbed of 5."),
                  ev(t + 2, "a1", "diary", text="They took it all.", about="robbed"),
                  ev(t + 3, "a2", "diary", text="A new home.", about="colony")]
        calls = [{"t": t, "kind": "llm_call", "agent": "a1", "purpose": "rewrite:WARNING", "ok": True, "latency": 2.0, "tokens": 100},
                 {"t": t, "kind": "llm_call", "agent": "a1", "purpose": "diary", "ok": True, "latency": 4.0, "tokens": 50},
                 {"t": t, "kind": "llm_call", "agent": "a2", "purpose": "diary", "ok": False, "latency": 60.0, "error": "timed out"}]
        text = sl.summarize(events, None, calls)
        self.assertIn("LLM (", text)
        self.assertIn("3 calls, 1 failed, average latency 3.0 s (slowest 4.0 s), 150 tokens reported", text)
        self.assertIn("failed 1x: timed out", text)
        self.assertIn("1 board posts rewritten, 2 diary lines", text)
        self.assertIn("diary: A new home.", text)
        self.assertNotIn("LLM (", sl.summarize(events[:1]))                # no language model, no section

    def test_reads_the_call_log_and_survives_a_missing_or_broken_one(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(sl.read_llm_calls(d), [])
        (d / "llm.jsonl").write_text(json.dumps({"t": 100, "kind": "llm_call", "ok": True}) + "\n{broken\n" +
                                     json.dumps({"t": 10, "kind": "llm_call", "ok": True}) + "\n")
        self.assertEqual(len(sl.read_llm_calls(d)), 2)
        self.assertEqual(len(sl.read_llm_calls(d, since=50)), 1)
        self.assertEqual(sl.read_events(d), [])                             # the call log is not an agent log


class RecyclingReportTests(unittest.TestCase):
    def test_report_counts_trips_haul_and_skips(self):
        t = 1_700_000_000
        events = [
            ev(t, "a1", "units", what="recycler", amount=3, why="60,000 debris remembered"),
            ev(t + 1, "a1", "recycle_sent", at="1:1:5", ships=3, debris=60000, haul=40000, fuel=120, seconds=60),
            ev(t + 2, "a1", "recycle_sent", at="1:2:5", ships=1, debris=20000, haul=20000, fuel=30, seconds=60),
            ev(t + 3, "a1", "recycle_skipped", at="1:9:5", debris=5000, why="haul 5,000 not worth 900 deuterium"),
        ]
        text = sl.summarize(events)
        self.assertIn("a1: 2 trips, about 60,000 metal+crystal expected, 150 deuterium burned, 3 recyclers built, 1 skipped", text)
        self.assertIn("skipped 1:9:5: haul 5,000 not worth 900 deuterium", text)

    def test_report_without_recycling_says_none(self):
        text = sl.summarize([ev(1000.0, "a1", "build", what="metal_mine")])
        self.assertIn("RECYCLING", text)
        self.assertRegex(text, r"RECYCLING[^\n]*\n  none")


if __name__ == "__main__":
    unittest.main()


class ClassAndColonyReportTests(unittest.TestCase):
    def test_report_lists_classes_and_colony_events(self):
        events = [
            {"t": 1000.0, "agent": "agent0001", "event": "class", "chosen": "collector"},
            {"t": 1001.0, "agent": "agent0002", "event": "class", "chosen": "discoverer"},
            {"t": 1002.0, "agent": "agent0002", "event": "expand", "why": "only 67 free fields on the home planet"},
            {"t": 2000.0, "agent": "agent0002", "event": "colony_sent", "at": "1:3:9", "why": "position 9: about 198 fields"},
            {"t": 3000.0, "agent": "agent0002", "event": "colony_founded", "planets": 2},
        ]
        text = sl.summarize(events)
        self.assertIn("collector: agent0001", text)
        self.assertIn("discoverer: agent0002", text)
        self.assertIn("agent0002 colony_sent: position 9: about 198 fields at 1:3:9", text)
        self.assertIn("agent0002 colony_founded", text)

    def test_report_without_them_says_none(self):
        text = sl.summarize([{"t": 1000.0, "agent": "agent0001", "event": "build", "what": "metal_mine"}])
        self.assertIn("none in this period", text)


class ExpeditionReportTests(unittest.TestCase):
    def test_report_counts_outcomes_resources_and_losses(self):
        events = [
            {"t": 1000.0, "agent": "agent0001", "event": "expedition_sent", "ships": {"small_cargo": 4}},
            {"t": 1100.0, "agent": "agent0001", "event": "expedition_result", "outcome": "resources", "metal": 12000},
            {"t": 1200.0, "agent": "agent0001", "event": "expedition_sent", "ships": {"small_cargo": 4}},
            {"t": 1300.0, "agent": "agent0001", "event": "expedition_result", "outcome": "ships", "ships": {"small_cargo": 3}},
            {"t": 1400.0, "agent": "agent0001", "event": "expedition_sent", "ships": {"small_cargo": 4}},
            {"t": 1500.0, "agent": "agent0001", "event": "expedition_result", "outcome": "loss", "lost": {"small_cargo": 4}, "lost_value": 4000},
            {"t": 1600.0, "agent": "agent0001", "event": "expedition_result", "outcome": "dark_matter", "dark_matter": 150},
            {"t": 1700.0, "agent": "agent0001", "event": "expedition_refused", "code": "mission_rejected"},
        ]
        text = sl.summarize(events)
        self.assertIn("EXPEDITIONS", text)
        self.assertIn("agent0001: 3 sent, 4 reports, 1 refused: resources 1, ships 1, loss 1, dark_matter 1", text)
        self.assertIn("found metal 12,000", text)
        self.assertIn("dark matter 150", text)
        self.assertIn("ships lost worth 4,000", text)
        self.assertIn("net resources +8,000", text)
        self.assertIn("ships found: 3 small_cargo", text)
        self.assertIn("ships lost: 4 small_cargo", text)

    def test_no_expeditions_says_none(self):
        text = sl.summarize([{"t": 1000.0, "agent": "a", "event": "build", "what": "metal_mine"}])
        self.assertIn("EXPEDITIONS", text)


class BoardAndTradeReportTests(unittest.TestCase):
    def test_report_counts_posts_deals_goods_and_cheats(self):
        t = 1_700_000_000
        events = [
            ev(t, "a1", "board_post", kind="OFFER", text="OFFER 20,000 metal for 13,733 crystal", id=1),
            ev(t + 1, "a1", "trade_offer", action="posted", give={"metal": 20000}, want={"crystal": 13733}, post=1),
            ev(t + 2, "a2", "board_post", kind="WARNING", text="Robbed", id=2),
            ev(t + 3, "a2", "board_post", kind="REVENGE", text="Revenge", id=3),
            ev(t + 4, "a3", "board_read", posts=2, warnings=1, revenge=1, guard=["1:1:5"]),
            ev(t + 5, "a2", "trade_offer", action="answered", **{"with": "a1"}, give={"crystal": 13733}, want={"metal": 20000}),
            ev(t + 6, "a2", "trade_sent", to="a1", at="1:3:8", resources={"crystal": 3000}, value=4500, instalment="1/4", fuel=10, seconds=60),
            ev(t + 7, "a1", "trade_received", partner="a2", resources={"crystal": 3000}, value=4500),
            ev(t + 8, "a1", "trade_sent", to="a2", at="1:5:2", resources={"metal": 5000}, value=5000, instalment="1/4", fuel=10, seconds=60),
            ev(t + 9, "a1", "trade_offer", action="done", **{"with": "a2"}, sent=20000, received=19400, instalments=4),
            ev(t + 10, "a1", "trade_cheated", partner="a9", owed=4800, sent=5000, received=0, why="no payment in time; shunned"),
        ]
        text = sl.summarize(events)
        self.assertIn("BOARD AND TRADE", text)
        self.assertIn("3 posts (OFFER 1, WARNING 1, REVENGE 1), 1 reads with news (1 warnings, 1 revenge calls, guard raised 1x)", text)
        self.assertIn("1 offers posted, 1 answered, 0 accepted, 1 completed", text)
        self.assertIn("2 instalments sent (worth 9,500: crystal 3,000, metal 5,000), 1 deliveries received (worth 4,500), 1 cheats", text)
        self.assertIn("a1: 1 posts, sent 5,000, received 4,500, cheated 1x", text)
        self.assertIn("a1 was cheated by a9: owed 4,800", text)

    def test_prices_learned_subsection(self):
        t = 1_700_000_000
        events = [
            ev(t, "a1", "board_post", kind="OFFER", text="OFFER", id=1),
            ev(t + 1, "a1", "price_learned", pair="metal>crystal", kind="ask", old=1.03, new=0.9476, why="unanswered", start=1.03, answered=0, unanswered=1),
            ev(t + 2, "a1", "price_learned", pair="metal>crystal", kind="ask", old=0.9476, new=0.8718, why="unanswered", start=1.03, answered=0, unanswered=2),
            ev(t + 3, "a1", "price_learned", pair="metal>crystal", kind="ask", old=0.8718, new=0.898, why="answered", start=1.03, answered=1, unanswered=2),
            ev(t + 4, "a1", "price_learned", pair="metal>crystal", kind="ask", old=0.898, new=0.925, why="completed", start=1.03, answered=1, unanswered=2),
            ev(t + 5, "a2", "price_learned", pair="crystal>metal", kind="accept", old=1.0526, new=1.0726, why="need", start=1.03, answered=0, unanswered=0),
        ]
        text = sl.summarize(events)
        self.assertIn("PRICES LEARNED", text)
        self.assertIn("a1 metal>crystal ask: start 1.03 -> now 0.93 (answered 1, unanswered 2, completed 1)", text)
        self.assertIn("a2 crystal>metal accept: start 1.05 -> now 1.07 (loosened 1x after unmet need, deals completed 0)", text)
        self.assertNotIn("PRICES LEARNED", sl.summarize([ev(t, "a1", "board_post", kind="OFFER", text="x", id=1)]))

    def test_bounties_and_revenge_section(self):
        t = 1_700_000_000
        events = [
            ev(t, "a3", "bounty_post", bounty=1, target="1:1:5", target_id=2, reward={"metal": 4000}, value=4000, expires=t + 21600),
            ev(t + 1, "a1", "revenge_join", post=9, victim="a3", raider_at="1:1:5", raider_id=2, until=t + 7200, replied=True),
            ev(t + 50, "a1", "bounty_claim", bounty=1, claim=2, poster="a3", at="1:1:5", loot=9000, report=77, reward={"metal": 4000}),
            ev(t + 60, "a1", "bounty_verdict", bounty=1, claim=2, ok=True, why="confirmed", role="claimant", poster="a3"),
            ev(t + 61, "a3", "bounty_verdict", bounty=1, claim=2, ok=True, why="confirmed", role="poster", claimant="a1"),
            ev(t + 70, "a3", "bounty_paid", side="poster", bounty="1", to="a1", resources={"metal": 4000}, value=4000),
            ev(t + 90, "a1", "bounty_paid", side="claimant", bounty="1", poster="a3", resources={"metal": 4000}, value=4000),
            ev(t + 100, "a1", "revenge_done", raider_at="1:1:5", loot=9000),
            ev(t + 110, "a5", "bounty_post", bounty=7, target="1:2:2", target_id=4, reward={"crystal": 3000}, value=4500, expires=t + 21600),
            ev(t + 120, "a6", "bounty_claim", bounty=7, claim=8, poster="a5", at="1:2:2", loot=100, report=3, reward={"crystal": 3000}),
            ev(t + 130, "a6", "bounty_verdict", bounty=7, claim=8, ok=True, why="confirmed", role="claimant", poster="a5"),
            ev(t + 9000, "a6", "bounty_unpaid", side="claimant", bounty="7", poster="a5", promised={"crystal": 3000}, why="x"),
            ev(t + 140, "a7", "bounty_verdict", bounty=11, claim=12, ok=False, why="no loot", role="claimant", poster="a5"),
        ]
        text = sl.summarize(events)
        self.assertIn("BOUNTIES AND REVENGE", text)
        self.assertIn("revenge: 1 joins (a1 1), 1 ended by a raid on the named raider", text)
        self.assertIn("a1 joined a3's call against 1:1:5 (player 2)", text)
        self.assertIn("bounties: 2 posted (rewards worth 8,500), 2 claims, 2 confirmed, 1 rejected, 1 paid by the poster (worth 4,000), "
                      "1 received, 1 never paid, 0 the poster gave up on", text)
        self.assertIn("a3 wants 1:1:5 (player 2) for 4,000 metal: paid", text)
        self.assertIn("a5 wants 1:2:2 (player 4) for 3,000 crystal: NOT PAID", text)
        self.assertIn("a7's claim on bounty 11 was rejected: no loot", text)
        self.assertIn("a6 was never paid by a5 (bounty 7); shunned", text)

    def test_bounty_section_says_none_without_bounties(self):
        text = sl.summarize([ev(1000.0, "a1", "build", what="metal_mine")])
        self.assertRegex(text, r"BOUNTIES AND REVENGE[^\n]*\n  none")

    def test_report_without_a_board_says_none(self):
        text = sl.summarize([ev(1000.0, "a1", "build", what="metal_mine")])
        self.assertIn("BOARD AND TRADE", text)
        self.assertRegex(text, r"BOARD AND TRADE[^\n]*\n  none")


class ExpeditionStayedHomeTests(unittest.TestCase):
    def test_report_says_why_agents_stayed_home(self):
        events = [{"t": 1000.0 + i, "agent": "agent0002", "event": "expedition_skipped",
                   "why": "astrophysics too low" if i < 2 else "no free expedition slot"} for i in range(3)]
        text = sl.summarize(events)
        self.assertIn("stayed home: astrophysics too low x2, no free expedition slot x1", text)


class EvolutionSection(unittest.TestCase):
    def test_section_compares_evolving_with_control_and_shows_the_drift(self):
        from swarm import genome as G
        t = 1_700_000_000
        evo = {**G.neutral(), "metal_mine": 1.5}
        events = [
            ev(t, "a1", "evolve", group="evolving", persona="miner", window=3, growth=0.30, action="kept", genome=evo),
            ev(t + 1, "a2", "evolve", group="evolving", persona="miner", window=3, growth=0.10, action="replaced", genome=evo),
            ev(t + 2, "a3", "evolve", group="control", persona="miner", window=3, growth=0.15, action="recorded", genome=G.neutral()),
        ]
        text = sl.summarize(events)
        self.assertIn("EVOLUTION", text)
        self.assertIn("evolving: 2 results, average growth +20%", text)
        self.assertIn("control: 1 results, average growth +15%", text)
        self.assertIn("1 genomes replaced", text)
        self.assertIn("metal_mine x1.50", text)

    def test_section_says_how_to_start_it_when_empty(self):
        self.assertIn("--evolve on", sl.summarize([ev(1_700_000_000, "a1", "wait", why="x")]))
