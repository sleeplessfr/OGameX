import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import observatory as ob
from swarm.client import ApiError

T = 1_700_000_000


def line(**kw):
    return json.dumps(kw) + "\n"


class FakeApi:
    """Stands in for the game: a highscore and a galaxy view. Records every path asked for."""

    def __init__(self, fail=None):
        self.paths, self.fail = [], fail

    def get(self, path):
        self.paths.append(path)
        if self.fail:
            raise self.fail
        if path.startswith("/highscore"):
            return {"players": [
                {"rank": 1, "user_id": 5, "username": "Prophet Gaspra", "score": 900, "main_planet": {"galaxy": 1, "system": 3, "position": 8}},
                {"rank": 2, "user_id": 6, "username": "Human Hank", "score": 100, "main_planet": {"galaxy": 1, "system": 9, "position": 4}}]}
        if path.startswith("/galaxy/"):
            s = int(path.rsplit("/", 1)[1])
            positions = []
            if s == 3:
                positions = [{"position": 8, "planet": {"id": 1, "name": "Home", "owner": {"username": "Prophet Gaspra"}, "has_moon": False}},
                             {"position": 10, "planet": {"id": 2, "name": "Colony", "owner": {"username": "Prophet Gaspra"}, "has_moon": True}}]
            return {"galaxy": 1, "system": s, "positions": positions}
        raise ApiError(404, "not_found", path)


def make_logs(folder: Path):
    (folder / "agent0001.jsonl").write_text(
        line(t=T, agent="agent0001", event="registered", player="Prophet Gaspra", persona="raider")
        + line(t=T + 1, agent="agent0001", event="class", chosen="general")
        + line(t=T + 10, agent="agent0001", event="colony_sent", at="1:3:10", why="x", seconds=60)
        + line(t=T + 20, agent="agent0001", event="colony_founded", planets=2)
        + line(t=T + 30, agent="agent0001", event="battle", at="1:3:8", role="attacker", my_loss=60000, their_loss=2000, loot=0, verdict="bad", rounds=6)
        + line(t=T + 31, agent="agent0001", event="wait", why="saving"))


class LogReaderTests(unittest.TestCase):
    def test_tolerates_garbage_half_lines_and_growth(self):
        d = Path(tempfile.mkdtemp())
        f = d / "a.jsonl"
        f.write_bytes(b'not json\n{"t": 1, "agent": "a", "event": "build"}\n{"t": 2, "agent"')
        tail = ob.LogTail(d)
        self.assertEqual([e["t"] for e in tail.poll()], [1])          # the half line waits
        with f.open("ab") as h:
            h.write(b': "a", "event": "spy"}\n\xff\xfe broken\n')
        self.assertEqual([e["event"] for e in tail.poll()], ["spy"])  # completed, the junk skipped
        self.assertEqual(tail.poll(), [])                               # nothing new: nothing read twice
        f.write_text(line(t=9, agent="a", event="trade"))               # the file was replaced
        self.assertEqual([e["event"] for e in tail.poll()], ["trade"])

    def test_missing_folder_is_fine(self):
        self.assertEqual(ob.LogTail("/does/not/exist").poll(), [])


