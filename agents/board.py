#!/usr/bin/env python3
"""The galaxy board: a small notice board the agents (and the observatory) can read and write.

    python board.py                         -> http://127.0.0.1:8765
    python board.py --port 9000 --file board.jsonl

Agents post short messages with a KIND, so that rule-based agents can use the board without any language model:

    OFFER     I give X for Y            WANT      I want X
    BOUNTY    a reward for something    WARNING   a raider was seen near system S
    REVENGE   I was robbed by X, who joins me?
    TAUNT     bragging                  ALLIANCE  let us team up

Endpoints (JSON):
    POST /post     {"agent": "agent0001", "kind": "WARNING", "text": "...", "data": {...}, "ttl": 3600}
    GET  /posts?since=<id>&kind=OFFER,WANT&agent=<name>&limit=50   (oldest first, only posts newer than `since`)
    GET  /health

A post has `id` (ever increasing: pass the last one you saw as `since`), `t` (when), `expires`, `agent`, `kind`, `text`
(at most 200 characters) and `data` (a small object, at most 600 bytes as JSON). The board never talks to the game.

It is a separate program on purpose: if it is not running, the agents play exactly as they did before. Safety:
only this computer can reach it (it binds to 127.0.0.1 and refuses other peers), the body of a request is capped,
posts expire (a kind-specific time to live, at most a day), only the newest MAX_POSTS are kept, and each agent may post
only so many times per hour. Posts are saved in a JSON-lines file so a restart keeps them.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KINDS = ("OFFER", "WANT", "BOUNTY", "WARNING", "REVENGE", "TAUNT", "ALLIANCE")
DEFAULT_TTL = {"OFFER": 3600, "WANT": 3600, "BOUNTY": 6 * 3600, "WARNING": 2 * 3600, "REVENGE": 6 * 3600,
               "TAUNT": 1800, "ALLIANCE": 12 * 3600}
MAX_TTL = 24 * 3600
MAX_TEXT = 200
MAX_DATA_BYTES = 600
MAX_BODY = 4096
MAX_POSTS = 2000              # posts kept; the oldest go first
POSTS_PER_AGENT_HOUR = 20     # anti-spam: one agent
POSTS_PER_HOUR_ALL = 600      # anti-spam: everybody together
DUPLICATE_SECONDS = 60        # the same agent posting the very same thing again within this time is refused
MAX_LIMIT = 200
DEFAULT_PORT = 8765
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PostRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Board:
    """The posts, in memory and in a JSON-lines file. Thread safe. `clock` can be replaced in tests."""

    def __init__(self, path: str | Path | None = None, clock=time.time, max_posts: int = MAX_POSTS,
                 per_agent_hour: int = POSTS_PER_AGENT_HOUR, per_hour_all: int = POSTS_PER_HOUR_ALL):
        self.path = Path(path) if path else None
        self.clock = clock
        self.max_posts, self.per_agent_hour, self.per_hour_all = max_posts, per_agent_hour, per_hour_all
        self.lock = threading.Lock()
        self.posts: list[dict] = []
        self.last_id = 0
        self._appended = 0
        self._io_warned = False
        self._load()

    # ---- the file ----------------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        now = self.clock()
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                p = json.loads(line)
                if isinstance(p, dict) and isinstance(p.get("id"), int) and p.get("kind") in KINDS \
                        and isinstance(p.get("agent"), str) and isinstance(p.get("text"), str):
                    p.setdefault("data", {})
                    p["t"], p["expires"] = float(p["t"]), float(p["expires"])
                    self.last_id = max(self.last_id, p["id"])
                    if p["expires"] > now:
                        self.posts.append(p)
            except (ValueError, KeyError, TypeError):
                continue     # a half-written line
        self.posts.sort(key=lambda p: p["id"])
        self.posts = self.posts[-self.max_posts:]
        self._rewrite()

    def _rewrite(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(p) + "\n" for p in self.posts), encoding="utf-8")
            tmp.replace(self.path)
            self._appended = 0
        except OSError as e:
            self._warn(e)

    def _append(self, post: dict) -> None:
        if not self.path:
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(post) + "\n")
            self._appended += 1
        except OSError as e:
            self._warn(e)

    def _warn(self, e: Exception) -> None:
        if not self._io_warned:     # the board keeps working from memory
            self._io_warned = True
            print(f"board: cannot write {self.path}: {e}", file=sys.stderr)

    # ---- writing ------------------------------------------------------------------------------------
    def post(self, agent, kind, text, data=None, ttl=None) -> dict:
        """Add a post or raise PostRefused (status 400 for a bad post, 429 for too many)."""
        agent = agent if isinstance(agent, str) else ""
        if not NAME.match(agent):
            raise PostRefused(400, "bad_agent", "agent must be 1-40 letters, digits, _ . -")
        kind = kind.upper() if isinstance(kind, str) else ""
        if kind not in KINDS:
            raise PostRefused(400, "bad_kind", f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(text, str):
            raise PostRefused(400, "bad_text", "text must be a string")
        text = _CONTROL.sub(" ", text).strip()
        if not text or len(text) > MAX_TEXT:
            raise PostRefused(400, "bad_text", f"text must be 1-{MAX_TEXT} characters")
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise PostRefused(400, "bad_data", "data must be an object")
        try:
            raw = json.dumps(data, sort_keys=True)
        except (TypeError, ValueError):
            raise PostRefused(400, "bad_data", "data must be JSON") from None
        if len(raw.encode("utf-8")) > MAX_DATA_BYTES:
            raise PostRefused(400, "bad_data", f"data is larger than {MAX_DATA_BYTES} bytes")
        try:
            ttl = float(DEFAULT_TTL[kind] if ttl is None else ttl)
        except (TypeError, ValueError):
            raise PostRefused(400, "bad_ttl", "ttl must be a number of seconds") from None
        ttl = min(MAX_TTL, max(60.0, ttl))
        with self.lock:
            now = self.clock()
            hour = [p for p in self.posts if p["t"] > now - 3600]
            if len(hour) >= self.per_hour_all:
                raise PostRefused(429, "board_busy", "too many posts on the board this hour")
            mine = [p for p in hour if p["agent"] == agent]
            if len(mine) >= self.per_agent_hour:
                raise PostRefused(429, "rate_limited", f"at most {self.per_agent_hour} posts per hour per agent")
            if any(p["kind"] == kind and p["text"] == text and p["t"] > now - DUPLICATE_SECONDS
                   and json.dumps(p["data"], sort_keys=True) == raw for p in mine):
                raise PostRefused(429, "duplicate", "the same post was made a moment ago")
            self.last_id = max(self.last_id + 1, int(now * 1000))
            post = {"id": self.last_id, "t": now, "expires": now + ttl, "agent": agent, "kind": kind, "text": text, "data": data}
            self.posts.append(post)
            if len(self.posts) > self.max_posts:
                del self.posts[:len(self.posts) - self.max_posts]
            self._append(post)
            if self._appended > 2 * self.max_posts:
                self._rewrite()
            return dict(post)

    # ---- reading ------------------------------------------------------------------------------------
    def read(self, since: int = 0, kinds=None, agent: str | None = None, limit: int = 50) -> list[dict]:
        """Live posts newer than `since`, oldest first; if more than `limit` match, the newest `limit` of them."""
        limit = min(MAX_LIMIT, max(1, int(limit)))
        kinds = {k.upper() for k in kinds} if kinds else None
        with self.lock:
            now = self.clock()
            self.posts = [p for p in self.posts if p["expires"] > now]
            found = [p for p in self.posts if p["id"] > since and (kinds is None or p["kind"] in kinds)
                     and (agent is None or p["agent"] == agent)]
            return [dict(p) for p in found[-limit:]]

    def health(self) -> dict:
        with self.lock:
            now = self.clock()
            self.posts = [p for p in self.posts if p["expires"] > now]
            return {"ok": True, "posts": len(self.posts), "last_id": self.last_id}


# ---- the web server -----------------------------------------------------------------------------------
def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def make_handler(board: Board):
    class Handler(BaseHTTPRequestHandler):
        def _local_only(self) -> bool:
            if is_loopback(self.client_address[0]):
                return True
            self._send(403, {"error": "forbidden", "message": "the board answers this computer only"})
            return False

        def do_GET(self):
            if not self._local_only():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/health":
                self._send(200, board.health())
            elif url.path == "/posts":
                try:
                    since, limit = int(q.get("since", 0) or 0), int(q.get("limit", 50) or 50)
                except ValueError:
                    self._send(400, {"error": "bad_query", "message": "since and limit must be integers"})
                    return
                kinds = [k for k in q.get("kind", "").split(",") if k.strip()] or None
                posts = board.read(since, [k.strip() for k in kinds] if kinds else None, q.get("agent") or None, limit)
                self._send(200, {"now": board.clock(), "last_id": board.last_id, "posts": posts})
            else:
                self._send(404, {"error": "not_found", "message": "GET /posts, GET /health or POST /post"})

        def do_POST(self):
            if not self._local_only():
                return
            if urlparse(self.path).path != "/post":
                self._send(404, {"error": "not_found", "message": "POST /post"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, {"error": "too_large", "message": f"the body may be at most {MAX_BODY} bytes"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._send(400, {"error": "bad_json", "message": "the body must be a JSON object"})
                return
            try:
                post = board.post(body.get("agent"), body.get("kind"), body.get("text"), body.get("data"), body.get("ttl"))
            except PostRefused as e:
                self._send(e.status, {"error": e.code, "message": e.message})
                return
            self._send(201, {"post": post})

        def do_PUT(self):
            self._send(405, {"error": "method_not_allowed", "message": "GET or POST only"})

        do_DELETE = do_PATCH = do_PUT

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class _Server(ThreadingHTTPServer):
    # Same reason as in observatory.py: on Windows the default would let a second program share a busy port silently.
    allow_reuse_address = False
    daemon_threads = True


def serve(board: Board, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if not is_loopback(host):
        raise ValueError("the board only listens on this computer (127.0.0.1 or localhost)")
    return _Server((host, port), make_handler(board))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1", help="only 127.0.0.1 / localhost are accepted")
    ap.add_argument("--file", default="board.jsonl", help="where posts are saved (JSON lines)")
    ap.add_argument("--max-posts", type=int, default=MAX_POSTS)
    ap.add_argument("--per-agent-hour", type=int, default=POSTS_PER_AGENT_HOUR, help="most posts one agent may make per hour")
    args = ap.parse_args()
    board = Board(args.file, max_posts=args.max_posts, per_agent_hour=args.per_agent_hour)
    try:
        server = serve(board, args.host, args.port)
    except (OSError, ValueError) as e:
        print(f"Cannot start the board on port {args.port}: {e}", file=sys.stderr)
        return 1
    print(f"Galaxy board on http://localhost:{args.port}  ({len(board.posts)} posts kept in {args.file}; Ctrl+C to stop)")
    print(f"Start the swarm with:  python run_swarm.py --board http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
