import contextlib
import io
import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import bench
from swarm import llm as L
from swarm import multiproc as M
from swarm.agent import Agent, TokenStore
from swarm.personas import PRESETS
from swarm.routine import DAY, Routine, make_routine
from tests.fakegame import FakeGame

NAMES = [f"agent{i:04d}" for i in range(1, 301)]


class RoutineTests(unittest.TestCase):
    def test_off_is_none(self):
        self.assertIsNone(make_routine("off", "a", "miner"))
        self.assertIsNone(make_routine(None, "a", "miner"))
        with self.assertRaises(ValueError):
            Routine("a", "miner", "bogus")

    def test_schedule_is_deterministic_and_does_not_use_global_random(self):
        random.seed(1)
        a = Routine("agent0007", "raider", "real").windows(3)
        random.seed(2)
        b = Routine("agent0007", "raider", "real").windows(3)
        self.assertEqual(a, b)
        self.assertNotEqual(a, Routine("agent0008", "raider", "real").windows(3))
        self.assertNotEqual(Routine("agent0007", "raider", "real").windows(3), Routine("agent0007", "raider", "real").windows(4))

    def test_does_not_touch_the_persona_rng(self):
        r1, r2 = random.Random(5), random.Random(5)
        Routine("x", "miner", "real").windows(0)
        self.assertEqual(r1.random(), r2.random())

    def test_windows_never_overlap_and_stay_in_the_day(self):
        for mode in ("light", "real"):
            for name in NAMES[:80]:
                for persona in ("miner", "raider", "scientist", "balanced"):
                    for day in range(3):
                        w = Routine(name, persona, mode).windows(day)
                        for s, e in w:
                            self.assertTrue(0 <= s < e <= DAY)
                        for (s1, e1), (s2, e2) in zip(w, w[1:]):
                            self.assertLess(e1, s2)

    def test_session_counts_and_lengths_in_real_mode(self):
        for name in NAMES[:100]:
            w = Routine(name, "balanced", "real").windows(0)
            self.assertLessEqual(len(w), 5)
            for s, e in w:
                self.assertLessEqual(e - s, 40 * 60 + 1)

    def test_share_online(self):
        for mode, lo, hi in (("real", 0.04, 0.16), ("light", 0.35, 0.65)):
            shares = [Routine(n, p, mode).share_online(7) for n in NAMES[:150] for p in ("miner", "raider", "balanced")]
            mean = sum(shares) / len(shares)
            self.assertTrue(lo < mean < hi, f"{mode}: {mean}")

    def test_share_of_the_swarm_online_at_one_moment(self):
        t0 = 1_700_000_000.0
        for mode, lo, hi in (("real", 0.03, 0.20), ("light", 0.3, 0.7)):
            rs = [Routine(n, "balanced", mode) for n in NAMES]
            for t in (t0, t0 + 6 * 3600, t0 + 13 * 3600):
                share = sum(r.online(t) for r in rs) / len(rs)
                self.assertTrue(lo < share < hi, f"{mode} {t - t0}: {share}")

    def test_miners_check_in_more_often_than_scientists(self):
        miner = sum(len(Routine(n, "miner", "real").windows(0)) for n in NAMES)
        sci = sum(len(Routine(n, "scientist", "real").windows(0)) for n in NAMES)
        self.assertGreater(miner, sci)

    def test_raiders_play_in_the_evening(self):
        def evening(persona):
            n = tot = 0
            for name in NAMES:
                r = Routine(name, persona, "real")
                for s, e in r.windows(0):
                    tot += 1
                    n += 17 * 3600 <= s < 24 * 3600
            return n / tot
        self.assertGreater(evening("raider"), evening("balanced"))

    def test_timezone_offsets_spread_the_swarm(self):
        self.assertGreater(len({Routine(n, "balanced", "real").tz for n in NAMES}), 12)

    def test_online_agrees_with_windows_and_waits_are_right(self):
        r = Routine("agent0003", "balanced", "real", 1.0)
        s, e = r.windows(0)[0]
        base = -r.tz * 3600.0            # real time at which local midnight of day 0 starts
        self.assertTrue(r.online(base + s + 1))
        self.assertFalse(r.online(base + e + 1))
        self.assertAlmostEqual(r.session_end(base + s + 1), base + e, places=3)
        self.assertEqual(r.seconds_until_online(base + s + 1), 0.0)
        gap = r.seconds_until_online(base + e + 1)
        self.assertGreater(gap, 0)
        self.assertTrue(r.online(base + e + 1 + gap + 0.5))
        self.assertFalse(r.online(base + e + 1 + gap - 5))

    def test_clip_cuts_a_pause_at_the_end_of_the_session(self):
        r = Routine("agent0003", "balanced", "real", 1.0)
        s, e = r.windows(0)[0]
        now = -r.tz * 3600.0 + e - 100
        self.assertAlmostEqual(r.clip(now, 3000), 100.01, places=2)
        self.assertEqual(r.clip(now, 50), 50)

    def test_time_scale_compresses_days(self):
        slow = Routine("agent0009", "balanced", "real", 1.0)
        fast = Routine("agent0009", "balanced", "real", 24.0)
        # the same schedule, seen 24 times faster: what is real-time t for the slow one is t/24 for the fast one
        for t in range(0, 20 * 3600, 1700):
            self.assertEqual(slow.online(float(t)), fast.online(t / 24.0))
        # a whole routine day passes in one real hour
        hours = sum(fast.online(i * 10.0) for i in range(360)) * 10.0
        self.assertAlmostEqual(hours / 3600.0, fast.share_online(1, 0) * 1.0, delta=0.12)
        self.assertAlmostEqual(fast.seconds_until_online(0.0) * 24, slow.seconds_until_online(0.0), delta=1e-3 * 24 + 1)


