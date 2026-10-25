#!/usr/bin/env python3
"""The observatory: a read-only web page to watch the swarm.

    python observatory.py                      -> http://localhost:8080
    python observatory.py --port 9000 --logs logs --state agents_state.json --base-url http://localhost/api/v1

Four tabs: a MAP of the systems where agents live (coloured by personality, colonies marked, recent battles ringed),
a NEWSPAPER written from the logs (no language model, no network), a live FEED of events, and a LEADERBOARD.

It is strictly read-only. It reads the agents' log files, and it asks the game only for the galaxy view and the highscore
(GET requests, at most about 4 per second, using the token of one existing agent). It never writes to the game, never
creates an account and never writes to any file of the agents (agents_state.json is only read). If the game cannot be
reached, the map and the ranking say so and the newspaper and the feed keep working.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarm.client import ApiClient, ApiError

KEEP_EVENTS = 20000          # events kept in memory (counters are kept for the whole history)
NOISE = ("wait", "opponent")  # logged often and of no interest to a reader: left out of the feed
RECENT_BATTLE_S = 6 * 3600
SCAN_BUDGET = 15             # galaxy views fetched per refresh
SCAN_TTL = 300               # seconds before a system is looked at again
SCAN_RATE = 4.0              # requests per second at most
SCAN_WINDOW = 6              # systems either side of a known planet (agents colonise within this distance)
PERSONAS = ("miner", "scientist", "turtle", "raider", "balanced")
COLONY_SCORE = 300_000       # how big a colony is as news, next to a fight measured in resources


# ---- reading the logs ---------------------------------------------------------------------------------
class LogTail:
    """Reads the *.jsonl logs, remembering how far it got in each file. Tolerates garbage, half-written lines, truncation."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, bytes] = {}

    def poll(self) -> list[dict]:
        found: list[dict] = []
        try:
            paths = sorted(self.folder.glob("*.jsonl"))
        except OSError:
            return found
        for path in paths:
            key = str(path)
            try:
                size = path.stat().st_size
                offset = self.offsets.get(key, 0)
                if size < offset:             # the file was emptied or replaced: start again
                    offset, self.partial[key] = 0, b""
                if size == offset:
                    continue
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError:
                continue
            self.offsets[key] = offset + len(chunk)
            data = self.partial.get(key, b"") + chunk
            lines = data.split(b"\n")
            self.partial[key] = lines.pop()    # b"" when the chunk ended with a newline, else a half-written line
            for raw in lines:
                event = parse_line(raw)
                if event is not None:
                    found.append(event)
        found.sort(key=lambda e: e["t"])
        return found


def parse_line(raw: bytes) -> dict | None:
    line = raw.decode("utf-8", errors="replace").strip()
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or not isinstance(e.get("event"), str) or not isinstance(e.get("t"), (int, float)):
        return None
    e["agent"] = str(e.get("agent", "?"))
    return e


# ---- what the events say ------------------------------------------------------------------------------
def coord_of(text) -> tuple[int, int, int] | None:
    try:
        g, s, p = (int(x) for x in str(text).split(":"))
        return g, s, p
    except ValueError:
        return None


