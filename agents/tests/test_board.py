import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import board as B
from swarm.boardclient import BoardClient, NullBoard, is_local_url, make_board


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class BoardRuleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = B.Board(None, clock=self.clock)

    def test_post_and_read_back_oldest_first_with_since_and_kind(self):
        a = self.b.post("agent0001", "warning", "raider near 1:3", {"at": "1:3:4"})
        self.clock.t += 1
        c = self.b.post("agent0002", "OFFER", "metal for crystal")
        self.assertEqual(a["kind"], "WARNING")
        self.assertEqual([p["id"] for p in self.b.read()], [a["id"], c["id"]])
        self.assertEqual([p["id"] for p in self.b.read(since=a["id"])], [c["id"]])
        self.assertEqual([p["id"] for p in self.b.read(kinds=["offer", "want"])], [c["id"]])
        self.assertEqual([p["id"] for p in self.b.read(agent="agent0001")], [a["id"]])
        self.assertLess(a["id"], c["id"])

    def test_limit_keeps_the_newest(self):
        ids = []
        for i in range(5):
            self.clock.t += 1
            ids.append(self.b.post("a", "TAUNT", f"t{i}")["id"])
        self.assertEqual([p["id"] for p in self.b.read(limit=2)], ids[-2:])

    def test_bad_posts_are_refused(self):
        bad = [("", "OFFER", "x", None), ("a", "SPAM", "x", None), ("a", "OFFER", "", None), ("a", "OFFER", "x" * 201, None),
               ("a b", "OFFER", "x", None), ("a", "OFFER", "x", [1]), ("a", "OFFER", "x", {"k": "v" * 700}), ("a", "OFFER", 5, None),
               ("a", "OFFER", "x", {"k": object()})]
        for agent, kind, text, data in bad:
            with self.assertRaises(B.PostRefused, msg=str((agent, kind, text))) as cm:
                self.b.post(agent, kind, text, data)
            self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.b.read(), [])

    def test_control_characters_are_removed(self):
        self.assertEqual(self.b.post("a", "TAUNT", "hi\x00\nthere")["text"], "hi  there")

    def test_posts_expire(self):
        self.b.post("a", "WARNING", "w")                 # 2 h
        self.b.post("a", "TAUNT", "t")                   # 30 min
        self.clock.t += 1900
        self.assertEqual([p["kind"] for p in self.b.read()], ["WARNING"])
        self.clock.t += 7200
        self.assertEqual(self.b.read(), [])
        self.assertEqual(self.b.health()["posts"], 0)

    def test_ttl_is_capped_and_has_a_floor(self):
        p = self.b.post("a", "TAUNT", "x", ttl=10 ** 9)
        self.assertEqual(p["expires"] - p["t"], B.MAX_TTL)
        q = self.b.post("a", "TAUNT", "y", ttl=1)
        self.assertEqual(q["expires"] - q["t"], 60)

    def test_rate_limit_per_agent_and_recovery(self):
        board = B.Board(None, clock=self.clock, per_agent_hour=3)
        for i in range(3):
            board.post("spammer", "TAUNT", f"m{i}")
        with self.assertRaises(B.PostRefused) as cm:
            board.post("spammer", "TAUNT", "m3")
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "rate_limited"))
        board.post("honest", "TAUNT", "hello")            # others are not affected
        self.clock.t += 3601
        board.post("spammer", "TAUNT", "m4")              # an hour later it may post again

    def test_global_cap_and_duplicates(self):
        board = B.Board(None, clock=self.clock, per_hour_all=2)
        board.post("a", "TAUNT", "x")
        with self.assertRaises(B.PostRefused) as cm:
            board.post("a", "TAUNT", "x")
        self.assertEqual(cm.exception.code, "duplicate")
        board.post("b", "TAUNT", "y")
        with self.assertRaises(B.PostRefused) as cm:
            board.post("c", "TAUNT", "z")
        self.assertEqual(cm.exception.code, "board_busy")

    def test_only_the_newest_posts_are_kept(self):
        board = B.Board(None, clock=self.clock, max_posts=3)
        for i in range(6):
            self.clock.t += 1
            board.post(f"a{i}", "TAUNT", f"t{i}")
        self.assertEqual([p["text"] for p in board.read()], ["t3", "t4", "t5"])


