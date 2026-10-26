#!/usr/bin/env python3
"""Measure how many requests a second your OGameX server can take, using READ-ONLY calls of the kind the agents make.

Example:  python bench.py --base-url http://localhost/api/v1 --agents 50 --seconds 60 --max-rate 50
          python bench.py --dry-run          (tries the tool against a built-in pretend server; touches nothing)

Only registers throwaway accounts (named bench0001, bench0002, ... kept in bench_state.json); after that it only reads:
status, buildings, galaxy view, fleet, highscore, messages. It never builds, researches, sends fleets or posts anything.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The read calls a real agent makes, with how often (per 10 calls) and a label that groups the numbers.
CYCLE = [("status", 5), ("buildings", 2), ("galaxy", 1), ("fleet", 1), ("highscore", 0.5), ("messages", 0.5)]
ROUTINE_SHARE = 0.07          # share of agents online at one time with --routine real (README: about 5-10 %)


# ---- the HTTP layer (injectable) ---------------------------------------------------------------------------------
def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """The real thing: (status, body). Network trouble raises OSError. A transport for tests only has to look like this."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class RateGate:
    """Spaces requests out so that all threads together stay under `rate` a second (0 = no limit)."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = self.clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self.sleep(start - now)


class Api:
    def __init__(self, base_url: str, transport=urllib_transport, timeout: float = 30.0, gate: RateGate | None = None):
        self.base = base_url.rstrip("/")
        self.transport, self.timeout, self.gate = transport, timeout, gate

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict | None, float]:
        """(status, parsed JSON or None, seconds). Status 0 = no answer at all (timeout, refused...)."""
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.gate:
            self.gate.wait()
        started = time.monotonic()
        try:
            status, raw = self.transport(method, self.base + path, headers, data, self.timeout)
        except Exception:
            return 0, None, time.monotonic() - started
        took = time.monotonic() - started
        try:
            parsed = json.loads(raw.decode() or "null")
        except ValueError:
            parsed = None
        return status, parsed if isinstance(parsed, dict) else None, took


# ---- accounts ----------------------------------------------------------------------------------------------------
def load_state(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def ensure_accounts(api: Api, state_path: str, n: int, prefix: str = "bench", domain: str = "example.com", say=print) -> list[dict]:
    """Accounts bench0001..bench000n: reused from the state file, else registered. Returns [{name, token}] for those that work."""
    state = load_state(state_path)
    out = []
    for i in range(1, n + 1):
        name = f"{prefix}{i:04d}"
        entry = state.get(name)
        if entry and entry.get("token"):
            out.append({"name": name, "token": entry["token"], "home": entry.get("home")})
            continue
        password = secrets.token_urlsafe(18)
        email = f"{name}@{domain}"
        status, data, _ = api.call("POST", "/auth/register", body={"email": email, "password": password, "token_name": name})
        if status < 300 and data and data.get("success"):
            token = data["data"]["token"]
            state[name] = {"email": email, "password": password, "token": token}
            save_state(state_path, state)
            out.append({"name": name, "token": token, "home": None})
            say(f"  registered {name} ({len(out)}/{n})")
        else:
            say(f"  could not register {name}: HTTP {status}" + (" (too many requests: lower --max-rate or wait a minute)" if status == 429 else ""))
            if status in (0, 429, 502, 503):
                time.sleep(1.0)
    return out


# ---- the load ------------------------------------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.codes: dict[str, Counter] = defaultdict(Counter)

    def add(self, label: str, status: int, seconds: float) -> None:
        with self.lock:
            self.lat[label].append(seconds)
            self.codes[label][status] += 1

    def total(self) -> int:
        return sum(len(v) for v in self.lat.values())


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(p / 100.0 * len(v)) - 1))
    return v[k]


def _pick(rng: random.Random) -> str:
    return rng.choices([c[0] for c in CYCLE], weights=[c[1] for c in CYCLE])[0]


def one_call(api: Api, acct: dict, kind: str, rng: random.Random, stats: Stats) -> None:
    token = acct["token"]
    if kind == "buildings":
        if not acct.get("home"):
            kind = "status"
        else:
            path = f"/planets/{acct['home']}/buildings"
    if kind == "status":
        path = "/status"
    elif kind == "galaxy":
        path = f"/galaxy/{rng.randint(1, 3)}/{rng.randint(1, 499)}"
    elif kind == "fleet":
        path = "/fleet"
    elif kind == "highscore":
        path = "/highscore?type=general&per_page=100&page=1"
    elif kind == "messages":
        path = "/messages?category=combat&unread_only=1&per_page=20"
    status, data, took = api.call("GET", path, token)
    if kind == "status" and status == 200 and data:
        try:
            acct["home"] = data["data"]["player"]["current_planet_id"]
        except (KeyError, TypeError):
            pass
    stats.add(kind, status, took)


def run_load(api: Api, accounts: list[dict], seconds: float, pace: float, threads: int, stats: Stats, seed: int = 1,
             clock=time.monotonic, sleep=time.sleep) -> float:
    """Every simulated agent calls the read endpoints in turn, one call every `pace` seconds (0 = as fast as the gate allows).
    Returns the seconds the run really took."""
    workers = max(1, min(threads, len(accounts)))
    deadline = clock() + seconds
    started = clock()

    def work(k: int) -> None:
        mine = accounts[k::workers]
        rng = random.Random(seed * 1000 + k)
        due = {id(a): started + rng.uniform(0, pace) for a in mine}      # spread the first calls so they do not arrive together
        while clock() < deadline and mine:
            acct = min(mine, key=lambda a: due[id(a)])
            wait = due[id(acct)] - clock()
            if wait > 0:
                sleep(min(wait, max(deadline - clock(), 0)))
                if clock() >= deadline:
                    break
            one_call(api, acct, _pick(rng), rng, stats)
            due[id(acct)] = max(due[id(acct)] + pace, clock()) if pace > 0 else clock()

    ts = [threading.Thread(target=work, args=(k,), daemon=True) for k in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(seconds + api.timeout + 5)
    return max(clock() - started, 1e-9)


# ---- the report -----------------------------------------------------------------------------------------------------
def report(stats: Stats, elapsed: float, agents: int, max_rate: float, typical_interval: float) -> tuple[str, dict]:
    rows = []
    allv: list[float] = []
    codes: Counter = Counter()
    for label in sorted(stats.lat):
        v = stats.lat[label]
        allv += v
        codes.update(stats.codes[label])
        c = stats.codes[label]
        rows.append((label, len(v), percentile(v, 50), percentile(v, 95), percentile(v, 99),
                     c[429], c[502], sum(n for s, n in c.items() if s == 0 or s >= 500 and s != 502)))
    n = len(allv)
    ok = sum(c for s, c in codes.items() if 200 <= s < 300)
    rps = n / elapsed
    ok_rps = ok / elapsed
    p50, p95, p99 = (percentile(allv, p) for p in (50, 95, 99))
    n429, n502 = codes[429], codes[502]
    n_err = sum(c for s, c in codes.items() if s == 0 or s >= 500 and s != 502)
    other_4xx = sum(c for s, c in codes.items() if 400 <= s < 500 and s != 429)
    lines = [f"{'endpoint':<11}{'requests':>9}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'429':>6}{'502':>6}{'errors':>8}"]
    for label, cnt, a, b, c3, e429, e502, err in rows:
        lines.append(f"{label:<11}{cnt:>9}{a * 1000:>9.0f}{b * 1000:>9.0f}{c3 * 1000:>9.0f}{e429:>6}{e502:>6}{err:>8}")
    lines.append(f"{'ALL':<11}{n:>9}{p50 * 1000:>9.0f}{p95 * 1000:>9.0f}{p99 * 1000:>9.0f}{n429:>6}{n502:>6}{n_err:>8}")
    lines.append("")
    lines.append(f"{n} requests in {elapsed:.1f} s = {rps:.1f} requests/s ({ok_rps:.1f} succeeded), {agents} simulated agents.")
    problem = (n429 + n502 + n_err + other_4xx) / n if n else 1.0
    capped = max_rate > 0 and rps >= 0.9 * max_rate
    supports = int(ok_rps * typical_interval)
    verdict = (f"VERDICT: the server handled {ok_rps:.0f} req/s with p95 {p95 * 1000:.0f} ms; at the typical rate of one request per agent "
               f"per {typical_interval:g} s that supports about {supports} agents online at the same time")
    if n == 0:
        verdict = "VERDICT: no request got through. Is the server up and --base-url right?"
    else:
        if supports:
            verdict += f" (about {int(supports / ROUTINE_SHARE)} registered agents with '--routine real', where roughly {int(ROUTINE_SHARE * 100)}% are online)"
        verdict += "."
        if problem > 0.05:
            verdict += (f" It was struggling: {100 * problem:.0f}% of the answers were errors"
                        f"{' (429 = the game refused because of its own rate limit: raise OGAMEX_API_RATE_LIMIT, or use fewer accounts per address)' if n429 else ''}"
                        f"{' (502 = the web server lost the PHP workers: it is overloaded or restarting)' if n502 else ''}. Treat the number as the limit.")
        elif p95 > 1.0:
            verdict += " Answers were slow (p95 over 1 s): this is about the limit; do not go higher."
        elif capped:
            verdict += f" The test itself was the limit (--max-rate {max_rate:g}): the server was not full. Raise --max-rate and run again to find its real limit."
        else:
            verdict += " Healthy: no real trouble at this load."
    lines.append(verdict)
    summary = {"requests": n, "rps": rps, "ok_rps": ok_rps, "p50": p50, "p95": p95, "p99": p99, "n429": n429, "n502": n502,
               "errors": n_err, "problem": problem, "supports": supports, "capped": capped, "verdict": verdict}
    return "\n".join(lines), summary


# ---- a pretend server (for --dry-run and the tests) ------------------------------------------------------------------
class FakeServer:
    """A local HTTP server that answers like the game's API for the calls above. `latency` seconds per answer, `fail` = a
    status code returned for a share `fail_share` of the requests (e.g. 429)."""

    def __init__(self, latency: float = 0.005, fail: int = 0, fail_share: float = 0.0):
        outer = self
        self.latency, self.fail, self.fail_share = latency, fail, fail_share
        self.paths: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.users = 0

        class H(BaseHTTPRequestHandler):
            def _send(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                with outer.lock:
                    outer.paths.append((method, self.path))
                    roll = random.random()
                time.sleep(outer.latency)
                if outer.fail and roll < outer.fail_share and not self.path.startswith("/auth"):
                    return self._send(outer.fail, {"success": False, "error": {"code": "fake", "message": "fake failure"}})
                path = self.path.split("?")[0]
                if method == "POST" and path.endswith("/auth/register"):
                    with outer.lock:
                        outer.users += 1
                        uid = outer.users
                    return self._send(200, {"success": True, "data": {"token": f"tok{uid}", "user": {"id": uid, "username": f"u{uid}"}}})
                if path.endswith("/status"):
                    return self._send(200, {"success": True, "data": {"player": {"current_planet_id": 10}, "planets": [{"id": 10}]}})
                return self._send(200, {"success": True, "data": {}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---- main -------------------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None, transport=urllib_transport) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost/api/v1")
    ap.add_argument("--agents", type=int, default=50, help="simulated agents (= accounts bench0001..)")
    ap.add_argument("--seconds", type=float, default=60.0, help="how long to run the load")
    ap.add_argument("--max-rate", type=float, default=50.0, help="most requests per second in total (0 = no limit: as fast as the server answers)")
    ap.add_argument("--pace", type=float, default=0.0, help="seconds between two calls of one agent (0 = as fast as --max-rate allows)")
    ap.add_argument("--threads", type=int, default=100, help="most threads (each handles several agents when --agents is larger)")
    ap.add_argument("--typical-interval", type=float, default=10.0,
                    help="seconds between two requests of one real agent, for the verdict (default 10: a round of about 5 calls every 45 s)")
    ap.add_argument("--state", default="bench_state.json", help="where the throwaway accounts are remembered (not agents_state.json)")
    ap.add_argument("--prefix", default="bench")
    ap.add_argument("--email-domain", default="example.com")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="test against a built-in pretend server (no game needed, nothing is registered)")
    args = ap.parse_args(argv)
    if args.agents < 1 or args.seconds <= 0:
        ap.error("--agents and --seconds must be above 0")

    fake = None
    state = args.state
    base = args.base_url
    if args.dry_run:
        fake = FakeServer(latency=0.01).__enter__()
        base = fake.url
        state = str(Path(tempfile.mkdtemp()) / "bench_state.json")
        print(f"Dry run against a pretend server at {base} (the numbers say nothing about your game)")
    try:
        api = Api(base, transport, args.timeout, RateGate(args.max_rate))
        status, _, _ = api.call("GET", "/universe")
        if status == 0:
            print(f"Cannot reach the API at {base}. Is the game running?", file=sys.stderr)
            return 1
        print(f"Preparing {args.agents} accounts ({args.prefix}0001..) in {state} ...")
        print("  Each new account creates a real player and planet in your game (about 1-3 s each), so this can take a few minutes the first time.")
        print("  Accounts are kept: the next run reuses them. Registration is silent for a while if the game is busy: wait for the 'registered' lines.")
        accounts = ensure_accounts(api, state, args.agents, args.prefix, args.email_domain)
        if not accounts:
            print("No account could be made or reused: nothing to test.", file=sys.stderr)
            return 1
        print(f"Running {len(accounts)} simulated agents for {args.seconds:g} s (read-only calls, at most "
              f"{'unlimited' if args.max_rate <= 0 else f'{args.max_rate:g}'} requests/s) ...")
        stats = Stats()
        elapsed = run_load(api, accounts, args.seconds, args.pace, args.threads, stats, args.seed)
        text, _ = report(stats, elapsed, len(accounts), args.max_rate, args.typical_interval)
        print()
        print(text)
        return 0
    finally:
        if fake:
            fake.__exit__()


if __name__ == "__main__":
    sys.exit(main())
