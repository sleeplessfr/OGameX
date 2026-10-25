"""A tiny client for the galaxy board (agents/board.py). It FAILS SOFT.

An agent must behave exactly as before when the board is not running, so nothing here ever raises or waits long:
every call has a short timeout (2 s), and after a failure the client does not try again for a while (`backoff`),
so a dead board costs one slow call, not one per round. A failed call returns a result with `ok` False.
Standard library only.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

TIMEOUT = 2.0
BACKOFF = 60.0     # seconds without a new attempt after a failure


def is_local_url(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https") and (u.hostname or "") in ("localhost", "127.0.0.1", "::1")
    except ValueError:
        return False


class NullBoard:
    """The board is switched off (no --board): every call does nothing and costs nothing."""
    enabled = False

    def post(self, kind, text, data=None, ttl=None):
        return None

    def read(self, since=0, kind=None, limit=50):
        return {"ok": False, "now": 0.0, "last_id": since, "posts": []}


class BoardClient:
    enabled = True

    def __init__(self, url: str, agent: str, timeout: float = TIMEOUT, backoff: float = BACKOFF, clock=time.monotonic):
        self.url = url.rstrip("/")
        self.agent = agent
        self.timeout, self.backoff, self.clock = timeout, backoff, clock
        self.down_until = 0.0
        self.last_error = ""
        # Never use a proxy for the board: it lives on this computer.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None = None):
        """(ok, parsed JSON or None). Never raises."""
        if self.clock() < self.down_until:
            return False, None
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return True, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # The board answered, so it is up: a refusal (rate limit, bad post) is not an outage.
            self.last_error = f"HTTP {e.code}"
            return False, None
        except Exception as e:   # refused, timeout, bad JSON, DNS, ... whatever it is, the agent carries on
            self.last_error = f"{type(e).__name__}: {str(e)[:80]}"
            self.down_until = self.clock() + self.backoff
            return False, None

    def post(self, kind: str, text: str, data: dict | None = None, ttl: float | None = None) -> dict | None:
        """The stored post, or None (board down, post refused)."""
        body = {"agent": self.agent, "kind": kind, "text": text, "data": data or {}}
        if ttl is not None:
            body["ttl"] = ttl
        ok, reply = self._call("POST", "/post", body)
        return reply.get("post") if ok and isinstance(reply, dict) else None

    def read(self, since: int = 0, kind=None, limit: int = 50) -> dict:
        """{'ok', 'now', 'last_id', 'posts'} (oldest first). `kind` is one kind or a list. On failure ok is False and posts is []."""
        q = f"?since={int(since)}&limit={int(limit)}"
        if kind:
            q += "&kind=" + quote(",".join(kind) if isinstance(kind, (list, tuple, set)) else str(kind))
        ok, reply = self._call("GET", "/posts" + q)
        if not ok or not isinstance(reply, dict) or not isinstance(reply.get("posts"), list):
            return {"ok": False, "now": 0.0, "last_id": since, "posts": []}
        posts = [p for p in reply["posts"] if isinstance(p, dict) and isinstance(p.get("id"), int)]
        try:
            return {"ok": True, "now": float(reply.get("now") or 0.0), "last_id": int(reply.get("last_id") or since), "posts": posts}
        except (TypeError, ValueError):
            return {"ok": False, "now": 0.0, "last_id": since, "posts": []}


def make_board(url: str | None, agent: str):
    """A client for `url`, or a NullBoard when the feature is off (url empty)."""
    return BoardClient(url, agent) if url else NullBoard()