class PersistenceTests(unittest.TestCase):
    def test_posts_survive_a_restart_and_ids_keep_growing(self):
        path = Path(tempfile.mkdtemp()) / "board.jsonl"
        clock = Clock()
        first = B.Board(path, clock=clock)
        p1 = first.post("a", "OFFER", "one", {"x": 1})
        clock.t += 10
        again = B.Board(path, clock=clock)
        self.assertEqual([p["text"] for p in again.read()], ["one"])
        self.assertEqual(again.read()[0]["data"], {"x": 1})
        p2 = again.post("a", "OFFER", "two")
        self.assertGreater(p2["id"], p1["id"])

    def test_expired_and_damaged_lines_are_dropped_and_the_file_is_compacted(self):
        path = Path(tempfile.mkdtemp()) / "board.jsonl"
        clock = Clock()
        b = B.Board(path, clock=clock)
        b.post("a", "TAUNT", "old")
        clock.t += 5000
        b.post("a", "WARNING", "fresh")
        with path.open("a") as f:
            f.write("{broken\nnot json\n")
        clock.t += 100
        b2 = B.Board(path, clock=clock)
        self.assertEqual([p["text"] for p in b2.read()], ["fresh"])
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_unwritable_file_does_not_stop_the_board(self):
        import contextlib
        import io
        d = Path(tempfile.mkdtemp())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            b = B.Board(d, clock=Clock())          # a folder, not a file
            b.post("a", "TAUNT", "still works")
        self.assertEqual(len(b.read()), 1)
        self.assertIn("cannot write", err.getvalue())


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = B.Board(None, per_agent_hour=5)
        cls.server = B.serve(cls.board, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def raw(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_and_unknown_paths(self):
        status, body = self.raw("GET", "/health")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.raw("GET", "/nope")[0], 404)
        self.assertEqual(self.raw("PUT", "/post", b"{}")[0], 405)
        self.assertEqual(self.raw("DELETE", "/posts")[0], 405)

    def test_post_and_read_over_http(self):
        status, body = self.raw("POST", "/post", json.dumps({"agent": "h1", "kind": "BOUNTY", "text": "kill X", "data": {"at": "1:2:3"}}).encode())
        self.assertEqual(status, 201)
        status, body = self.raw("GET", "/posts?kind=BOUNTY&agent=h1")
        self.assertEqual([p["text"] for p in body["posts"]], ["kill X"])
        self.assertIn("now", body)
        self.assertEqual(self.raw("GET", "/posts?since=abc")[0], 400)

    def test_bad_requests(self):
        self.assertEqual(self.raw("POST", "/post", b"not json")[0], 400)
        self.assertEqual(self.raw("POST", "/post", b"[1]")[0], 400)
        self.assertEqual(self.raw("POST", "/post", json.dumps({"agent": "h2", "kind": "NOPE", "text": "x"}).encode())[0], 400)
        self.assertEqual(self.raw("POST", "/post", b"x" * (B.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.raw("POST", "/other", b"{}")[0], 404)

    def test_rate_limit_answers_429(self):
        codes = [self.raw("POST", "/post", json.dumps({"agent": "flood", "kind": "TAUNT", "text": f"m{i}"}).encode())[0] for i in range(7)]
        self.assertEqual(codes, [201] * 5 + [429] * 2)

    def test_a_busy_port_is_refused_not_shared(self):
        with self.assertRaises(OSError):
            B.serve(B.Board(None), "127.0.0.1", self.port)

    def test_only_loopback_hosts_are_accepted(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with self.assertRaises(ValueError):
                B.serve(B.Board(None), host, 0)
        self.assertTrue(B.is_loopback("::1") and B.is_loopback("localhost") and B.is_loopback("127.0.0.1"))

    def test_client_round_trip(self):
        c = BoardClient(self.url, "client1")
        p = c.post("WARNING", "raider near 1:3", {"at": "1:3:4", "player_id": 7})
        self.assertEqual(p["agent"], "client1")
        r = c.read(since=p["id"] - 1, kind=["WARNING", "REVENGE"])
        self.assertTrue(r["ok"])
        self.assertEqual([x["id"] for x in r["posts"]], [p["id"]])
        self.assertGreaterEqual(r["last_id"], p["id"])
        self.assertEqual(c.read(since=r["last_id"])["posts"], [])
        self.assertIsNone(c.post("NOPE", "x"))             # refused: None, but the board is not considered down
        self.assertEqual(c.down_until, 0.0)


class FailSoftTests(unittest.TestCase):
    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_dead_board_returns_quietly_and_backs_off(self):
        now = [100.0]
        c = BoardClient(f"http://127.0.0.1:{self.free_port()}", "a", timeout=0.5, backoff=60, clock=lambda: now[0])
        self.assertIsNone(c.post("TAUNT", "hi"))
        self.assertGreater(c.down_until, now[0])
        self.assertIn("Error", c.last_error)
        t = time.time()
        for _ in range(50):                      # during the back-off nothing touches the network
            self.assertFalse(c.read()["ok"])
            self.assertIsNone(c.post("TAUNT", "hi"))
        self.assertLess(time.time() - t, 0.5)
        now[0] += 61                             # later it tries again
        c.read()
        self.assertGreater(c.down_until, now[0])

    def test_silent_board_times_out(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)                            # accepts connections in the kernel but never answers
        try:
            c = BoardClient(f"http://127.0.0.1:{srv.getsockname()[1]}", "a", timeout=0.3)
            t = time.time()
            r = c.read()
            self.assertFalse(r["ok"])
            self.assertEqual(r["posts"], [])
            self.assertLess(time.time() - t, 2.0)
        finally:
            srv.close()

    def test_garbage_answer_is_not_a_crash(self):
        srv = B._Server(("127.0.0.1", 0), type("H", (B.BaseHTTPRequestHandler,), {
            "do_GET": lambda self: (self.send_response(200), self.send_header("Content-Length", "5"), self.end_headers(), self.wfile.write(b"<html"))[-1],
            "log_message": lambda *a: None}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = BoardClient(f"http://127.0.0.1:{srv.server_address[1]}", "a", timeout=1)
            self.assertFalse(c.read()["ok"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_null_board_and_factory(self):
        n = make_board(None, "a")
        self.assertIsInstance(n, NullBoard)
        self.assertFalse(n.enabled)
        self.assertIsNone(n.post("TAUNT", "x"))
        self.assertEqual(n.read()["posts"], [])
        self.assertIsInstance(make_board("http://localhost:1", "a"), BoardClient)

    def test_local_url_check(self):
        self.assertTrue(is_local_url("http://localhost:8765"))
        self.assertTrue(is_local_url("http://127.0.0.1:8765/"))
        self.assertFalse(is_local_url("http://example.com:8765"))
        self.assertFalse(is_local_url("ftp://localhost"))


if __name__ == "__main__":
    unittest.main()