class AgentRoutineTests(unittest.TestCase):
    def make(self, routine, clock):
        game = FakeGame()
        game.token = "fake"        # logged in already: the account steps are not what is tested here
        tmp = tempfile.mkdtemp()
        agent = Agent("sim", PRESETS["balanced"], "http://x", TokenStore(str(Path(tmp) / "t.json")), random.Random(1), log_dir=tmp, client=game)
        events = []
        agent.log = lambda event, **f: events.append(event)
        agent.routine = routine
        agent.routine_clock = clock
        return game, agent, events

    def test_offline_agent_makes_no_calls_and_online_agent_plays(self):
        r = Routine("agent0003", "balanced", "real", 1.0)
        s, e = r.windows(0)[0]
        base = -r.tz * 3600.0
        t = [base + e + 10]       # offline
        game, agent, events = self.make(r, lambda: t[0])
        calls = []
        orig = game.get
        game.get = lambda p: (calls.append(p), orig(p))[1]
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        agent.ROUTINE_CHUNK = 0.05
        agent.run(stop, speedup=1.0)
        self.assertEqual(calls, [])
        self.assertEqual(events.count("offline"), 1)       # once, not at every look
        # now it is online: one round, then back offline in the same loop because the pause is cut at the session end
        t[0] = base + s + 5
        stop2 = threading.Event()
        agent.run(stop2, max_ticks=1)
        self.assertIn("/status", calls)
        self.assertIn("online", events)

    def test_default_agent_is_unchanged(self):
        game, agent, events = self.make(None, time.time)
        agent.run(threading.Event(), max_ticks=1)
        self.assertEqual(agent.stats["ticks"], 1)
        self.assertNotIn("online", events)

    def test_stop_file_works_while_offline(self):
        r = Routine("agent0003", "balanced", "real", 1.0)
        s, e = r.windows(0)[0]
        game, agent, events = self.make(r, lambda: -r.tz * 3600.0 + e + 10)
        agent.ROUTINE_CHUNK = 0.05
        tmp = Path(tempfile.mkdtemp()) / "STOP"
        tmp.write_text("x")
        done = threading.Event()
        threading.Thread(target=lambda: (agent.run(threading.Event(), stop_file=str(tmp)), done.set()), daemon=True).start()
        self.assertTrue(done.wait(3))
        self.assertIn("stop_file_found", events)


class QuotaTests(unittest.TestCase):
    def test_per_agent_quota_drops_and_logs(self):
        tmp = tempfile.mkdtemp()
        now = [1000.0]
        c = L.LLMClient("fake", log_dir=tmp, per_agent_hour=2, clock=lambda: now[0])
        got = [c.ask("chatty", "diary", "TONE: peaceful", "EVENT: x", wait=3) for _ in range(3)]
        self.assertTrue(got[0] and got[1])
        self.assertIsNone(got[2])
        self.assertTrue(c.ask("quiet", "diary", "TONE: peaceful", "EVENT: y", wait=3))      # others are not affected
        self.assertEqual(c.counts["quota"], 1)
        lines = [json.loads(x) for x in (Path(tmp) / "llm.jsonl").read_text().splitlines()]
        self.assertTrue(any("quota" in (x.get("error") or "") and x["agent"] == "chatty" for x in lines))
        now[0] += 3601                                                                      # an hour later the quota is fresh
        self.assertTrue(c.ask("chatty", "diary", "TONE: peaceful", "EVENT: z", wait=3))
        c.close()

    def test_quota_off_by_default(self):
        c = L.LLMClient("fake", log_dir=None)
        self.assertTrue(all(c.ask("a", "diary", "TONE: x", "EVENT: y", wait=3) for _ in range(5)))
        c.close()


