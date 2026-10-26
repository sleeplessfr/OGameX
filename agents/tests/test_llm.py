import json
import random
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarm import llm as L
from swarm import voice as V
from swarm.agent import Agent, TokenStore
from swarm.military import Memory
from swarm.personas import PRESETS


class FakeLM:
    """An OpenAI-compatible server on an ephemeral port. mode: ok / bad_json / empty / slow / error."""

    def __init__(self, mode="ok", answer="Hello there.", delay=0.0):
        self.mode, self.answer, self.delay = mode, answer, delay
        self.requests, self.active, self.max_active, self.order = [], 0, 0, []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer.lock:
                    outer.requests.append((self.path, body))
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                    outer.order.append(body["messages"][-1]["content"])
                try:
                    time.sleep(outer.delay)
                    if outer.mode == "error":
                        self.send_response(500)
                        self.end_headers()
                        return
                    if outer.mode == "bad_json":
                        out = b"<html>no</html>"
                    elif outer.mode == "empty":
                        out = json.dumps({"choices": [{"message": {"content": "  "}}]}).encode()
                    else:
                        out = json.dumps({"choices": [{"message": {"content": outer.answer}}], "usage": {"total_tokens": 42}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass
                finally:
                    with outer.lock:
                        outer.active -= 1

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def read_log(d):
    p = Path(d) / "llm.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def server(self, **kw):
        s = FakeLM(**kw)
        self.servers.append(s)
        return s

    def client(self, s, **kw):
        kw.setdefault("timeout", 5)
        return L.LLMClient(s.url, "m", log_dir=self.tmp, **kw)

    def test_answer_is_returned_and_logged(self):
        s = self.server(answer=" Hi. ")
        c = self.client(s)
        self.assertEqual(c.ask("a1", "diary", "sys", "prompt"), "Hi.")
        path, body = s.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["model"], body["messages"][0]["role"], body["messages"][1]["content"]), ("m", "system", "prompt"))
        line = read_log(self.tmp)[0]
        self.assertEqual((line["agent"], line["purpose"], line["prompt"], line["answer"], line["tokens"], line["ok"]),
                         ("a1", "diary", "prompt", "Hi.", 42, True))
        self.assertGreaterEqual(line["latency"], 0)
        self.assertNotIn("event", line)       # the observatory must not read it as an agent event

    def test_every_failure_is_none_and_never_raises(self):
        for mode in ("bad_json", "empty", "error"):
            s = self.server(mode=mode)
            self.assertIsNone(self.client(s).ask("a1", "x", "s", "p"), mode)
        s = self.server()
        url = s.url
        s.close()                                   # connection refused
        self.assertIsNone(L.LLMClient(url, timeout=2, log_dir=self.tmp).ask("a1", "x", "s", "p"))
        slow = self.server(delay=1.5)
        t0 = time.time()
        self.assertIsNone(self.client(slow, timeout=0.3).ask("a1", "x", "s", "p"))
        self.assertLess(time.time() - t0, 1.4)
        lines = read_log(self.tmp)
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(not x["ok"] and x["error"] for x in lines))

    def test_a_callback_that_raises_does_not_kill_the_worker(self):
        s = self.server()
        c = self.client(s)

        def boom(answer):
            raise RuntimeError("x")

        c.submit("a", "p", "s", "p", boom)
        self.assertEqual(c.ask("a", "p", "s", "p"), "Hello there.")

    def test_only_one_request_in_flight(self):
        s = self.server(delay=0.1)
        c = self.client(s)
        results = []
        done = threading.Event()
        for i in range(5):
            c.submit(f"a{i}", "p", "s", f"p{i}", lambda a: (results.append(a), len(results) == 5 and done.set()))
        self.assertTrue(done.wait(10))
        self.assertEqual(s.max_active, 1)
        self.assertEqual(len(results), 5)

    def test_urgent_requests_go_first(self):
        s = self.server(delay=0.3)
        c = self.client(s)
        c.submit("a", "p", "s", "first", lambda a: None)
        time.sleep(0.1)                              # "first" is now being served
        for name, prio in (("routine1", L.NORMAL), ("routine2", L.NORMAL), ("battle", L.HIGH)):
            c.submit("a", "p", "s", name, lambda a: None, prio)
        end = time.time() + 5
        while len(s.order) < 4 and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(s.order, ["first", "battle", "routine1", "routine2"])

    def test_a_request_that_waits_too_long_is_dropped(self):
        s = self.server(delay=0.4)
        c = self.client(s)
        c.submit("a", "p", "s", "slow", lambda a: None)
        time.sleep(0.1)
        got = []
        done = threading.Event()
        c.submit("a", "p", "s", "late", lambda a: (got.append(a), done.set()), max_wait=0.05)
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [None])
        self.assertEqual(len(s.requests), 1)
        self.assertIn("dropped", read_log(self.tmp)[-1]["error"])

    def test_hourly_budget(self):
        s = self.server()
        c = self.client(s, max_per_hour=2)
        self.assertTrue(c.ask("a", "p", "s", "1"))
        self.assertTrue(c.ask("a", "p", "s", "2"))
        self.assertIsNone(c.ask("a", "p", "s", "3"))
        self.assertEqual(len(s.requests), 2)
        self.assertIn("budget", read_log(self.tmp)[-1]["error"])

    def test_fake_mode_needs_no_server_and_keeps_numbers(self):
        c = L.make_llm("fake", log_dir=self.tmp)
        sheet = V.make_sheet("agent0001", PRESETS["raider"])
        out = c.ask("a", "rewrite", V.system_prompt(sheet), V.rewrite_prompt("WARNING", "Robbed of 1,000 at 1:2:3."))
        self.assertIn("Robbed of 1,000 at 1:2:3.", out)
        self.assertIsNone(L.make_llm(None))


