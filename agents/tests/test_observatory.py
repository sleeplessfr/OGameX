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


class FrontPageTests(unittest.TestCase):
    def ev(self, k, t=T, agent="a1", **kw):
        return {"t": t, "agent": agent, "event": k, **kw}

    def test_a_big_bounty_beats_a_colony_and_a_small_raid(self):
        events = [self.ev("colony_founded", planets=2),
                  self.ev("battle", role="attacker", at="1:3:4", my_loss=0, their_loss=1000, loot=25000, verdict="good"),
                  self.ev("bounty_paid", side="poster", to="a2", resources={"metal": 900_000})]
        paper = ob.build_newspaper(events)
        self.assertEqual(paper["headline"]["kind"], "bounty")
        self.assertIn("900,000 metal", paper["headline"]["text"])
        self.assertEqual(paper["headline"]["kicker"], "BOUNTY")

    def test_the_posted_reward_counts_when_there_is_no_value(self):
        paper = ob.build_newspaper([self.ev("bounty_post", target="1:2:3", reward={"metal": 700_000}),
                                    self.ev("colony_founded", planets=2)])
        self.assertEqual(paper["headline"]["kind"], "bounty")

    def test_fleet_saved_and_completed_trade_are_stories_with_templates(self):
        events = [self.ev("fleet_saved", ships={"cruiser": 40, "battleship": 2}, value=900_000, attacker="1:9:9"),
                  self.ev("trade_received", agent="a2", partner="a3", resources={"crystal": 5000}, value=7500)]
        paper = ob.build_newspaper(events)
        self.assertEqual(paper["headline"]["kind"], "fleet")
        self.assertIn("42 ships", paper["headline"]["text"])
        self.assertIn("1:9:9", paper["headline"]["text"])
        (second,) = paper["secondary"]
        self.assertEqual(second["kind"], "trade")
        self.assertIn("5,000 crystal", second["text"])
        self.assertIn("a3", second["title"])

    def test_the_largest_robbery_leads(self):
        events = [self.ev("battle", role="defender", my_loss=10, their_loss=0, loot=5000, rounds=3),
                  self.ev("battle", agent="a2", role="defender", my_loss=10, their_loss=0, loot=800_000, rounds=3)]
        head = ob.build_newspaper(events)["headline"]
        self.assertEqual((head["kind"], head["title"]), ("defence", "a2 is robbed"))
        self.assertIn("800,000", head["text"])

    def test_secondary_stories_are_at_most_five_and_vary_in_kind(self):
        events = [self.ev("battle", t=T + i, agent=f"r{i}", role="attacker", at="1:1:1", my_loss=0, their_loss=0, loot=100_000 - i) for i in range(8)]
        events += [self.ev("colony_founded", planets=2, t=T + 20), self.ev("fleet_saved", ships={"x": 1}, value=1000, attacker="1:1:1", t=T + 21)]
        paper = ob.build_newspaper(events)
        kinds = [a["kind"] for a in paper["secondary"]]
        self.assertEqual(len(kinds), 5)
        self.assertIn("colony", kinds + [paper["headline"]["kind"]])
        self.assertIn("fleet", kinds)
        shown = [paper["headline"]] + paper["secondary"] + paper["articles"]
        self.assertEqual(len({a["title"] for a in shown}), len(shown))     # nothing is told twice

    def test_front_page_is_stable_and_the_quiet_page_has_no_secondary(self):
        arts = [dict(t=T, kind="trade", score=5, title="b", text=""), dict(t=T, kind="trade", score=5, title="a", text="")]
        self.assertEqual(ob.front_page(arts), ob.front_page(list(reversed(arts))))
        quiet = ob.build_newspaper([])
        self.assertEqual((quiet["secondary"], quiet["headline"]["kicker"]), ([], "QUIET"))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        make_logs(self.dir)
        (self.dir / "state.json").write_text(json.dumps({"agent0001": {"token": "tok", "persona": "raider"}}))

    def obs(self, api):
        o = ob.Observatory(self.dir, self.dir / "state.json", api)
        o.universe._sleep = lambda s: None
        return o

    def test_leaderboard_has_all_four_rankings_alliance_and_ships(self):
        class Api(FakeApi):
            def get(self, path):
                data = super().get(path)
                if path.startswith("/highscore"):
                    kind = path.split("type=")[1].split("&")[0]
                    players = [dict(p, alliance_tag="ABC" if p["username"] == "Prophet Gaspra" else None) for p in data["players"]]
                    if kind == "military":
                        players = [dict(p, rank=3 - p["rank"], score=7, total_ships=40) for p in players]
                    return {"players": players}
                return data
        o = self.obs(Api())
        o.ingest()
        o.scan()
        row = next(r for r in o.state()["leaderboard"]["rows"] if r["username"] == "Prophet Gaspra")
        self.assertEqual(row["alliance"], "ABC")
        self.assertEqual(row["rankings"]["military"], {"rank": 2, "score": 7})
        self.assertEqual(row["rankings"]["economy"]["rank"], 1)
        self.assertEqual(row["ships"], 40)

    def test_the_other_rankings_are_not_asked_for_every_time(self):
        api = FakeApi()
        o = self.obs(api)
        o.ingest()
        o.scan()
        o.scan()
        asked = [p for p in api.paths if "type=military" in p]
        self.assertEqual(len(asked), 1)

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