class VoicedTests(unittest.TestCase):
    def make(self, voiced):
        tmp = tempfile.mkdtemp()
        agent = Agent("sim", PRESETS["balanced"], "http://x", TokenStore(str(Path(tmp) / "t.json")), random.Random(1), log_dir=tmp, client=FakeGame())
        agent.voiced = voiced
        agent.llm = L.LLMClient("fake", log_dir=None)
        return agent

    def test_unvoiced_agent_gets_no_diary_and_no_rewrite(self):
        a = self.make(False)
        self.assertFalse(a._voice_on())
        self.assertEqual(a._voice_text("WARNING", "Raid at 1:2:3", {}), "Raid at 1:2:3")
        a._voice_notice("colony_founded", {"planets": 2})
        self.assertEqual(a.llm.counts["ok"], 0)
        a.llm.close()

    def test_voiced_agent_still_uses_the_model_and_diplomacy_answers_for_everybody(self):
        a = self.make(True)
        self.assertTrue(a._voice_on() and a._diplo_voiced())
        a.llm.close()
        b = self.make(False)
        self.assertTrue(b._diplo_on())          # answers go through the model for every agent
        self.assertFalse(b._diplo_voiced())     # hello / announce use the template
        b.llm.close()


class MultiprocTests(unittest.TestCase):
    def test_slices_cover_all_numbers_once(self):
        for first, count, procs in ((1, 10, 3), (6, 5, 2), (1, 2, 5), (1, 100, 7), (1, 1, 1)):
            parts = M.slices(first, count, procs)
            nums = [n for f, c in parts for n in range(f, f + c)]
            self.assertEqual(nums, list(range(first, first + count)))
            self.assertLessEqual(max(c for _, c in parts) - min(c for _, c in parts), 1)
            self.assertLessEqual(len(parts), procs)

    def test_strip_options_handles_both_forms(self):
        argv = ["--count", "10", "--procs=4", "--seed", "3", "--max-rate", "8", "--first=5", "--llm", "fake", "--state", "x.json"]
        self.assertEqual(M.strip_options(argv), ["--seed", "3", "--llm", "fake"])

    def test_child_command(self):
        cmd = M.child_command("run_swarm.py", ["--count", "10", "--procs", "2", "--llm", "http://l/v1", "--board", "http://localhost:8765"],
                              2, 6, 5, "agents_state.json", 8.0, 120, 2, python="py")
        self.assertEqual(cmd[:2], ["py", "run_swarm.py"])
        self.assertIn("--llm", cmd)
        self.assertEqual(cmd[cmd.index("--first") + 1], "6")
        self.assertEqual(cmd[cmd.index("--count") + 1], "5")
        self.assertEqual(cmd[cmd.index("--state") + 1], "agents_state.json.p2")
        self.assertEqual(float(cmd[cmd.index("--max-rate") + 1]), 4.0)
        self.assertEqual(cmd[cmd.index("--llm-max-per-hour") + 1], "60")
        self.assertEqual(cmd[cmd.index("--procs") + 1], "1")
        self.assertEqual(cmd.count("--count"), 1)
        self.assertEqual(cmd[cmd.index("--board") + 1], "http://localhost:8765")

    def test_unlimited_rate_stays_unlimited(self):
        cmd = M.child_command("r.py", [], 1, 1, 5, "s.json", 0.0, 120, 3)
        self.assertEqual(cmd[cmd.index("--max-rate") + 1], "0")

    def test_state_seed_and_merge(self):
        d = Path(tempfile.mkdtemp())
        main = str(d / "s.json")
        Path(main).write_text(json.dumps({"agent0001": {"token": "a"}, "agent0002": {"token": "b"}, "agent0009": {"token": "z"}}))
        c1, c2 = M.state_path(main, 1), M.state_path(main, 2)
        self.assertEqual(M.seed_child_state(main, c1, ["agent0001", "agent0002", "agent0003"]), 2)
        self.assertEqual(M.seed_child_state(main, c2, ["agent0004"]), 0)
        self.assertFalse(Path(c2).exists())
        Path(c1).write_text(json.dumps({"agent0001": {"token": "new"}, "agent0002": {"token": "b"}}))
        Path(c2).write_text(json.dumps({"agent0004": {"token": "d"}}))
        self.assertEqual(M.merge_child_states(main, [c1, c2]), 3)
        merged = json.loads(Path(main).read_text())
        self.assertEqual(merged["agent0001"]["token"], "new")
        self.assertEqual(set(merged), {"agent0001", "agent0002", "agent0004", "agent0009"})

    def test_run_children_with_dummy_children(self):
        d = Path(tempfile.mkdtemp())
        script = d / "child.py"
        script.write_text("import sys, json\n"
                          "a = sys.argv; st = a[a.index('--state') + 1]; first = int(a[a.index('--first') + 1]); n = int(a[a.index('--count') + 1])\n"
                          "json.dump({f'agent{i:04d}': {'token': 't'} for i in range(first, first + n)}, open(st, 'w'))\n")
        args = SimpleNamespace(first=1, count=7, procs=3, state=str(d / "s.json"), prefix="agent", max_rate=6.0, llm_max_per_hour=120,
                               stop_file=str(d / "STOP"))
        self.assertEqual(M.run_children(args, ["--count", "7", "--procs", "3"], script=str(script), sleep=lambda s: time.sleep(0.01)), 0)
        self.assertEqual(len(json.loads(Path(args.state).read_text())), 7)