class VoiceRuleTests(unittest.TestCase):
    ORIG = "Robbed of 12,500 at 1:2:3 by player 7 from 1:4:5. Watch your stores."
    DATA = {"victim_at": "1:2:3", "loot": 12500, "raider_id": 7, "raider_at": "1:4:5"}

    def test_good_answer_passes_with_quotes_stripped(self):
        self.assertEqual(V.check('"They hit me for 12500 at 1:2:3 from 1:4:5, player 7. Guard your stores!"', self.ORIG, self.DATA, True),
                         "They hit me for 12500 at 1:2:3 from 1:4:5, player 7. Guard your stores!")

    def test_rejections(self):
        bad = ["", "   ", None, 5,
               "word " * 30,                                            # too long
               "x" * 300,
               "**Robbed** of 12,500 at 1:2:3 from 1:4:5",              # markup
               "<b>Robbed</b> at 1:2:3 from 1:4:5",
               "see http://x.com 1:2:3 1:4:5",
               "Robbed of 99,999 at 1:2:3 from 1:4:5",                  # a number that differs
               "Robbed of 12,500 at 1:2:9 from 1:4:5",                  # a coordinate that differs
               "Robbed of 12,500, someone from 1:4:5",                  # a coordinate dropped (require_coords)
               "line one\nline two 1:2:3 1:4:5"]
        for b in bad:
            self.assertIsNone(V.check(b, self.ORIG, self.DATA, True), repr(b))

    def test_numbers_may_come_from_the_data(self):
        self.assertEqual(V.check("Player 7 again.", "Robbed by a raider.", {"raider_id": 7}), "Player 7 again.")
        self.assertIsNone(V.check("Player 8 again.", "Robbed by a raider.", {"raider_id": 7}))

    def test_sheet_is_deterministic_and_follows_the_persona(self):
        a, b = V.make_sheet("agent0001", PRESETS["raider"]), V.make_sheet("agent0001", PRESETS["raider"])
        self.assertEqual(a, b)
        self.assertNotEqual(a["backstory"], V.make_sheet("agent0002", PRESETS["raider"])["backstory"])
        self.assertIn("ruthless", a["traits"])
        self.assertIn("peaceful", V.make_sheet("x", PRESETS["turtle"])["traits"])
        self.assertIn("cautious", V.make_sheet("x", PRESETS["turtle"])["traits"])
        self.assertIn("agent0001", V.system_prompt(a))