def num(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def describe(e: dict) -> str:
    k = e["event"]
    g = e.get
    if k in ("build", "research"):
        return f"{'built' if k == 'build' else 'researched'} {g('what')} level {g('level')}"
    if k == "units":
        return f"ordered {g('amount')} {g('what')}"
    if k == "spy":
        return f"sent probes to {g('target')} at {g('at')}"
    if k == "report":
        return f"read a report on {g('at')}: loot {num(g('loot')):,}, {g('defenders') if g('defenders') is not None else '?'} defenders"
    if k == "attack":
        ships = g("ships") if isinstance(g("ships"), dict) else {}
        return f"attacked {g('at')} with {sum(num(v) for v in ships.values()):,} ships"
    if k == "attack_skipped":
        return f"held back at {g('at')}: {str(g('why', ''))[:90]}"
    if k == "battle":
        if g("role") == "attacker":
            return (f"battle at {g('at')}: lost {num(g('my_loss')):,}, destroyed {num(g('their_loss')):,}, "
                    f"looted {num(g('loot')):,} ({g('verdict') or 'n/a'})")
        return f"was attacked: robbed of {num(g('loot')):,}, lost {num(g('my_loss')):,}, destroyed {num(g('their_loss')):,}"
    if k == "threat":
        return f"{g('kind')} fleet seen coming from {g('from')}"
    if k == "defend":
        return f"built {g('amount')} {g('what')} ({g('why')})"
    if k == "trade":
        return f"traded with the merchant: gave {_res(g('gave'))} for {_res(g('received'))}"
    if k == "lesson":
        return str(g("why", "learned a lesson"))
    if k == "class":
        return f"chose the {g('chosen')} class"
    if k == "expand":
        return f"wants a colony: {g('why')}"
    if k == "colony_sent":
        return f"sent a colony ship to {g('at')}"
    if k == "colony_founded":
        return f"founded a colony (now {g('planets')} planets)"
    if k == "colony_failed":
        return f"colony attempt failed: {g('why')}"
    if k == "registered":
        return f"joined the universe as {g('player')} ({g('persona')})"
    if k in ("api_error", "error"):
        return f"error: {str(g('msg', ''))[:100]}"
    rest = {a: b for a, b in e.items() if a not in ("t", "agent", "event")}
    return f"{k} {json.dumps(rest)[:100]}" if rest else k


def _res(d) -> str:
    if not isinstance(d, dict):
        return "?"
    return ", ".join(f"{num(v):,} {r}" for r, v in d.items()) or "nothing"


# ---- the newspaper ------------------------------------------------------------------------------------
def _pick(options: list[str], e: dict) -> str:
    """Several ways to say the same thing, chosen the same way every time for the same event."""
    return options[(num(e.get("t")) + len(e["agent"])) % len(options)]


def articles_from(events: list[dict], owner_of=None) -> list[dict]:
    """One article per interesting event, each with a `score` (how big the news is). `owner_of(coord)` may name a planet's owner."""
    owner_of = owner_of or (lambda coord: None)
    out: list[dict] = []
    attacks = [e for e in events if e["event"] == "battle" and e.get("role") == "attacker"]
    last_sent: dict[str, str] = {}
    for e in events:
        a, k, t = e["agent"], e["event"], e["t"]
        if k == "colony_sent":
            last_sent[a] = str(e.get("at"))
        elif k == "colony_founded":
            where = last_sent.get(a)
            out.append(dict(t=t, kind="colony", score=COLONY_SCORE, title=f"{a} founds a new colony",
                            text=_pick([f"{a} has settled a new world{' at ' + where if where else ''}.",
                                        f"A colony ship has arrived: {a} now owns {e.get('planets', 'more')} planets"
                                        f"{', the newest at ' + where if where else ''}."], e)))
        elif k == "colony_failed":
            out.append(dict(t=t, kind="colony", score=20_000, title=f"{a}'s colony plans stall",
                            text=f"{a} could not found a colony: {e.get('why')}."))
        elif k == "battle" and e.get("role") == "attacker":
            loss, kill, loot = num(e.get("my_loss")), num(e.get("their_loss")), num(e.get("loot"))
            victim = owner_of(coord_of(e.get("at"))) if e.get("at") else None
            who = victim or f"the planet at {e.get('at')}"
            if e.get("verdict") == "bad" or (loss > loot + kill and loss > 0):
                out.append(dict(t=t, kind="battle", score=loss, title=f"Disaster for {a} at {e.get('at')}",
                                text=_pick([f"{a} lost {loss:,} in resources attacking {who} and gained only {loot + kill:,}.",
                                            f"A costly defeat: {a}'s fleet was mauled at {e.get('at')}, {loss:,} gone for {loot + kill:,} gained."], e)))
            elif loot + kill > 0:
                out.append(dict(t=t, kind="battle", score=loot + kill, title=f"{a} raids {who}",
                                text=_pick([f"{a} struck {who} and carried off {loot:,} resources, destroying {kill:,} more while losing {loss:,}.",
                                            f"Another raid for {a}: {loot:,} looted at {e.get('at')}, {kill:,} destroyed, own losses {loss:,}."], e)))
        elif k == "battle" and e.get("role") == "defender":
            loss, kill, loot = num(e.get("my_loss")), num(e.get("their_loss")), num(e.get("loot"))
            if any(abs(x["t"] - t) < 900 and num(x.get("loot")) == loot and num(x.get("their_loss")) == loss for x in attacks):
                continue    # the attacker's own report tells this story already
            if loot or loss:
                out.append(dict(t=t, kind="defence", score=loot + loss, title=f"{a} is robbed",
                                text=f"{a} lost {loss:,} in ships and defence and {loot:,} in stolen resources, while destroying {kill:,} of the enemy."))
            elif num(e.get("rounds")) > 0:
                out.append(dict(t=t, kind="defence", score=kill, title=f"{a} repels a raid",
                                text=f"The attackers left empty-handed: {a} destroyed {kill:,} of their fleet."))
        elif k == "trade":
            gave = sum(num(v) for v in (e.get("gave") or {}).values()) if isinstance(e.get("gave"), dict) else 0
            out.append(dict(t=t, kind="trade", score=gave, title=f"{a} deals with the merchant",
                            text=f"{a} gave {_res(e.get('gave'))} and received {_res(e.get('received'))}. Reason: {e.get('why')}."))
        elif k == "lesson":
            out.append(dict(t=t, kind="lesson", score=50_000, title=f"{a} learns the hard way",
                            text=f"{e.get('why')}."))
        elif k == "defend" and num(e.get("amount")) >= 50:
            out.append(dict(t=t, kind="defence", score=30_000, title=f"{a} digs in",
                            text=f"{a} raised {num(e.get('amount')):,} {e.get('what')} ({e.get('why')})."))
    chosen = [e for e in events if e["event"] == "class"]
    if chosen:
        names = ", ".join(sorted({e["agent"] for e in chosen})[:6])
        out.append(dict(t=chosen[-1]["t"], kind="class", score=10_000, title="The classes are chosen",
                        text=f"{len(chosen)} {'player picked' if len(chosen) == 1 else 'players picked'} a character class ({names}{'...' if len(chosen) > 6 else ''})."))
    return out


def build_newspaper(events: list[dict], hours: float = 24.0, owner_of=None, max_articles: int = 12) -> dict:
    """The front page: the biggest piece of news first. Based on the newest event, so it still reads well when the swarm is stopped."""
    if not events:
        return {"dateline": "No news yet", "hours": hours, "headline": _quiet(), "articles": [], "events_considered": 0}
    now = max(e["t"] for e in events)
    recent = [e for e in events if e["t"] >= now - hours * 3600]
    arts = articles_from(sorted(recent, key=lambda e: e["t"]), owner_of)
    arts.sort(key=lambda a: (-a["score"], -a["t"]))
    headline = arts[0] if arts else _quiet()
    per_kind: dict[str, int] = {}
    rest = []
    for a in arts[1:]:
        per_kind[a["kind"]] = per_kind.get(a["kind"], 0) + 1
        if per_kind[a["kind"]] <= 4:
            rest.append(a)
    return {"dateline": datetime.fromtimestamp(now).strftime("%A %d %B, %H:%M") + " universe time", "hours": hours,
            "headline": headline, "articles": sorted(rest[:max_articles], key=lambda a: -a["t"]), "events_considered": len(recent)}


def _quiet() -> dict:
    return {"t": 0, "kind": "quiet", "score": 0, "title": "A quiet day in the universe",
            "text": "Nothing worth reporting has happened lately. The planets hum along."}


# ---- what the agents have done (counters over the whole history) --------------------------------------
class Tally:
    def __init__(self):
        self.agents: dict[str, dict] = {}

    def agent(self, name: str) -> dict:
        return self.agents.setdefault(name, {"persona": None, "username": None, "class": None, "attacks": 0, "net_loot": 0,
                                             "colonies": 0, "last_t": 0, "last_events": deque(maxlen=6)})

    def add(self, e: dict) -> None:
        a = self.agent(e["agent"])
        k = e["event"]
        a["last_t"] = max(a["last_t"], e["t"])
        if k == "registered":
            a["username"], a["persona"] = e.get("player") or a["username"], e.get("persona") or a["persona"]
        elif k == "class":
            a["class"] = e.get("chosen")
        elif k == "colony_founded":
            a["colonies"] += 1
        elif k == "battle" and e.get("role") == "attacker":
            a["attacks"] += 1
            a["net_loot"] += num(e.get("loot")) + num(e.get("their_loss")) - num(e.get("my_loss"))
        if k not in NOISE and k not in ("build", "research", "units", "spy", "report", "attack_skipped"):
            a["last_events"].append({"t": e["t"], "text": describe(e)})


# ---- the game side (read-only) ------------------------------------------------------------------------
class ReadOnlyClient:
    """The only door to the game: it has a get() and nothing else, so nothing here can change the game."""

    def __init__(self, base_url: str, token: str):
        ApiClient.shared_interval = 1.0 / SCAN_RATE     # one shared pace: at most about 4 requests a second
        self._client = ApiClient(base_url, token)

    def get(self, path: str):
        return self._client.get(path)


def token_from_state(path: str | Path) -> tuple[str | None, dict[str, str]]:
    """(a token of one existing agent, {agent name: persona}). The file is only read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    personas = {n: str(v.get("persona")) for n, v in data.items() if isinstance(v, dict) and v.get("persona")}
    for name in sorted(data):
        entry = data[name]
        if isinstance(entry, dict) and entry.get("token"):
            return str(entry["token"]), personas
    return None, personas


class Universe:
    """What we know of the game: galaxy views (cached) and the highscore."""

    def __init__(self, client, galaxy: int = 1, budget: int = SCAN_BUDGET, ttl: float = SCAN_TTL, sleep=time.sleep, clock=time.time):
        self.client, self.galaxy, self.budget, self.ttl = client, galaxy, budget, ttl
        self._sleep, self._clock = sleep, clock
        self.systems: dict[int, tuple[float, list[dict]]] = {}   # system -> (fetched at, occupied slots)
        self.highscore: list[dict] = []
        self.available = client is not None
        self.note = "" if client else "no agent token found: map and ranking unavailable"

    def refresh(self, hint_systems: set[int]) -> None:
        if self.client is None:
            return
        try:
            data = self.client.get("/highscore?type=general&per_page=100")
            self.highscore = list(data.get("players") or [])
            self.available, self.note = True, ""
        except (ApiError, OSError, ValueError, AttributeError) as e:
            self.available, self.note = False, f"game unreachable: {str(e)[:80]}"
            return
        wanted = set(hint_systems)
        for row in self.highscore:
            mp = row.get("main_planet") or {}
            if num(mp.get("galaxy")) == self.galaxy and mp.get("system") is not None:
                wanted.add(num(mp["system"]))
        window = {s2 for s in wanted for s2 in range(s - SCAN_WINDOW, s + SCAN_WINDOW + 1) if 1 <= s2 <= 499}
        now = self._clock()
        due = sorted((s for s in window if s not in self.systems or now - self.systems[s][0] > self.ttl),
                     key=lambda s: (s in self.systems, self.systems.get(s, (0,))[0], s))
        for s in due[:self.budget]:
            try:
                view = self.client.get(f"/galaxy/{self.galaxy}/{s}")
            except ApiError as e:
                if e.status in (401, 403):
                    self.available, self.note = False, "the agent token was refused: map unavailable"
                    return
                continue
            except (OSError, ValueError):
                continue
            slots = []
            for p in view.get("positions") or []:
                planet = p.get("planet")
                if planet:
                    owner = (planet.get("owner") or {}).get("username")
                    slots.append({"position": num(p.get("position")), "planet": planet.get("name"), "owner": owner,
                                  "moon": bool(planet.get("has_moon"))})
            self.systems[s] = (self._clock(), slots)
            self._sleep(1.0 / SCAN_RATE)


# ---- putting it together ------------------------------------------------------------------------------
class Observatory:
    def __init__(self, logs: str | Path, state: str | Path, client=None, galaxy: int = 1, news_hours: float = 24.0, refresh: int = 30):
        self.tail = LogTail(logs)
        self.events: deque = deque(maxlen=KEEP_EVENTS)
        self.tally = Tally()
        self.state_path = state
        self.universe = Universe(client, galaxy)
        self.galaxy, self.news_hours, self.refresh = galaxy, news_hours, refresh
        self.lock = threading.Lock()
        self._personas: dict[str, str] = {}
        self._persona_read = 0.0

    def ingest(self) -> None:
        with self.lock:
            new = self.tail.poll()
            for e in new:
                self.events.append(e)
                self.tally.add(e)

    def scan(self) -> None:
        with self.lock:
            hints = set()
            for e in list(self.events)[-3000:]:
                for key in ("at", "from"):
                    c = coord_of(e.get(key)) if e.get(key) else None
                    if c and c[0] == self.galaxy:
                        hints.add(c[1])
        self.universe.refresh(hints)

    def _persona_of(self, agent: str) -> str | None:
        if time.time() - self._persona_read > 30:
            self._persona_read = time.time()
            _, self._personas = token_from_state(self.state_path)
        return self.tally.agent(agent)["persona"] or self._personas.get(agent)

    def state(self) -> dict:
        self.ingest()
        with self.lock:
            events = list(self.events)
            latest = max((e["t"] for e in events), default=0)
            names = sorted(self.tally.agents)
            by_user = {}
            agents = {}
            for n in names:
                a = self.tally.agents[n]
                persona = self._persona_of(n)
                agents[n] = {"persona": persona, "username": a["username"], "class": a["class"], "attacks": a["attacks"],
                             "net_loot": a["net_loot"], "colonies": a["colonies"], "last_t": a["last_t"],
                             "last_events": list(a["last_events"])}
                if a["username"]:
                    by_user[a["username"]] = n
            planets_owned: dict[str, int] = {}
            owner_at: dict[tuple, str] = {}
            main_of: dict[str, tuple] = {}
            for row in self.universe.highscore:
                mp = row.get("main_planet") or {}
                if row.get("username"):
                    main_of[row["username"]] = (num(mp.get("galaxy")), num(mp.get("system")), num(mp.get("position")))
            colony_spots = {c for e in events if e["event"] == "colony_sent" and (c := coord_of(e.get("at")))}
            recent = {c for e in events if e["event"] == "battle" and e["t"] >= latest - RECENT_BATTLE_S and (c := coord_of(e.get("at")))}
            systems = []
            for s in sorted(self.universe.systems):
                slots = []
                for sl in self.universe.systems[s][1]:
                    coord = (self.galaxy, s, sl["position"])
                    agent = by_user.get(sl["owner"])
                    if sl["owner"]:
                        planets_owned[sl["owner"]] = planets_owned.get(sl["owner"], 0) + 1
                        owner_at[coord] = sl["owner"]
                    is_colony = coord in colony_spots or (sl["owner"] in main_of and main_of[sl["owner"]] != coord)
                    slots.append({"position": sl["position"], "planet": sl["planet"], "owner": sl["owner"], "agent": agent,
                                  "persona": agents[agent]["persona"] if agent else None, "colony": bool(is_colony and agent),
                                  "moon": sl["moon"], "battle": coord in recent})
                if slots:
                    systems.append({"galaxy": self.galaxy, "system": s, "slots": slots})
            leader = []
            for row in self.universe.highscore:
                agent = by_user.get(row.get("username"))
                a = agents.get(agent) if agent else None
                leader.append({"rank": row.get("rank"), "username": row.get("username"), "score": row.get("score"),
                               "agent": agent, "persona": a["persona"] if a else None,
                               "planets": planets_owned.get(row.get("username")) or (1 + a["colonies"] if a else None),
                               "attacks": a["attacks"] if a else None, "net_loot": a["net_loot"] if a else None,
                               "colonies": a["colonies"] if a else None})
            news = build_newspaper(events, self.news_hours, lambda c: owner_at.get(c) if c else None)
            feed = [{"t": e["t"], "agent": e["agent"], "kind": e["event"], "persona": agents.get(e["agent"], {}).get("persona"),
                     "text": describe(e)} for e in events[-1500:] if e["event"] not in NOISE][-600:]
            feed.reverse()
            counters = [{"agent": n, **{k: v for k, v in agents[n].items() if k in ("persona", "attacks", "net_loot", "colonies")}}
                        for n in names]
            return {"generated": time.time(), "latest_event": latest, "refresh": self.refresh, "events_in_memory": len(events),
                    "personas": list(PERSONAS), "agents": agents, "kinds": sorted({f["kind"] for f in feed}),
                    "map": {"available": self.universe.available, "note": self.universe.note, "systems": systems,
                            "scanned": len(self.universe.systems)},
                    "newspaper": news, "feed": feed,
                    "leaderboard": {"available": bool(self.universe.highscore), "rows": leader, "agents": counters}}


# ---- the web server -----------------------------------------------------------------------------------
def make_handler(observatory: Observatory):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    body = json.dumps(observatory.state()).encode("utf-8")
                except Exception as e:   # the page must keep answering whatever happens in the data
                    body = json.dumps({"error": str(e)[:200]}).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            self._send(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):   # keep the console quiet
            pass

    return Handler


def serve(observatory: Observatory, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(observatory))


def scan_loop(observatory: Observatory, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            observatory.ingest()
            observatory.scan()
        except Exception as e:   # never let the background thread die
            print(f"scan error: {e}", file=sys.stderr)
        stop.wait(observatory.refresh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = only this computer (default)")
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--state", default="agents_state.json", help="only read, for a token and the personalities")
    ap.add_argument("--base-url", default="http://localhost/api/v1")
    ap.add_argument("--galaxy", type=int, default=1)
    ap.add_argument("--refresh", type=int, default=30, help="seconds between galaxy scans and page updates")
    ap.add_argument("--news-hours", type=float, default=24.0, help="how far back the newspaper looks")
    args = ap.parse_args()

    token, _ = token_from_state(args.state)
    client = ReadOnlyClient(args.base_url, token) if token else None
    obs = Observatory(args.logs, args.state, client, args.galaxy, args.news_hours, args.refresh)
    stop = threading.Event()
    threading.Thread(target=scan_loop, args=(obs, stop), daemon=True).start()
    try:
        server = serve(obs, args.host, args.port)
    except OSError as e:
        print(f"Cannot open port {args.port}: {e}", file=sys.stderr)
        return 1
    print(f"Observatory on http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}  (read-only; Ctrl+C to stop)")
    if client is None:
        print(f"Note: no token found in {args.state}: the map and the ranking will be unavailable.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Observatory</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:16px;margin:0}.dim{color:var(--dim)}
nav button{background:none;border:1px solid var(--line);color:var(--text);padding:6px 14px;border-radius:6px;cursor:pointer;margin-right:6px}
nav button.on{background:var(--accent);border-color:var(--accent);color:#04101f}
main{padding:16px;max-width:1300px;margin:0 auto}.tab{display:none}.tab.on{display:block}
.legend span{display:inline-flex;align-items:center;gap:5px;margin-right:12px}.dot{width:12px;height:12px;border-radius:3px;display:inline-block}
.systems{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin-top:12px}
.sys{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px}
.sys h3{margin:0 0 6px;font-size:12px;color:var(--dim)}
.slots{display:grid;grid-template-columns:repeat(15,1fr);gap:2px}
.slot{aspect-ratio:1;border-radius:3px;background:#21262d;position:relative;cursor:default}
.slot.col::after{content:"";position:absolute;inset:3px;border-radius:50%;background:#0d1117;opacity:.75}
.slot.bat{outline:2px solid #ff4d4d;outline-offset:1px}
#info{position:sticky;bottom:0;margin-top:12px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;min-height:60px}
.paper{background:#f4ecd8;color:#2b2416;border-radius:6px;padding:20px 26px;font-family:Georgia,'Times New Roman',serif}
.paper .mast{text-align:center;border-bottom:3px double #2b2416;padding-bottom:8px;margin-bottom:12px}
.paper .mast h2{margin:0;font-size:34px;letter-spacing:1px}.paper .date{font-size:13px;font-style:italic}
.paper h3{margin:0 0 4px;font-size:20px}.paper .head h3{font-size:30px}.paper .cols{columns:2;column-gap:28px;margin-top:14px}
.paper article{break-inside:avoid;margin-bottom:14px;border-bottom:1px solid #b9ad8c;padding-bottom:8px}.paper p{margin:0}
@media(max-width:800px){.paper .cols{columns:1}}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}select,input{background:var(--panel);border:1px solid var(--line);color:var(--text);padding:5px 8px;border-radius:6px}
.row{padding:3px 0;border-bottom:1px solid #1c2128}.row .t{color:var(--dim);margin-right:8px;font-variant-numeric:tabular-nums}.row .a{font-weight:600;margin-right:6px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}th{color:var(--dim);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.warn{color:#f0b429}
</style></head><body>
<header><h1>Observatory</h1><nav id="nav"></nav><span id="status" class="dim"></span></header>
<main>
<section id="map" class="tab on"><div class="legend" id="legend"></div><div id="mapnote" class="warn"></div><div class="systems" id="systems"></div><div id="info" class="dim">Hover a planet.</div></section>
<section id="news" class="tab"><div class="paper" id="paper"></div></section>
<section id="feed" class="tab"><div class="filters"><select id="fa"></select><select id="fp"></select><select id="fk"></select><input id="ft" placeholder="search text"></div><div id="rows"></div></section>
<section id="board" class="tab"><div id="boardnote" class="warn"></div><div id="boardtable"></div></section>
</main>
<script>
const COLORS={miner:"#d9a441",scientist:"#4aa3ff",turtle:"#4fbf7a",raider:"#e5534b",balanced:"#b07cf0",other:"#6b7280"};
const $=id=>document.getElementById(id);
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function fmt(n){return n==null?"":Number(n).toLocaleString()}
function when(t){return new Date(t*1000).toLocaleString([], {day:"2-digit",hour:"2-digit",minute:"2-digit"})}
let S=null,tab="map";
const tabs=[["map","Map"],["news","Newspaper"],["feed","Feed"],["board","Leaderboard"]];
tabs.forEach(([id,name])=>{const b=el("button",id==="map"?"on":"",name);b.onclick=()=>{tab=id;document.querySelectorAll(".tab").forEach(s=>s.classList.toggle("on",s.id===id));[...$("nav").children].forEach(c=>c.classList.toggle("on",c===b))};$("nav").appendChild(b)});
Object.entries(COLORS).forEach(([k,c])=>{const s=el("span");const d=el("span","dot");d.style.background=c;s.append(d,k==="other"?"other / human":k);$("legend").appendChild(s)});
{const s=el("span");s.append("● hole = colony   red ring = battle in the last 6 h");$("legend").appendChild(s)}
function drawMap(){
  const m=S.map;$("mapnote").textContent=m.available?"":(m.note||"map unavailable");
  const box=$("systems");box.replaceChildren();
  if(!m.systems.length){box.appendChild(el("div","dim",m.available?"No systems scanned yet...":""));return}
  m.systems.forEach(sys=>{
    const card=el("div","sys");card.appendChild(el("h3","",`System ${sys.galaxy}:${sys.system}`));
    const grid=el("div","slots");const by={};sys.slots.forEach(s=>by[s.position]=s);
    for(let p=1;p<=15;p++){
      const c=el("div","slot");const s=by[p];
      if(s){c.style.background=COLORS[s.persona]||COLORS.other;if(s.colony)c.classList.add("col");if(s.battle)c.classList.add("bat");
        c.onmouseenter=c.onclick=()=>showInfo(sys,s)}
      grid.appendChild(c)}
    card.appendChild(grid);box.appendChild(card)})}
function showInfo(sys,s){
  const i=$("info");i.replaceChildren();i.classList.remove("dim");
  i.appendChild(el("div","",`${sys.galaxy}:${sys.system}:${s.position}  ${s.planet||""}${s.moon?" (moon)":""}`));
  i.appendChild(el("div","",`Owner: ${s.owner||"?"}${s.agent?` (${s.agent}, ${s.persona||"?"})`:" - not one of our agents"}${s.colony?"  colony":""}${s.battle?"  recent battle":""}`));
  const a=s.agent&&S.agents[s.agent];
  if(a){i.appendChild(el("div","dim",`class ${a.class||"-"}, attacks ${a.attacks}, net loot ${fmt(a.net_loot)}, colonies ${a.colonies}`));
    a.last_events.slice().reverse().forEach(e=>i.appendChild(el("div","dim",`${when(e.t)}  ${e.text}`)))}}
function drawNews(){
  const n=S.newspaper,p=$("paper");p.replaceChildren();
  const mast=el("div","mast");mast.appendChild(el("h2","","The Daily Ogame"));mast.appendChild(el("div","date",`${n.dateline} - last ${n.hours} h, ${n.events_considered} events`));p.appendChild(mast);
  const h=el("div","head");h.appendChild(el("h3","",n.headline.title));h.appendChild(el("p","",n.headline.text));p.appendChild(h);
  const cols=el("div","cols");n.articles.forEach(a=>{const r=el("article");r.appendChild(el("h3","",a.title));r.appendChild(el("p","",a.text));cols.appendChild(r)});p.appendChild(cols)}
function fillSelect(sel,label,values){const cur=sel.value;sel.replaceChildren();sel.appendChild(new Option(label,""));values.forEach(v=>sel.appendChild(new Option(v,v)));sel.value=values.includes(cur)?cur:""}
function drawFeed(){
  fillSelect($("fa"),"all agents",Object.keys(S.agents));fillSelect($("fp"),"all personalities",S.personas);fillSelect($("fk"),"all kinds",S.kinds);
  const fa=$("fa").value,fp=$("fp").value,fk=$("fk").value,ft=$("ft").value.toLowerCase();
  const rows=$("rows");rows.replaceChildren();let shown=0;
  for(const f of S.feed){if(fa&&f.agent!==fa||fp&&f.persona!==fp||fk&&f.kind!==fk||ft&&!(f.agent+" "+f.text).toLowerCase().includes(ft))continue;
    const r=el("div","row");r.appendChild(el("span","t",when(f.t)));const a=el("span","a",f.agent);a.style.color=COLORS[f.persona]||COLORS.other;r.append(a,f.text);rows.appendChild(r);if(++shown>=300)break}}
function drawBoard(){
  const b=S.leaderboard,box=$("boardtable");box.replaceChildren();
  $("boardnote").textContent=b.available?"":"The game's highscore is unavailable: showing what the logs say.";
  const t=el("table");const head=el("tr");(b.available?["#","Player","Score","Agent","Planets","Attacks","Net loot","Colonies"]:["Agent","Personality","Attacks","Net loot","Colonies"]).forEach(x=>head.appendChild(el("th","",x)));t.appendChild(head);
  const add=cells=>{const r=el("tr");cells.forEach((c,i)=>r.appendChild(el("td",typeof c==="number"?"n":"",typeof c==="number"?fmt(c):(c==null?"":c))));t.appendChild(r)};
  if(b.available)b.rows.forEach(r=>add([r.rank,r.username,r.score,r.agent?`${r.agent} (${r.persona||"?"})`:"",r.planets,r.attacks,r.net_loot,r.colonies]));
  else b.agents.forEach(a=>add([a.agent,a.persona||"?",a.attacks,a.net_loot,a.colonies]));
  box.appendChild(t)}
function draw(){drawMap();drawNews();drawFeed();drawBoard();
  $("status").textContent=`${S.events_in_memory} events - last at ${S.latest_event?when(S.latest_event):"never"} - map ${S.map.available?"ok":"unavailable"}`}
async function load(){try{const r=await fetch("/api/state");const j=await r.json();if(j.error){$("status").textContent="error: "+j.error}else{S=j;draw()}}catch(e){$("status").textContent="cannot reach the observatory"}
  setTimeout(load,((S&&S.refresh)||30)*1000)}
["fa","fp","fk"].forEach(i=>$(i).onchange=()=>S&&drawFeed());$("ft").oninput=()=>S&&drawFeed();
load();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
