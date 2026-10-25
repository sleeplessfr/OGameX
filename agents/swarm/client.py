"""Thin HTTP client for the OGameX /api/v1 API. Standard library only."""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any


class ApiError(Exception):
    """The server answered with an error (HTTP status >= 400 or success=false)."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.request_line = ""  # "GET /path", filled in by the client so logs say which call failed


class ApiClient:
    # One pace for every client in this process: the game allows 300 requests a minute (per player, or per address if
    # it cannot tell the players apart), and 20 agents starting together used to burst past it. 0.25 s = 240 a minute.
    shared_interval = 0.25
    _shared_lock = threading.Lock()
    _shared_next = 0.0

    @classmethod
    def _shared_pause(cls) -> float:
        with cls._shared_lock:
            now = time.monotonic()
            start = max(now, cls._shared_next)
            cls._shared_next = start + cls.shared_interval
            return start - now

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0, min_interval: float = 0.15):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.min_interval = min_interval  # self-imposed pause between requests (be polite to the server)
        self._last_request = 0.0

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        if self.shared_interval > 0:
            wait = self._shared_pause()
            if wait > 0:
                time.sleep(wait)

        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        finally:
            self._last_request = time.monotonic()

        try:
            payload = json.loads(raw.decode() or "null")
        except ValueError:
            error = ApiError(status, "bad_response", f"Not JSON: {raw[:120]!r}")
            error.request_line = f"{method} {path}"
            raise error

        if status >= 400 or not (isinstance(payload, dict) and payload.get("success")):
            err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
            if status == 429:
                error = ApiError(429, "rate_limited", "Too many requests", {"retry_after": 20 + random.random() * 20})   # jitter: agents must not all wake at once
            else:
                # A crash inside the game (HTTP 500) comes back as Laravel's own JSON, not our envelope: keep its message.
                body = payload if isinstance(payload, dict) else {}
                message = err.get("message") or body.get("message") or f"HTTP {status}"
                if body.get("exception"):
                    message = f"{body['exception']}: {message} ({body.get('file', '?')}:{body.get('line', '?')})"
                error = ApiError(status, err.get("code", "http_error"), message[:400], err.get("details"))
            error.request_line = f"{method} {path}"
            raise error
        return payload["data"]

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.request("POST", path, body or {})