class BenchTests(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(bench.percentile([], 95), 0.0)

    def test_load_against_local_server_reads_only(self):
        d = tempfile.mkdtemp()
        with bench.FakeServer(latency=0.002) as srv:
            api = bench.Api(srv.url, gate=bench.RateGate(200))
            accounts = bench.ensure_accounts(api, str(Path(d) / "b.json"), 6, say=lambda *a: None)
            self.assertEqual(len(accounts), 6)
            self.assertEqual(set(json.loads((Path(d) / "b.json").read_text())), {f"bench000{i}" for i in range(1, 7)})
            # a second call reuses the accounts: nothing is registered again
            registered = sum(1 for m, p in srv.paths if m == "POST")
            self.assertEqual(len(bench.ensure_accounts(api, str(Path(d) / "b.json"), 6)), 6)
            self.assertEqual(sum(1 for m, p in srv.paths if m == "POST"), registered)
            stats = bench.Stats()
            elapsed = bench.run_load(api, accounts, 1.0, 0.0, 6, stats)
            text, summary = bench.report(stats, elapsed, 6, 200, 10)
            self.assertGreater(summary["requests"], 50)
            self.assertEqual(summary["n429"], 0)
            self.assertIn("VERDICT", text)
            self.assertIn("status", text)
            self.assertTrue(all(m == "GET" for m, p in srv.paths[registered:]))       # read-only after registering
            self.assertLessEqual(summary["rps"], 260)

    def test_pace_limits_each_agent(self):
        with bench.FakeServer(latency=0.0) as srv:
            api = bench.Api(srv.url)
            accts = [{"name": f"b{i}", "token": "t", "home": 10} for i in range(4)]
            stats = bench.Stats()
            bench.run_load(api, accts, 1.0, 0.25, 4, stats)
            self.assertLessEqual(stats.total(), 4 * 5 + 4)
            self.assertGreaterEqual(stats.total(), 8)

    def test_errors_and_429_are_counted_and_in_the_verdict(self):
        with bench.FakeServer(latency=0.0, fail=429, fail_share=0.5) as srv:
            api = bench.Api(srv.url, gate=bench.RateGate(300))
            accts = [{"name": f"b{i}", "token": "t", "home": 10} for i in range(4)]
            stats = bench.Stats()
            elapsed = bench.run_load(api, accts, 1.0, 0.0, 4, stats)
            text, summary = bench.report(stats, elapsed, 4, 300, 10)
            self.assertGreater(summary["n429"], 10)
            self.assertIn("429", text)
            self.assertIn("struggling", summary["verdict"])

    def test_injected_transport_and_dead_server(self):
        def dead(method, url, headers, body, timeout):
            raise OSError("down")
        api = bench.Api("http://nowhere/api/v1", dead)
        self.assertEqual(api.call("GET", "/status")[0], 0)
        self.assertEqual(bench.main(["--base-url", "http://nowhere/api/v1", "--seconds", "1"], transport=dead), 1)
        seen = []

        def canned(method, url, headers, body, timeout):
            seen.append((method, url))
            return 200, json.dumps({"success": True, "data": {"token": "t", "user": {"id": 1}, "player": {"current_planet_id": 3}}}).encode()
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bench.main(["--agents", "2", "--seconds", "0.3", "--max-rate", "0", "--state", str(Path(d) / "s.json")], transport=canned), 0)
        self.assertTrue(any(m == "POST" and u.endswith("/auth/register") for m, u in seen))

    def test_dry_run(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bench.main(["--dry-run", "--agents", "3", "--seconds", "0.5", "--max-rate", "100"]), 0)
        self.assertIn("VERDICT", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class SummaryTests(unittest.TestCase):
    def test_activity_section_shows_share_online(self):
        import summarize_logs as S
        ev = [{"t": 0, "agent": "a", "event": "online"}, {"t": 100, "agent": "a", "event": "offline"},
              {"t": 0, "agent": "b", "event": "offline"}, {"t": 50, "agent": "b", "event": "online"},
              {"t": 200, "agent": "c", "event": "offline"}]
        out = "\n".join(S.activity_section(ev, 0, 200))
        self.assertIn("1 of 3 agents with a routine are online", out)
        self.assertIn("2 sessions", out)
        self.assertEqual(S.activity_section([{"t": 1, "agent": "a", "event": "build"}], 0, 10), [])