class NewspaperTests(unittest.TestCase):
    def ev(self, k, **kw):
        return {"t": T, "agent": "a1", "event": k, **kw}

    def test_biggest_loss_makes_the_headline(self):
        events = [self.ev("colony_founded", planets=2), self.ev("class", chosen="general"),
                  self.ev("battle", role="attacker", at="1:4:9", my_loss=2_060_000, their_loss=582_000, loot=0, verdict="bad")]
        paper = ob.build_newspaper(events)
        self.assertIn("Disaster", paper["headline"]["title"])
        self.assertIn("2,060,000", paper["headline"]["text"])

    def test_a_colony_beats_a_class_choice(self):
        paper = ob.build_newspaper([self.ev("class", chosen="general"), self.ev("colony_founded", planets=2)])
        self.assertIn("colony", paper["headline"]["title"])

    def test_quiet_universe_and_empty_logs(self):
        self.assertEqual(ob.build_newspaper([])["headline"]["kind"], "quiet")
        self.assertEqual(ob.build_newspaper([self.ev("build", what="metal_mine")])["headline"]["kind"], "quiet")

    def test_old_news_is_dropped_and_the_same_fight_is_not_told_twice(self):
        old = {"t": T - 10 * 3600, "agent": "a1", "event": "colony_founded", "planets": 2}
        fight_a = {"t": T, "agent": "a1", "event": "battle", "role": "attacker", "at": "1:3:4", "my_loss": 0, "their_loss": 1000, "loot": 25000, "verdict": "good"}
        fight_d = {"t": T + 2, "agent": "a2", "event": "battle", "role": "defender", "my_loss": 1000, "their_loss": 0, "loot": 25000, "rounds": 3}
        paper = ob.build_newspaper([old, fight_a, fight_d], hours=6)
        titles = [paper["headline"]["title"]] + [a["title"] for a in paper["articles"]]
        self.assertEqual(len(titles), 1)
        self.assertNotIn("colony", titles[0])

    def test_the_wording_is_the_same_every_time(self):
        events = [self.ev("battle", role="attacker", at="1:3:4", my_loss=5, their_loss=10, loot=500, verdict="good")]
        self.assertEqual(ob.build_newspaper(events), ob.build_newspaper(events))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        make_logs(self.dir)
        (self.dir / "state.json").write_text(json.dumps({"agent0001": {"token": "tok", "persona": "raider"}}))

    def obs(self, api):
        o = ob.Observatory(self.dir, self.dir / "state.json", api)
        o.universe._sleep = lambda s: None
        return o

    def test_state_has_every_section(self):
        api = FakeApi()
        o = self.obs(api)
        o.ingest()
        o.scan()
        s = o.state()
        for key in ("agents", "map", "newspaper", "feed", "leaderboard", "kinds", "personas", "refresh"):
            self.assertIn(key, s)
        json.dumps(s)    # serialisable
        self.assertEqual(s["agents"]["agent0001"]["class"], "general")
        self.assertEqual(s["agents"]["agent0001"]["colonies"], 1)
        self.assertEqual(s["agents"]["agent0001"]["net_loot"], -58000)
        self.assertNotIn("wait", [f["kind"] for f in s["feed"]])
        sys3 = next(x for x in s["map"]["systems"] if x["system"] == 3)
        by_pos = {x["position"]: x for x in sys3["slots"]}
        self.assertEqual(by_pos[8]["persona"], "raider")
        self.assertFalse(by_pos[8]["colony"])
        self.assertTrue(by_pos[10]["colony"])       # the colony ship went there, and it is not the main planet
        self.assertTrue(by_pos[8]["battle"])         # a fight there in the last 6 hours
        self.assertFalse(by_pos[10]["battle"])
        self.assertEqual(s["leaderboard"]["rows"][0]["agent"], "agent0001")
        self.assertIsNone(s["leaderboard"]["rows"][1]["agent"])    # a human: not one of ours
        self.assertIn("colony", s["newspaper"]["headline"]["title"])      # 300,000 of news beats a 60,000 loss

    def test_only_reads_and_stays_within_budget(self):
        api = FakeApi()
        o = self.obs(api)
        o.scan()
        self.assertTrue(all(p.startswith(("/highscore", "/galaxy/")) for p in api.paths))
        self.assertLessEqual(len([p for p in api.paths if p.startswith("/galaxy/")]), ob.SCAN_BUDGET)
        self.assertFalse(hasattr(ob.ReadOnlyClient("http://x", "t"), "post"))

    def test_game_down_or_no_token_does_not_break_the_rest(self):
        for client in (FakeApi(fail=ApiError(502, "bad_response", "down")), None, FakeApi(fail=ApiError(401, "unauthenticated", "no"))):
            o = self.obs(client)
            o.scan()
            s = o.state()
            self.assertFalse(s["map"]["available"])
            self.assertTrue(s["map"]["note"])
            self.assertTrue(s["feed"])
            self.assertIn("colony", s["newspaper"]["headline"]["title"])
            self.assertFalse(s["leaderboard"]["available"])

    def test_token_is_read_without_touching_the_file(self):
        p = self.dir / "state.json"
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        token, personas = ob.token_from_state(p)
        self.assertEqual((token, personas), ("tok", {"agent0001": "raider"}))
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        self.assertEqual(ob.token_from_state(self.dir / "missing.json"), (None, {}))


class ServerTests(unittest.TestCase):
    def test_serves_the_page_and_the_state_and_refuses_writes(self):
        d = Path(tempfile.mkdtemp())
        make_logs(d)
        o = ob.Observatory(d, d / "none.json", None)
        server = ob.serve(o, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            html = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("The Daily Ogame", html)
            self.assertNotIn("http://", html.replace("http://localhost", ""))     # nothing loaded from outside: works offline
            state = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertIn("newspaper", state)
            self.assertEqual(state["agents"]["agent0001"]["persona"], "raider")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"{}", method="POST"))
            self.assertEqual(cm.exception.code, 405)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/nothing")
            self.assertEqual(cm.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