class StubBoard:
    enabled = True

    def __init__(self):
        self.posted = []

    def post(self, kind, text, data=None, ttl=None):
        self.posted.append((kind, text, data))
        return {"id": len(self.posted)}


class AgentVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = TokenStore(str(self.tmp / "s.json"))
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()

    def agent(self, llm="fake", name="agent0001", persona="raider", mem=True):
        a = Agent(name, PRESETS[persona], "http://x", self.store, random.Random(1), log_dir=str(self.tmp / "logs"), client=object(),
                  memory_dir=str(self.tmp / "memory") if mem else None)
        if llm:
            a.llm = L.make_llm(llm, log_dir=str(self.tmp / "logs")) if isinstance(llm, str) else llm
            self.clients.append(a.llm)
        a.board = StubBoard()
        a.now = 1000.0
        a.memory.social["posted"] = []
        return a

    def events(self, a, kind):
        p = a.log_path
        return [e for e in (json.loads(x) for x in p.read_text().splitlines()) if e["event"] == kind] if p.exists() else []

    def wait_for(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def test_off_changes_nothing(self):
        a = self.agent(llm=None)
        data = {"loot": 5}
        self.assertEqual(a._voice_text("WARNING", "Robbed of 5.", data), "Robbed of 5.")
        a.log("battle", role="defender", loot=500, my_loss=1)
        self.assertEqual(a.memory.voice, {})
        self.assertNotIn("voice", a.memory.to_dict())
        self.assertFalse(self.events(a, "diary"))

    def test_post_text_is_rewritten_data_untouched(self):
        a = self.agent()
        data = {"victim_at": "1:2:3", "loot": 12500}
        before = json.dumps(data, sort_keys=True)
        a.voice_grace = 3.0
        sent = a._board_post("WARNING", "Robbed of 12,500 at 1:2:3. Watch your stores.", data)
        self.assertIsNotNone(sent)
        kind, text, d = a.board.posted[0]
        self.assertNotEqual(text, "Robbed of 12,500 at 1:2:3. Watch your stores.")
        self.assertIn("12,500 at 1:2:3", text)
        self.assertEqual(json.dumps(d, sort_keys=True), before)
        self.assertEqual(self.events(a, "voice_post")[0]["kind"], "WARNING")

    def test_unwanted_kinds_and_bad_rewrites_keep_the_original(self):
        a = self.agent()
        a.voice_grace = 3.0
        self.assertEqual(a._voice_text("VERDICT", "Confirmed 5.", {}), "Confirmed 5.")
        self.assertEqual(len(self.clients[0]._sent), 0)

        class Liar:
            enabled = True

            def submit(self, agent, purpose, system, prompt, cb, prio=5):
                cb("Robbed of 99 at 1:2:3, **hard**")
                return True

            def close(self):
                pass

        b = self.agent(llm=Liar(), name="agent0002")
        b.voice_grace = 1.0
        self.assertEqual(b._voice_text("WARNING", "Robbed of 5 at 1:2:3.", {"loot": 5}), "Robbed of 5 at 1:2:3.")

    def test_one_voice_request_per_agent_per_five_minutes(self):
        a = self.agent()
        a.voice_grace = 3.0
        clock = [1000.0]
        a.voice_clock = lambda: clock[0]
        first = a._voice_text("TAUNT", "Mine is bigger, 3 to 1.", {})
        self.assertNotEqual(first, "Mine is bigger, 3 to 1.")
        self.assertEqual(a._voice_text("TAUNT", "Another taunt, 4 times.", {}), "Another taunt, 4 times.")
        clock[0] += 301
        self.assertNotEqual(a._voice_text("TAUNT", "Another taunt, 4 times.", {}), "Another taunt, 4 times.")

    def test_a_late_rewrite_is_kept_for_the_retry(self):
        s = FakeLM(delay=0.4, answer="Robbed again, 5 stolen at 1:2:3, stay sharp.")
        try:
            a = self.agent(llm=L.LLMClient(s.url, "m", timeout=5, log_dir=str(self.tmp / "logs")))
            a.voice_grace = 0.05                                      # does not wait for the slow model
            t0 = time.time()
            self.assertEqual(a._voice_text("WARNING", "Robbed of 5 at 1:2:3.", {"loot": 5}), "Robbed of 5 at 1:2:3.")
            self.assertLess(time.time() - t0, 0.3)                    # the round was not held up
            self.assertTrue(self.wait_for(lambda: a._voice_runtime()["ready"]))
            self.assertEqual(a._voice_text("WARNING", "Robbed of 5 at 1:2:3.", {"loot": 5}), "Robbed again, 5 stolen at 1:2:3, stay sharp.")
        finally:
            s.close()

    def test_diary_after_notable_events_keeps_twenty_and_is_logged(self):
        a = self.agent()
        a.voice_every = 0.0
        a.log("build", what="metal_mine")                             # not notable
        a.log("battle", role="defender", loot=9000, my_loss=100, at="1:1:1")
        self.assertTrue(self.wait_for(lambda: len(self.events(a, "diary")) == 1))
        e = self.events(a, "diary")[0]
        self.assertIn("robbed", e["text"].lower())
        for i in range(25):
            a.log("colony_founded", planets=i)
            self.wait_for(lambda: len(a.memory.voice["diary"]) >= min(20, i + 2), 1)
        self.assertTrue(self.wait_for(lambda: len(self.events(a, "diary")) >= 15))
        self.assertLessEqual(len(a.memory.voice["diary"]), 20)

    def test_diary_waits_for_the_five_minute_slot(self):
        a = self.agent()
        clock = [1000.0]
        a.voice_clock = lambda: clock[0]
        a.log("colony_founded", planets=2)
        self.assertTrue(self.wait_for(lambda: len(self.events(a, "diary")) == 1))
        a.log("fleet_saved", ships={})
        time.sleep(0.2)
        self.assertEqual(len(self.events(a, "diary")), 1)             # held back
        clock[0] += 301
        a.log("build", what="x")                                      # any later log line gives it its turn
        self.assertTrue(self.wait_for(lambda: len(self.events(a, "diary")) == 2))

    def test_notable_events(self):
        from swarm.llm_agent import notable
        self.assertIsNotNone(notable("expedition_result", {"outcome": "loss"}))
        self.assertIsNotNone(notable("battle", {"role": "attacker", "loot": 50000, "verdict": "good"}))
        self.assertIsNone(notable("battle", {"role": "attacker", "loot": 100, "verdict": "good"}))
        self.assertIsNone(notable("battle", {"role": "defender", "loot": 0}))
        self.assertIsNotNone(notable("trade_offer", {"action": "done", "with": "agent2"}))
        self.assertIsNone(notable("trade_offer", {"action": "posted"}))

    def test_memory_round_trip_and_old_files(self):
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).voice, {})
        a = self.agent()
        a.voice_every = 0.0
        a.log("colony_founded", planets=2)
        self.assertTrue(self.wait_for(lambda: a.memory.voice.get("diary")))
        a.save_memory()
        b = self.agent(name="agent0001", llm=None)
        self.assertEqual(b.memory.voice["sheet"], a.memory.voice["sheet"])
        self.assertEqual(b.memory.voice["diary"], a.memory.voice["diary"])
        for junk in ({"voice": 5}, {"voice": {"diary": "x", "sheet": 3}}, {"voice": {"diary": [1, {"text": "ok"}]}}):
            m = Memory.from_dict(junk)
            self.assertIsInstance(m.voice if m.voice else {}, dict)
        self.assertEqual(Memory.from_dict({"voice": {"diary": [1, {"text": "ok"}]}}).voice["diary"], [{"text": "ok"}])


if __name__ == "__main__":
    unittest.main()