class BoardTabTests(unittest.TestCase):
    POSTS = [
        {"id": 1, "t": T, "expires": T + 3600, "agent": "agent0001", "kind": "WARNING", "text": "raider seen near 1:3", "data": {}},
        {"id": 2, "t": T + 5, "expires": T + 3600, "agent": "agent0002", "kind": "OFFER", "text": "10k metal for crystal", "data": {}},
        {"id": 3, "t": T + 9, "expires": T + 3600, "agent": "agent0001", "kind": "TAUNT", "text": "who is next?", "data": {}},
        {"id": 4, "t": T + 9, "expires": T + 3600, "agent": "agent0003", "kind": "TAUNT", "text": "<script>x</script>", "data": {}},
    ]

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        make_logs(self.dir)

    def obs(self, board):
        return ob.Observatory(self.dir, self.dir / "none.json", None, board=board)

    def reader(self, fetch):
        return ob.BoardReader("http://localhost:1", fetch=fetch, clock=lambda: 10 ** 9, ttl=0)

    def test_state_has_the_board_and_the_newspaper_section(self):
        paths = []

        def fetch(path):
            paths.append(path)
            return {"now": T + 20, "last_id": 4, "posts": self.POSTS}
        o = self.obs(self.reader(fetch))
        o.scan()
        s = o.state()
        json.dumps(s)
        self.assertEqual(paths, ["/posts?limit=200"])           # a plain GET, nothing else
        self.assertTrue(s["board"]["available"])
        self.assertEqual(s["board"]["counts"], {"WARNING": 1, "OFFER": 1, "TAUNT": 2})
        self.assertEqual(s["board"]["posts"][0]["t"], T + 9)    # newest first
        kinds = [x["kind"] for x in s["newspaper"]["board"]]
        self.assertEqual(kinds[:3], ["WARNING", "OFFER", "TAUNT"])  # most interesting kinds first
        self.assertEqual(len(kinds), 4)

    def test_without_a_board_the_tab_says_so(self):
        o = self.obs(None)
        o.scan()
        s = o.state()
        self.assertFalse(s["board"]["enabled"])
        self.assertEqual(s["board"]["posts"], [])
        self.assertEqual(s["newspaper"]["board"], [])
        self.assertIn("--board", s["board"]["note"])

    def test_a_dead_or_garbage_board_does_not_break_anything(self):
        for fetch in (lambda p: (_ for _ in ()).throw(OSError("refused")), lambda p: {"nope": 1}, lambda p: "text"):
            o = self.obs(self.reader(fetch))
            o.scan()
            s = o.state()
            self.assertTrue(s["board"]["enabled"])
            self.assertFalse(s["board"]["available"])
            self.assertIn("cannot be reached", s["board"]["note"])
            self.assertIn("colony", s["newspaper"]["headline"]["title"])
            self.assertTrue(s["feed"])

    def test_the_board_is_not_asked_every_round(self):
        calls = []
        now = [0.0]
        r = ob.BoardReader("http://localhost:1", fetch=lambda p: calls.append(p) or {"posts": []}, clock=lambda: now[0], ttl=20)
        r.refresh()
        r.refresh()
        now[0] = 21
        r.refresh()
        self.assertEqual(len(calls), 2)

    def test_reads_a_real_board_and_never_writes(self):
        import board as B
        board = B.Board(None)
        board.post("agent0001", "WARNING", "raider near 1:3", {"at": "1:3:4"})
        server = B.serve(board, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            o = self.obs(ob.BoardReader(f"http://127.0.0.1:{server.server_address[1]}", ttl=0))
            o.scan()
            self.assertEqual([p["kind"] for p in o.state()["board"]["posts"]], ["WARNING"])
            self.assertEqual(len(board.read()), 1)                 # still just the one post
            self.assertFalse(hasattr(ob.BoardReader, "post"))
        finally:
            server.shutdown()
            server.server_close()

    def test_new_events_are_described_in_the_feed_and_cheating_is_news(self):
        e = {"t": T, "agent": "a1", "event": "trade_cheated", "partner": "a2", "owed": 40000}
        self.assertIn("cheated by a2", ob.describe(e))
        self.assertIn("a2", ob.build_newspaper([e])["headline"]["title"])
        for ev in ({"event": "board_post", "kind": "WARNING", "text": "x"}, {"event": "board_read", "posts": 3, "guard": True},
                   {"event": "trade_offer", "give": {"metal": 5}, "want": {"crystal": 3}}, {"event": "trade_sent", "to": "a2", "resources": {"metal": 5}, "instalment": "1/4"},
                   {"event": "trade_received", "partner": "a2", "resources": {"crystal": 3}}):
            self.assertTrue(ob.describe({"t": T, "agent": "a1", **ev}))


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
            self.assertIn("From the galaxy board", html)
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
