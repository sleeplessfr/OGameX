#!/usr/bin/env python3
"""The observatory: a read-only web page to watch the swarm.

    python observatory.py                      -> http://localhost:8080
    python observatory.py --port 9000 --logs logs --state agents_state.json --base-url http://localhost/api/v1

Four tabs: a MAP of the systems where agents live (coloured by personality, colonies marked, recent battles ringed),
a NEWSPAPER written from the logs (no language model, no network), a live FEED of events, and a LEADERBOARD.
With `--board URL` (the galaxy board, board.py) a fifth tab shows the posts the agents leave for each other and the
newspaper gets a "From the galaxy board" section. That is a plain GET of /posts, fail-soft: without the board nothing changes.

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
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import llm_page
import galaxy_page
from swarm.diplomacy import relations_from_events
from swarm.client import ApiClient, ApiError
from swarm.boardclient import TIMEOUT as BOARD_TIMEOUT

KEEP_EVENTS = 20000          # events kept in memory (counters are kept for the whole history)
NOISE = ("wait", "opponent")  # logged often and of no interest to a reader: left out of the feed
RECENT_BATTLE_S = 6 * 3600
RANKING_TYPES = ("economy", "research", "military")   # besides the general ranking
RANKING_TTL = 120.0   # seconds between two reads of those
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
    if k == "diary":
        return str(g("text", ""))
    if k == "voice_post":
        return f"put a {g('kind')} post in its own words: {str(g('text', ''))[:100]}"
    if k == "board_post":
        return f"posted on the board ({g('kind')}): {str(g('text', ''))[:100]}"
    if k == "board_read":
        return f"read the board: {g('posts')} new posts{', raised its guard' if g('guard') else ''}"
    if k == "trade_offer":
        return f"{g('action', 'offered')}: gives {_res(g('give'))} for {_res(g('want'))}{' with ' + str(g('with')) if g('with') else ''}"
    if k == "trade_sent":
        return f"sent {_res(g('resources'))} to {g('to')} (instalment {g('instalment')})"
    if k == "trade_received":
        return f"received {_res(g('resources'))} from {g('partner')}"
    if k == "trade_cheated":
        return f"was cheated by {g('partner')} (owed {num(g('owed')):,}); shunned"
    if k == "revenge_join":
        return f"joined {g('victim')}'s call for revenge against {g('raider_at')}"
    if k == "revenge_done":
        return f"revenge on {g('raider_at')} is over (looted {num(g('loot')):,})"
    if k == "bounty_post":
        return f"put a bounty of {_res(g('reward'))} on the raider at {g('target')}"
    if k == "bounty_claim":
        return f"claimed bounty {g('bounty')} after a raid on {g('at')} (loot {num(g('loot')):,})"
    if k == "bounty_verdict":
        return f"the referee {'confirmed' if g('ok') else 'rejected'} the claim on bounty {g('bounty')}: {str(g('why', ''))[:80]}"
    if k == "bounty_paid":
        if g("side") == "poster":
            return f"paid the bounty {_res(g('resources'))} to {g('to')}"
        return f"was paid the bounty {_res(g('resources'))} by {g('poster')}"
    if k == "bounty_unpaid":
        if g("side") == "poster":
            return f"could not pay bounty {g('bounty')} to {g('claimant')}: {str(g('why', ''))[:60]}"
        return f"was never paid bounty {g('bounty')} by {g('poster')}; shunned"
    if k == "fleet_saved":
        ships = g("ships") if isinstance(g("ships"), dict) else {}
        return f"saved {sum(num(v) for v in ships.values()):,} ships from the fleet at {g('attacker', 'an enemy')}"
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
    if k == "contact":
        return f"met {g('with')} ({g('how')})"
    if k == "diplo_hello":
        return f"proposes {g('step')} to {g('with')}: {str(g('text', ''))[:120]}"
    if k == "diplo_answer":
        return f"{g('decision')} ({g('side')}, {g('with')}): {str(g('reason', ''))[:100]}"
    if k == "diplo_step":
        return f"{g('with')}: {g('from')} -> {g('to')} (trust {g('trust')}, {g('why')})"
    if k == "diplo_trust":
        return f"trust with {g('with')} {g('delta')} ({g('why')}) now {g('trust')}"
    if k == "diplo_renewed":
        return f"pact with {g('with')} renewed"
    if k == "diplo_expired":
        return f"pact with {g('with')} expired (back to friendly)"
    if k == "diplo_betrayal":
        return f"BROKE its pact with {g('with')} to raid them (loot {g('loot')})"
    if k == "diplo_declined":
        return f"no pact with {g('with')}: {g('reason')}"
    if k == "diplo_announce":
        return f"announces: {str(g('text', ''))[:140]}"
    if k == "registered":
        return f"joined the universe as {g('player')} ({g('persona')})"
    if k in ("api_error", "error"):
        return f"error: {str(g('msg', ''))[:100]}"
    rest = {a: b for a, b in e.items() if a not in ("t", "agent", "event")}
    return f"{k} {json.dumps(rest)[:100]}" if rest else k


def _total(d) -> int:
    return sum(num(v) for v in d.values()) if isinstance(d, dict) else 0


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
        elif k == "trade_cheated":
            out.append(dict(t=t, kind="trade", score=max(20_000, num(e.get("owed"))), title=f"{a} is cheated by {e.get('partner')}",
                            text=f"{a} sent goods and got nothing back from {e.get('partner')} (owed {num(e.get('owed')):,}). The cheat is shunned from now on."))
        elif k == "bounty_post":
            out.append(dict(t=t, kind="bounty", score=max(15_000, num(e.get("value")), _total(e.get("reward"))), title=f"{a} puts a price on a raider",
                            text=f"WANTED: the raider at {e.get('target')}. {a} offers {_res(e.get('reward'))} to whoever hits it, paid after the referee confirms."))
        elif k == "bounty_paid" and e.get("side") == "poster":
            out.append(dict(t=t, kind="bounty", score=max(15_000, num(e.get("value")), _total(e.get("resources"))), title=f"{a} pays a bounty",
                            text=f"{a} sent {_res(e.get('resources'))} to {e.get('to')} for a confirmed raid. A debt honoured."))
        elif k == "trade_received":
            got = max(num(e.get("value")), _total(e.get("resources")))
            out.append(dict(t=t, kind="trade", score=got, title=f"{a} and {e.get('partner')} close a deal",
                            text=_pick([f"{a} received {_res(e.get('resources'))} from {e.get('partner')}: the trade is complete.",
                                        f"A fair swap between {a} and {e.get('partner')}: {_res(e.get('resources'))} changed hands."], e)))
        elif k == "fleet_saved":
            ships = e.get("ships") if isinstance(e.get("ships"), dict) else {}
            n = sum(num(v) for v in ships.values())
            out.append(dict(t=t, kind="fleet", score=max(num(e.get("value")), 20_000), title=f"{a} saves its fleet",
                            text=_pick([f"{a} saw {e.get('attacker', 'an enemy fleet')} coming and sent {n:,} ships out of reach just in time.",
                                        f"Warned in time: {a} moved {n:,} ships to safety before {e.get('attacker', 'the raiders')} arrived."], e)))
        elif k == "bounty_unpaid" and e.get("side") == "claimant":
            out.append(dict(t=t, kind="bounty", score=40_000, title=f"{a} is never paid",
                            text=f"The referee confirmed {a}'s claim on bounty {e.get('bounty')}, but {e.get('poster')} never paid. {e.get('poster')} is shunned."))
        elif k == "revenge_join":
            out.append(dict(t=t, kind="revenge", score=25_000, title=f"{a} joins a revenge",
                            text=f"{a} answers {e.get('victim')}'s call and sets out after the raider at {e.get('raider_at')}."))
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


KICKERS = {"bounty": "BOUNTY", "battle": "RAID", "defence": "ROBBERY", "colony": "NEW COLONY", "trade": "TRADE", "fleet": "FLEET SAVED",
           "revenge": "REVENGE", "lesson": "LESSON", "class": "CLASSES", "quiet": "QUIET"}
SECONDARY = 5


def front_page(arts: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """(headline, secondary stories, the rest). The headline is the biggest story. The secondary ones are the next best, at most
    SECONDARY, taking a story of a kind not yet shown first so the grid is not five raids. Ties: the newest, then the title."""
    ranked = sorted(arts, key=lambda a: (-a["score"], -a["t"], a["title"]))
    if not ranked:
        return _quiet(), [], []
    head, rest = ranked[0], ranked[1:]
    second: list[dict] = []
    seen = {head["kind"]}
    for a in rest:                       # one of each other kind first
        if len(second) < SECONDARY and a["kind"] not in seen:
            second.append(a)
            seen.add(a["kind"])
    for a in rest:                       # then fill up with the best of what is left
        if len(second) < SECONDARY and a not in second:
            second.append(a)
    second.sort(key=lambda a: (-a["score"], -a["t"], a["title"]))
    return head, second, [a for a in rest if a not in second]


def build_newspaper(events: list[dict], hours: float = 24.0, owner_of=None, max_articles: int = 12) -> dict:
    """The front page: the biggest piece of news first. Based on the newest event, so it still reads well when the swarm is stopped."""
    if not events:
        return {"dateline": "No news yet", "hours": hours, "headline": _quiet(), "secondary": [], "articles": [], "events_considered": 0}
    now = max(e["t"] for e in events)
    recent = [e for e in events if e["t"] >= now - hours * 3600]
    arts = articles_from(sorted(recent, key=lambda e: e["t"]), owner_of)
    headline, secondary, others = front_page(arts)
    per_kind: dict[str, int] = {}
    rest = []
    for a in others:
        per_kind[a["kind"]] = per_kind.get(a["kind"], 0) + 1
        if per_kind[a["kind"]] <= 4:
            rest.append(a)
    for a in [headline] + secondary + rest:
        a["kicker"] = KICKERS.get(a["kind"], a["kind"].upper())
    return {"dateline": datetime.fromtimestamp(now).strftime("%A %d %B, %H:%M") + " universe time", "hours": hours,
            "headline": headline, "secondary": secondary, "articles": sorted(rest[:max_articles], key=lambda a: -a["t"]),
            "events_considered": len(recent)}


def _quiet() -> dict:
    return {"t": 0, "kind": "quiet", "score": 0, "title": "A quiet day in the universe",
            "text": "Nothing worth reporting has happened lately. The planets hum along.", "kicker": "QUIET"}


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


# ---- the language model's call log (read-only, fail-soft) --------------------------------------------
class LlmStatus:
    """Counts the calls in <logs>/llm.jsonl (written by swarm/llm.py) for the header. Reads only what is new; never raises."""

    def __init__(self, folder: str | Path):
        self.path = Path(folder) / "llm.jsonl"
        self.offset, self.partial = 0, b""
        self.calls = self.failed = 0
        self.latency = 0.0
        self.last_t = 0

    def summary(self) -> dict | None:
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset, self.partial = 0, b""
                self.calls = self.failed = 0
                self.latency = 0.0
            if size > self.offset:
                with self.path.open("rb") as f:
                    f.seek(self.offset)
                    chunk = f.read(size - self.offset)
                self.offset += len(chunk)
                lines = (self.partial + chunk).split(b"\n")
                self.partial = lines.pop()
                for raw in lines:
                    try:
                        e = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(e, dict) or e.get("kind") != "llm_call":
                        continue
                    self.calls += 1
                    if not e.get("ok"):
                        self.failed += 1
                    self.latency += float(e.get("latency") or 0) if isinstance(e.get("latency"), (int, float)) else 0.0
                    self.last_t = max(self.last_t, num(e.get("t")))
        except OSError:
            return None
        except Exception:
            pass
        if not self.calls:
            return None
        return {"calls": self.calls, "failed": self.failed, "avg_latency": round(self.latency / self.calls, 2), "last_t": self.last_t}


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
        self.rankings: dict[str, list[dict]] = {}   # the other rankings: economy, research, military
        self._rankings_at = 0.0
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
        if self._clock() - self._rankings_at > RANKING_TTL:   # the other rankings change slowly: ask now and then
            self._rankings_at = self._clock()
            for kind in RANKING_TYPES:
                try:
                    self.rankings[kind] = list(self.client.get(f"/highscore?type={kind}&per_page=100").get("players") or [])
                except (ApiError, OSError, ValueError, AttributeError):
                    pass
                self._sleep(0.25)
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
                                  "moon": bool(planet.get("has_moon")), "planet_id": num(planet.get("id")) or None})
            self.systems[s] = (self._clock(), slots)
            self._sleep(1.0 / SCAN_RATE)


# ---- the galaxy board (read only) ---------------------------------------------------------------------
BOARD_KEEP = 200            # posts shown
NOTICE_KINDS = ("WARNING", "REVENGE", "BOUNTY", "CLAIM", "VERDICT", "ALLIANCE", "HELLO", "REPLY", "OFFER", "WANT", "TAUNT")   # in this order of interest


class BoardReader:
    """Reads the posts of board.py with a plain GET. Never writes, never raises: a board that is not there is just "unavailable"."""

    def __init__(self, url: str | None, fetch=None, clock=time.time, ttl: float = 20.0):
        self.url = url.rstrip("/") if url else None
        self._fetch = fetch or self._http
        self._clock = clock
        self.ttl = ttl
        self.posts: list[dict] = []
        self.available = False
        self.note = "no board connected (start board.py and pass --board)" if not self.url else "board not read yet"
        self.fetched_at = -1e12
        self.now = 0.0

    def _http(self, path: str):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.url + path, timeout=BOARD_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def refresh(self) -> None:
        if not self.url or self._clock() - self.fetched_at < self.ttl:
            return
        self.fetched_at = self._clock()
        try:
            data = self._fetch(f"/posts?limit={BOARD_KEEP}")
            posts = [p for p in data["posts"] if isinstance(p, dict) and isinstance(p.get("kind"), str)]
            self.posts, self.now, self.available, self.note = posts, float(data.get("now") or self._clock()), True, ""
        except Exception as e:   # down, garbage, whatever: the rest of the observatory keeps working
            self.available, self.note = False, f"the board at {self.url} cannot be reached ({type(e).__name__})"


def describe_post(p: dict) -> str:
    text = str(p.get("text", ""))[:200]
    d = p.get("data") if isinstance(p.get("data"), dict) else {}
    if p.get("kind") == "REVENGE" and d.get("reply_to") is not None:
        return "joins the call for revenge: " + text      # a hunter answering a REVENGE call
    return text


def _clean(d, depth: int = 0):
    """The data of a post as plain JSON the page can show: numbers, short strings, small dicts; anything else is dropped."""
    if isinstance(d, bool) or d is None:
        return d
    if isinstance(d, (int, float)):
        return d
    if isinstance(d, str):
        return d[:200]
    if isinstance(d, dict) and depth < 2:
        return {str(k)[:40]: _clean(v, depth + 1) for k, v in list(d.items())[:20]}
    return None


def _post_id(x) -> str:
    return str(x)


def wanted_notices(posts: list[dict], events: list[dict], now: float | None = None, limit: int = 8) -> list[dict]:
    """The 'wanted' notices: every BOUNTY on the board with what became of it. Status, strongest first: paid / unpaid (from the
    agents' bounty_paid / bounty_unpaid events), confirmed or rejected (the referee's VERDICT), claimed (a CLAIM waits), expired, open.
    Read only, and tolerant of anything odd on the board."""
    now = time.time() if not now else now
    claims: dict[str, list[dict]] = {}
    verdicts: dict[str, list[dict]] = {}
    for p in posts:
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        if p.get("kind") == "CLAIM" and d.get("bounty_id") is not None:
            claims.setdefault(_post_id(d["bounty_id"]), []).append(p)
        elif p.get("kind") == "VERDICT" and d.get("bounty_id") is not None and p.get("agent") == "referee":
            verdicts.setdefault(_post_id(d["bounty_id"]), []).append(p)
    paid = {_post_id(e.get("bounty")) for e in events if e.get("event") == "bounty_paid"}
    unpaid = {_post_id(e.get("bounty")) for e in events if e.get("event") == "bounty_unpaid"}
    out = []
    for p in sorted(posts, key=lambda p: -float(p.get("t") or 0)):
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        if p.get("kind") != "BOUNTY":
            continue
        key = _post_id(p.get("id"))
        vs = verdicts.get(key, [])
        if key in paid:
            status = "paid"
        elif key in unpaid:
            status = "unpaid"
        elif any((v.get("data") or {}).get("ok") is True for v in vs):
            status = "confirmed, payment due"
        elif vs:
            status = "claim rejected"
        elif claims.get(key):
            status = "claimed, waiting for the referee"
        elif num(d.get("expires")) and num(d.get("expires")) < now:
            status = "expired"
        else:
            status = "open"
        claimant = next((c.get("agent") for c in claims.get(key, [])), None)
        out.append({"id": p.get("id"), "t": p.get("t"), "poster": str(p.get("agent", "?")), "target_at": str(d.get("target_at", "?")),
                    "target_id": d.get("target_player_id"), "reward": _res(d.get("reward")), "status": status,
                    "claimant": str(claimant) if claimant else None})
    return out[:limit]


def notices_from(posts: list[dict], limit: int = 6) -> list[dict]:
    """The newspaper's board section: the newest post of each kind first, in order of interest, then the rest."""
    newest = sorted(posts, key=lambda p: -float(p.get("t") or 0))
    picked, seen = [], set()
    for kind in NOTICE_KINDS:
        for p in newest:
            if p["kind"] == kind and id(p) not in seen:
                picked.append(p)
                seen.add(id(p))
                break
    for p in newest:
        if len(picked) >= limit:
            break
        if id(p) not in seen:
            picked.append(p)
            seen.add(id(p))
    return [{"t": p.get("t"), "agent": str(p.get("agent", "?")), "kind": p["kind"], "text": describe_post(p)} for p in picked[:limit]]


def diplomacy_view(events: list[dict], limit: int = 150) -> dict:
    """The relationships table of the Diplomacy tab, from the log events (contact, diplo_step, diplo_trust). Never raises."""
    try:
        rows = relations_from_events(events)[:limit]
    except Exception:
        return {"rows": []}
    slots: dict[str, dict] = {}
    try:        # pact slots used and the next pact expiry per agent (the last `diplo_slots` line of each); fail-soft
        for e in events:
            if e.get("event") == "diplo_slots" and e.get("agent"):
                slots[str(e["agent"])] = {"agent": str(e["agent"]), "used": e.get("used"), "slots": e.get("slots"),
                                          "next_expiry": e.get("next_expiry"), "t": e.get("t")}
    except Exception:
        slots = {}
    return {"rows": rows, "slots": sorted(slots.values(), key=lambda r: r["agent"])[:limit]} if slots else {"rows": rows}


# ---- putting it together ------------------------------------------------------------------------------
class Observatory:
    def __init__(self, logs: str | Path, state: str | Path, client=None, galaxy: int = 1, news_hours: float = 24.0, refresh: int = 30,
                 board: BoardReader | None = None):
        self.board = board or BoardReader(None)
        self.tail = LogTail(logs)
        self.llm = LlmStatus(logs)
        self.events: deque = deque(maxlen=KEEP_EVENTS)
        self.tally = Tally()
        self.state_path = state
        self.universe = Universe(client, galaxy)
        self.galaxy, self.news_hours, self.refresh = galaxy, news_hours, refresh
        self.lock = threading.Lock()
        self._personas: dict[str, str] = {}
        self._persona_read = 0.0
        self.base_url = None            # set by main(): the game API, to read one planet on demand
        self._planet_cache: dict[int, tuple[float, dict]] = {}

    def planet_detail(self, planet_id: int, owner: str | None, name: str = "") -> dict:
        """One planet, read live from the game with the token of the agent that owns it (the API only shows your own planets).
        A planet of someone who is not one of our agents gives public facts only. Cached for 30 s; read only."""
        hit = self._planet_cache.get(planet_id)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        agent = None
        with self.lock:
            for n, a in self.tally.agents.items():
                if owner and a.get("username") == owner:
                    agent = n
        token = None
        if agent:
            try:
                token = (json.loads(Path(self.state_path).read_text(encoding="utf-8")).get(agent) or {}).get("token")
            except (OSError, ValueError):
                token = None
        if not token or not self.base_url:
            out = {"public": True, "name": name, "owner": owner, "agent": agent,
                   "note": "Not one of our agents: the game only shows its owner and name." if not agent else "No token for this agent."}
        else:
            try:
                out = galaxy_page.planet_view(ReadOnlyClient(self.base_url, token).get(f"/planets/{int(planet_id)}"))
                out.update(owner=owner, agent=agent, persona=self._persona_of(agent))
            except (ApiError, OSError, ValueError) as e:
                out = {"public": True, "name": name, "owner": owner, "agent": agent, "note": f"cannot read the planet: {str(e)[:100]}"}
        self._planet_cache[planet_id] = (time.time(), out)
        return out

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
        self.board.refresh()

    def _board_state(self, agents: dict, wanted: list[dict] | None = None) -> dict:
        b = self.board
        rows = sorted(b.posts, key=lambda p: -float(p.get("t") or 0))[:BOARD_KEEP] if b.available else []
        status = {_post_id(w["id"]): w["status"] for w in wanted or []}
        counts: dict[str, int] = {}
        for p in rows:
            counts[p["kind"]] = counts.get(p["kind"], 0) + 1
        return {"enabled": bool(b.url), "available": b.available, "note": b.note, "counts": counts,
                "now": b.now or time.time(),
                "posts": [{"id": p.get("id"), "t": p.get("t"), "expires": p.get("expires"), "agent": str(p.get("agent", "?")),
                           "persona": (agents.get(str(p.get("agent", "?"))) or {}).get("persona"), "kind": p["kind"],
                           "text": describe_post(p), "data": _clean(p.get("data")) if isinstance(p.get("data"), dict) else {},
                           "reply": isinstance(p.get("data"), dict) and p["data"].get("reply_to") is not None,
                           "status": status.get(_post_id(p.get("id"))) if p["kind"] == "BOUNTY" else None} for p in rows]}

    def _persona_of(self, agent: str) -> str | None:
        if time.time() - self._persona_read > 30:
            self._persona_read = time.time()
            _, self._personas = token_from_state(self.state_path)
        return self.tally.agent(agent)["persona"] or self._personas.get(agent)

    def galaxy_data(self) -> dict:
        """Data for /galaxy: the scanned systems with their planets and the fleets in flight (see galaxy_page.fleets_from)."""
        st = self.state()
        main_of = {}
        for row in self.universe.highscore:
            mp = row.get("main_planet") or {}
            if row.get("username"):
                main_of[row["username"]] = [num(mp.get("galaxy")), num(mp.get("system")), num(mp.get("position"))]
        user_of = {n: a.get("username") for n, a in st["agents"].items()}
        now = time.time()
        with self.lock:
            events = list(self.events)
        fleets = galaxy_page.fleets_from(events, lambda ag: main_of.get(user_of.get(ag)), now,
                                         lambda ag: (st["agents"].get(ag) or {}).get("persona"))
        return {"now": now, "systems": st["map"]["systems"], "fleets": fleets,
                "debris": galaxy_page.debris_from(events, now),
                "agents": {n: {"last_events": a.get("last_events", [])[-6:]} for n, a in st["agents"].items()},
                "missions": {k: [v[0], v[1]] for k, v in galaxy_page.MISSIONS.items()}}

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
                    slots.append({"position": sl["position"], "planet": sl["planet"], "owner": sl["owner"], "agent": agent, "planet_id": sl.get("planet_id"),
                                  "persona": agents[agent]["persona"] if agent else None, "colony": bool(is_colony and agent),
                                  "moon": sl["moon"], "battle": coord in recent})
                if slots:
                    systems.append({"galaxy": self.galaxy, "system": s, "slots": slots})
            leader = []
            other = {kind: {r.get("username"): r for r in rows} for kind, rows in self.universe.rankings.items()}
            for row in self.universe.highscore:
                agent = by_user.get(row.get("username"))
                a = agents.get(agent) if agent else None
                extra = {kind: {"rank": (other.get(kind, {}).get(row.get("username")) or {}).get("rank"),
                                "score": (other.get(kind, {}).get(row.get("username")) or {}).get("score")} for kind in RANKING_TYPES}
                ships = (other.get("military", {}).get(row.get("username")) or {}).get("total_ships")
                leader.append({"rank": row.get("rank"), "username": row.get("username"), "score": row.get("score"),
                               "alliance": row.get("alliance_tag"), "rankings": extra, "ships": ships,
                               "agent": agent, "persona": a["persona"] if a else None,
                               "planets": planets_owned.get(row.get("username")) or (1 + a["colonies"] if a else None),
                               "attacks": a["attacks"] if a else None, "net_loot": a["net_loot"] if a else None,
                               "colonies": a["colonies"] if a else None})
            news = build_newspaper(events, self.news_hours, lambda c: owner_at.get(c) if c else None)
            news["board"] = notices_from(self.board.posts) if self.board.available else []
            every = wanted_notices(self.board.posts, events, self.board.now, limit=BOARD_KEEP) if self.board.available else []
            wanted = every[:8]
            news["wanted"] = wanted
            feed = [{"t": e["t"], "agent": e["agent"], "kind": e["event"], "persona": agents.get(e["agent"], {}).get("persona"),
                     "text": describe(e), "quote": e["event"] == "diary"} for e in events[-1500:] if e["event"] not in NOISE][-600:]
            feed.reverse()
            counters = [{"agent": n, **{k: v for k, v in agents[n].items() if k in ("persona", "attacks", "net_loot", "colonies")}}
                        for n in names]
            return {"generated": time.time(), "latest_event": latest, "refresh": self.refresh, "events_in_memory": len(events),
                    "personas": list(PERSONAS), "agents": agents, "kinds": sorted({f["kind"] for f in feed}),
                    "map": {"available": self.universe.available, "note": self.universe.note, "systems": systems,
                            "scanned": len(self.universe.systems)},
                    "newspaper": news, "feed": feed, "board": self._board_state(agents, every),
                    "leaderboard": {"available": bool(self.universe.highscore), "rows": leader, "agents": counters},
                    "llm": self.llm.summary(), "diplomacy": diplomacy_view(events)}


# ---- the web server -----------------------------------------------------------------------------------
def make_handler(observatory: Observatory):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path in ("/llm", "/llm.html"):
                self._send(200, llm_page.PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/llm":
                try:
                    q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    lim = int(dict(x.split("=", 1) for x in q.split("&") if "=" in x).get("limit", 200))
                    body = json.dumps(llm_page.llm_view(observatory.tail.folder, lim)).encode("utf-8")
                except Exception as e:
                    body = json.dumps({"available": False, "calls": [], "totals": {}, "agents": [], "purposes": [], "error": str(e)[:200]}).encode("utf-8")
                self._send(200, body, "application/json")
            elif path.startswith("/assets/"):
                f = (Path(__file__).resolve().parent / "assets" / path[len("/assets/"):]).resolve()
                root = (Path(__file__).resolve().parent / "assets").resolve()
                if root in f.parents and f.is_file():
                    ctype = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg"}.get(f.suffix.lower(), "application/octet-stream")
                    self._send(200, f.read_bytes(), ctype)
                else:
                    self._send(404, b"not found", "text/plain")
            elif path in ("/galaxy", "/galaxy.html"):
                self._send(200, galaxy_page.PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/galaxy":
                try:
                    body = json.dumps(observatory.galaxy_data()).encode("utf-8")
                except Exception as e:
                    body = json.dumps({"error": str(e)[:200]}).encode("utf-8")
                self._send(200, body, "application/json")
            elif path == "/api/planet":
                try:
                    q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&") if "=" in x)
                    from urllib.parse import unquote
                    body = json.dumps(observatory.planet_detail(int(q.get("id", "0")), unquote(q.get("owner", "")) or None,
                                                                unquote(q.get("name", "")))).encode("utf-8")
                except Exception as e:
                    body = json.dumps({"public": True, "note": str(e)[:200]}).encode("utf-8")
                self._send(200, body, "application/json")
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


class _Server(ThreadingHTTPServer):
    # On Windows the default (allow_reuse_address) lets a second program share a busy port without any error, and the
    # browser then talks to the OTHER program. Refuse a busy port instead.
    allow_reuse_address = False


def serve(observatory: Observatory, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return _Server((host, port), make_handler(observatory))


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
    ap.add_argument("--board", default=None, metavar="URL", help="the galaxy board (board.py), e.g. http://localhost:8765: adds the Board tab (read only)")
    args = ap.parse_args()

    token, _ = token_from_state(args.state)
    client = ReadOnlyClient(args.base_url, token) if token else None
    obs = Observatory(args.logs, args.state, client, args.galaxy, args.news_hours, args.refresh, BoardReader(args.board))
    obs.base_url = args.base_url
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
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--onaccent:#04101f;--slot:#242b36;--hole:#0d1117;--warn:#f0b429;
  --offer:#3ddc97;--want:#4cc9f0;--warning:#ffb703;--revenge:#ff4d6d;--bounty:#f9c74f;--claim:#ff9f43;--verdict:#43d9ad;--taunt:#c77dff;--alliance:#7b9cff;--hello:#5eead4;--reply:#f472b6;
  --metal:#b8c2d6;--crystal:#6ee7f9;--deuterium:#7dd87d;--bad:#ff6b6b;--good:#3ddc97;
  --paper:#f1e8d0;--ink:#2b2416;--paper-line:#b9ad8c;--paper-dim:#6b5f44}
@media (prefers-color-scheme: light){:root{--bg:#f3f5fb;--panel:#fff;--panel2:#f0f3fb;--line:#d9dfef;--text:#16203a;--dim:#5b6685;--accent:#1f6feb;--onaccent:#fff;--slot:#e3e8f3;--hole:#fff;--warn:#9a5b00;
  --offer:#0a9b64;--want:#1479a8;--warning:#b36b00;--revenge:#cc1f44;--bounty:#8a6500;--claim:#b25400;--verdict:#0b7a5f;--taunt:#8a3fcf;--alliance:#3b5bd6;--hello:#0f8a7a;--reply:#c0307a;
  --metal:#667089;--crystal:#0e96ab;--deuterium:#3c9a3c;--bad:#c62828;--good:#0a8a55;
  --paper:#fbf5e4;--ink:#2b2416;--paper-line:#cfc4a4;--paper-dim:#6b5f44}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}
header{display:flex;gap:10px 16px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:16px;margin:0}.dim{color:var(--dim)}
nav{display:flex;gap:6px;flex-wrap:wrap}
nav button{background:none;border:1px solid var(--line);color:var(--text);padding:6px 14px;border-radius:6px;cursor:pointer;font:inherit}
nav button.on{background:var(--accent);border-color:var(--accent);color:var(--onaccent)}
#status{font-size:12px}
main{padding:16px;max-width:1300px;margin:0 auto}.tab{display:none}.tab.on{display:block}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px}.legend span{display:inline-flex;align-items:center;gap:5px}.dot{width:12px;height:12px;border-radius:3px;display:inline-block}
.systems{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:10px;margin-top:12px}
.sys{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px}
.sys h3{margin:0 0 6px;font-size:12px;color:var(--dim)}
.slots{display:grid;grid-template-columns:repeat(15,1fr);gap:2px}
.slot{aspect-ratio:1;border-radius:3px;background:var(--slot);position:relative;cursor:default}
.slot.col::after{content:"";position:absolute;inset:3px;border-radius:50%;background:var(--hole);opacity:.75}
.slot.bat{outline:2px solid #ff4d4d;outline-offset:1px}
#info{position:sticky;bottom:0;margin-top:12px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;min-height:60px;overflow-wrap:anywhere}
/* newspaper */
.paper{background:var(--paper);color:var(--ink);border-radius:6px;padding:20px 26px;font-family:Georgia,'Times New Roman',serif;box-shadow:0 1px 0 var(--line)}
.paper .mast{text-align:center;border-bottom:3px double var(--ink);padding-bottom:8px;margin-bottom:14px}
.paper .mast h2{margin:0;font-size:clamp(26px,6vw,40px);letter-spacing:1px}.paper .date{font-size:13px;font-style:italic;color:var(--paper-dim)}
.kicker{font:700 11px/1 system-ui,Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--k,#7a2f1d);margin-bottom:6px;display:block}
.lead{border-bottom:1px solid var(--paper-line);padding-bottom:16px;margin-bottom:16px}
.paper .lead h3{margin:0 0 8px;font-size:clamp(26px,5vw,42px);line-height:1.1;letter-spacing:-.01em}
.lead p{font-size:clamp(16px,2.2vw,19px);line-height:1.5;max-width:60ch}
.lead .when,.story .when{font:12px system-ui,Arial,sans-serif;color:var(--paper-dim);margin-top:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr));gap:0 22px;margin-bottom:6px}
.story{padding:0 0 14px;margin-bottom:14px;border-bottom:1px solid var(--paper-line)}
.story h4{margin:0 0 4px;font-size:19px;line-height:1.2}.story p{margin:0;font-size:14.5px}
.paper h3{margin:0 0 4px;font-size:20px}.paper .sect{font:700 12px system-ui,Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--paper-dim);margin:6px 0 10px}
.paper .cols{columns:2;column-gap:28px}
.paper article{break-inside:avoid;margin-bottom:14px;border-bottom:1px solid var(--paper-line);padding-bottom:8px}.paper p{margin:0}
.paper article h3{font-size:18px}
@media(max-width:800px){.paper .cols{columns:1}.paper{padding:16px}}
.paper .notices{border-top:3px double var(--ink);margin-top:10px;padding-top:8px}.paper .notices h3{font-size:18px}.paper .notices p{margin:4px 0}
/* filters, feed */
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
select,input{background:var(--panel);border:1px solid var(--line);color:var(--text);padding:5px 8px;border-radius:6px;font:inherit;max-width:100%}
.row{padding:5px 0;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:baseline}.row .t{color:var(--dim);font-variant-numeric:tabular-nums;flex:none}.row .a{font-weight:600}
.row .kd{width:8px;height:8px;border-radius:50%;flex:none;align-self:center;background:var(--c,var(--dim))}
.row .body{min-width:0;overflow-wrap:anywhere}
.row .quote{font-style:italic;border-left:3px solid var(--taunt);padding-left:8px}.row .quote b{font-style:normal;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin-right:6px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-top:8px}
table{border-collapse:collapse;width:100%;min-width:760px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:12px;background:var(--panel2);position:sticky;top:0}td.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}tr.ours td:first-child{box-shadow:inset 3px 0 0 var(--c,var(--accent))}
.pd{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--c,var(--dim))}
.warn{color:var(--warn)}.bar{margin-bottom:6px}
/* galaxy board cards */
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px;align-items:center}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:999px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.chip b{margin-left:6px;color:var(--dim);font-weight:600}
.chip.on{border-color:var(--c,var(--text));box-shadow:inset 0 0 0 1px var(--c,var(--text))}
.cards{max-width:860px}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:12px;padding:12px 14px;margin:0 0 10px;overflow-wrap:anywhere}
.card.reply{margin-left:28px;background:var(--panel2)}
.top{display:flex;align-items:center;gap:10px}
.av{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;color:#0b0f1a;flex:none}
.who{font-weight:650;white-space:nowrap}.top>div:nth-child(2){min-width:0}.age{color:var(--dim);font-size:12px}
.badges{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.badge{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--c);border:1px solid var(--c);border-radius:6px;padding:2px 7px;text-transform:uppercase}
.badge.st{background:var(--c);color:var(--bg)}
.text{margin:8px 0 2px}
.deal{display:flex;align-items:center;gap:10px;margin:10px 0 2px;flex-wrap:wrap}
.lot{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px 12px}
.card.reply .lot{background:var(--panel)}
.lot small{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.lot span{font-weight:700;font-variant-numeric:tabular-nums;display:block}
.arrow{color:var(--dim);font-size:20px}
.res{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.tag{font-size:12px;color:var(--dim);background:var(--panel2);border-radius:6px;padding:2px 8px}
.card.reply .tag{background:var(--panel)}
.empty{color:var(--dim);padding:30px 0}
@media(max-width:520px){main{padding:12px}.card.reply{margin-left:14px}.top{flex-wrap:wrap}.badges{margin-left:44px;flex-basis:100%;justify-content:flex-start}}
</style></head><body>
<header><h1>Observatory</h1><nav id="nav"></nav><a href="/galaxy" style="color:var(--accent)">Galaxy map</a><a href="/llm" style="color:var(--accent)">LLM log</a><span id="status" class="dim"></span></header>
<main>
<section id="map" class="tab on"><div class="legend" id="legend"></div><div id="mapnote" class="warn"></div><div class="systems" id="systems"></div><div id="info" class="dim">Hover a planet.</div></section>
<section id="news" class="tab"><div class="paper" id="paper"></div></section>
<section id="feed" class="tab"><div class="filters"><select id="fa"></select><select id="fp"></select><select id="fk"></select><input id="ft" placeholder="search text"></div><div id="rows"></div></section>
<section id="galaxyboard" class="tab"><div id="gnote" class="warn"></div><div class="chips" id="gchips"></div><div class="cards" id="gposts"></div></section>
<section id="diplo" class="tab"><div class="dim">Relationships as the agents last logged them (contact, neutral, friendly, pact, alliance). Read only, from the logs.</div><div id="diplotable"></div></section>
<section id="board" class="tab"><div class="bar">Sort by <select id="bsort"><option value="general">General</option><option value="economy">Economy</option><option value="research">Research</option><option value="military">Military</option></select> <span class="dim">Alliance stays empty until agents found alliances.</span></div><div id="boardnote" class="warn"></div><div id="boardtable"></div></section>
</main>
<script>
const KC={OFFER:"var(--offer)",WANT:"var(--want)",WARNING:"var(--warning)",REVENGE:"var(--revenge)",BOUNTY:"var(--bounty)",CLAIM:"var(--claim)",VERDICT:"var(--verdict)",TAUNT:"var(--taunt)",ALLIANCE:"var(--alliance)",HELLO:"var(--hello)",REPLY:"var(--reply)"};
const KCP={OFFER:"#0a7a4f",WANT:"#116a92",WARNING:"#9a5b00",REVENGE:"#b3193b",BOUNTY:"#8a6500",CLAIM:"#b25400",VERDICT:"#0b7a5f",TAUNT:"#7a2fb8",ALLIANCE:"#2f4cc0",HELLO:"#0f8a7a",REPLY:"#c0307a"};
const STORYC={bounty:"#8a6500",battle:"#b3193b",defence:"#b3193b",colony:"#0b7a5f",trade:"#116a92",fleet:"#2f4cc0",revenge:"#b3193b",lesson:"#7a2fb8",class:"#6b5f44",quiet:"#6b5f44"};
const COLORS={miner:"#d9a441",scientist:"#4aa3ff",turtle:"#4fbf7a",raider:"#e5534b",balanced:"#b07cf0",other:"#6b7280"};
if(window.matchMedia&&matchMedia("(prefers-color-scheme: light)").matches)Object.assign(COLORS,{miner:"#a8730f",scientist:"#1a6fcf",turtle:"#1e8a4c",raider:"#c62f27",balanced:"#7d3fc4"});
const STATUSC={open:"var(--bounty)",claimed:"var(--claim)",confirmed:"var(--verdict)",paid:"var(--offer)",unpaid:"var(--revenge)",rejected:"var(--revenge)",expired:"var(--dim)"};
const $=id=>document.getElementById(id);
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function fmt(n){return n==null?"":Number(n).toLocaleString("en-US")}
function when(t){return new Date(t*1000).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}
function hue(name){let h=0;for(const c of String(name))h=(h*31+c.charCodeAt(0))%360;return "hsl("+h+" 70% 62%)"}
function ago(t,now){const s=Math.max(0,Math.round(now-t));if(s<60)return s+" s ago";if(s<3600)return Math.round(s/60)+" min ago";if(s<86400)return (s/3600).toFixed(1)+" h ago";return Math.round(s/86400)+" d ago"}
function kindColor(k){k=String(k);
  if(/error|failed|cheated|unpaid|refused/.test(k))return "var(--revenge)";
  if(/battle|attack|threat|revenge/.test(k))return "var(--claim)";
  if(/bounty/.test(k))return "var(--bounty)";
  if(/trade|supply|recycle/.test(k))return "var(--offer)";
  if(/colony|expand|registered|class/.test(k))return "var(--want)";
  if(/diplo|contact/.test(k))return "var(--hello)";
  if(/board|lesson|diary|voice/.test(k))return "var(--taunt)";
  if(/defend|fleet/.test(k))return "var(--alliance)";
  return "var(--dim)"}
let S=null,tab="map",gfilter=null;
const tabs=[["map","Map"],["news","Newspaper"],["feed","Feed"],["galaxyboard","Board"],["diplo","Diplomacy"],["board","Leaderboard"]];
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
// ---- newspaper
function kicker(a){const k=el("span","kicker",a.kicker||a.kind);k.style.setProperty("--k",STORYC[a.kind]||"#7a2f1d");return k}
function drawNews(){
  const n=S.newspaper,p=$("paper");p.replaceChildren();
  const mast=el("div","mast");mast.appendChild(el("h2","","The Daily Ogame"));mast.appendChild(el("div","date",`${n.dateline} - last ${n.hours} h, ${n.events_considered} events`));p.appendChild(mast);
  const h=el("div","lead");h.appendChild(kicker(n.headline));h.appendChild(el("h3","",n.headline.title));h.appendChild(el("p","",n.headline.text));
  if(n.headline.t)h.appendChild(el("div","when",when(n.headline.t)));p.appendChild(h);
  if(n.secondary&&n.secondary.length){const g=el("div","grid");
    n.secondary.forEach(a=>{const s=el("div","story");s.appendChild(kicker(a));s.appendChild(el("h4","",a.title));s.appendChild(el("p","",a.text));s.appendChild(el("div","when",when(a.t)));g.appendChild(s)});p.appendChild(g)}
  if(n.articles.length){p.appendChild(el("div","sect","More news"));
    const cols=el("div","cols");n.articles.forEach(a=>{const r=el("article");r.appendChild(kicker(a));r.appendChild(el("h3","",a.title));r.appendChild(el("p","",a.text));cols.appendChild(r)});p.appendChild(cols)}
  if(n.board&&n.board.length){const b=el("div","notices");b.appendChild(el("h3","","From the galaxy board"));n.board.forEach(x=>{const r=el("p","");const k=el("b","",x.kind+" ");k.style.color=KCP[x.kind]||"inherit";r.appendChild(k);r.append(`${x.agent}: ${x.text}`);b.appendChild(r)});p.appendChild(b)}
  if(n.wanted&&n.wanted.length){const w=el("div","notices");w.appendChild(el("h3","","Wanted"));
    n.wanted.forEach(x=>{const r=el("p","");const k=el("b","","WANTED ");k.style.color=KCP.BOUNTY;r.appendChild(k);
      r.append(`player ${x.target_id==null?"?":x.target_id} at ${x.target_at}. Reward ${x.reward}, posted by ${x.poster}${x.claimant?`, claimed by ${x.claimant}`:""}: `);
      const st=el("i","",x.status);r.appendChild(st);w.appendChild(r)});p.appendChild(w)}}
// ---- feed
function fillSelect(sel,label,values){const cur=sel.value;sel.replaceChildren();sel.appendChild(new Option(label,""));values.forEach(v=>sel.appendChild(new Option(v,v)));sel.value=values.includes(cur)?cur:""}
function drawFeed(){
  fillSelect($("fa"),"all agents",Object.keys(S.agents));fillSelect($("fp"),"all personalities",S.personas);fillSelect($("fk"),"all kinds",S.kinds);
  const fa=$("fa").value,fp=$("fp").value,fk=$("fk").value,ft=$("ft").value.toLowerCase();
  const rows=$("rows");rows.replaceChildren();let shown=0;
  for(const f of S.feed){if(fa&&f.agent!==fa||fp&&f.persona!==fp||fk&&f.kind!==fk||ft&&!(f.agent+" "+f.text).toLowerCase().includes(ft))continue;
    const r=el("div","row");r.style.setProperty("--c",kindColor(f.kind));const d=el("span","kd");d.title=f.kind;
    const body=el("span","body");const a=el("span","a",f.agent);a.style.color=COLORS[f.persona]||COLORS.other;
    if(f.quote){const q=el("span","quote");q.appendChild(el("b","","Diary"));q.append("\u201c"+f.text+"\u201d");body.append(a," ",q)}else body.append(a," ",f.text);
    r.append(d,el("span","t",when(f.t)),body);rows.appendChild(r);if(++shown>=300)break}}
// ---- galaxy board: the cards of the board page
function lot(label,obj){const box=el("div","lot");box.appendChild(el("small","",label));
  for(const [r,v] of Object.entries(obj||{})){const line=el("span","");const sq=el("i","res");sq.style.background="var(--"+r+",var(--dim))";line.append(sq,fmt(v)+" "+r);box.appendChild(line)}return box}
function statusWord(s){s=String(s||"");if(s.startsWith("paid"))return "paid";if(s==="unpaid")return "unpaid";if(s.startsWith("confirmed"))return "confirmed";
  if(s.includes("rejected"))return "rejected";if(s.startsWith("claimed"))return "claimed";return s||"open"}
function postCard(p,isReply,now){
  const d=p.data||{};
  const c=el("div","card"+(isReply?" reply":""));c.style.setProperty("--c",KC[p.kind]||"var(--dim)");
  const top=el("div","top");const av=el("div","av",((String(p.agent).match(/\d+/)||[String(p.agent)[0]||"?"])[0]).slice(-2));av.style.background=hue(p.agent);
  const who=el("div","");const nm=el("div","who",p.agent);nm.style.color=COLORS[p.persona]||"inherit";who.appendChild(nm);
  const age=el("div","age",ago(p.t,now));age.title=when(p.t);who.appendChild(age);
  const badges=el("div","badges");
  if(p.kind==="BOUNTY"){const w=statusWord(p.status);const st=el("span","badge st",w);st.style.setProperty("--c",STATUSC[w]||"var(--dim)");badges.appendChild(st)}
  badges.appendChild(el("div","badge",p.kind));top.append(av,who,badges);c.appendChild(top);
  c.appendChild(el("div","text",(p.text||"")));
  if(d.give||d.want){const deal=el("div","deal");deal.appendChild(lot("gives",d.give));deal.appendChild(el("span","arrow","⇄"));deal.appendChild(lot("wants",d.want));c.appendChild(deal)}
  if(p.kind==="BOUNTY"&&d.reward){const deal=el("div","deal");deal.appendChild(lot("wanted: reward",d.reward));c.appendChild(deal)}
  const meta=el("div","meta"),tag=t=>meta.appendChild(el("span","tag",t));
  if(p.kind==="BOUNTY"){if(d.target_player_id!=null)tag("wanted: player "+d.target_player_id);if(d.target_at)tag("last seen at "+d.target_at);if(d.poster_at)tag("pays from "+d.poster_at+" after the referee confirms")}
  if(p.kind==="CLAIM"){if(d.bounty_id!=null)tag("claims bounty #"+d.bounty_id);if(d.loot)tag("loot "+fmt(d.loot));if(d.report!=null)tag("battle report "+d.report);if(d.claimant_at)tag("pay to "+d.claimant_at)}
  if(p.kind==="VERDICT"){const v=el("span","tag",d.ok===true?"CONFIRMED":"REJECTED");v.style.color=d.ok===true?"var(--offer)":"var(--revenge)";v.style.fontWeight="700";meta.appendChild(v);
    if(d.why)tag(String(d.why));if(d.claimant)tag("claimant "+d.claimant)}
  if(p.kind==="REVENGE"&&d.reply_to)tag("joins the call #"+d.reply_to);
  if(p.kind==="HELLO"){if(d.to_name)tag("to "+d.to_name);if(d.step)tag("proposes: "+d.step)}
  if(p.kind==="REPLY"){const v=el("span","tag",String(d.decision||"").toUpperCase());v.style.fontWeight="700";v.style.color=String(d.decision).toUpperCase()==="ACCEPT"?"var(--offer)":"var(--revenge)";meta.appendChild(v);if(d.step)tag("for: "+d.step)}
  if(p.kind==="ALLIANCE"&&d.announce)tag("announcement: "+d.announce);
  if(d.at)tag("at "+d.at);if(d.raider_at)tag("raider at "+d.raider_at);if(d.raider_id!=null)tag("raider: player "+d.raider_id);if(d.loot&&p.kind!=="CLAIM")tag("robbed of "+fmt(d.loot));
  if(p.expires)tag(p.expires<now?"expired":"expires in "+Math.max(0,Math.round((p.expires-now)/60))+" min");
  if(meta.childNodes.length)c.appendChild(meta);
  return c}
function drawGalaxyBoard(){
  const g=S.board,box=$("gposts"),chips=$("gchips"),now=g.now||S.generated;box.replaceChildren();chips.replaceChildren();
  $("gnote").textContent=g.available?"":(g.note||"");
  const posts=g.posts||[],counts=g.counts||{};
  if(gfilter&&!counts[gfilter])gfilter=null;
  const all=el("button","chip"+(gfilter?"":" on"),"All");all.appendChild(el("b","",String(posts.length)));all.onclick=()=>{gfilter=null;drawGalaxyBoard()};chips.appendChild(all);
  Object.keys(KC).forEach(k=>{if(!counts[k])return;const b=el("button","chip"+(gfilter===k?" on":""),k.toLowerCase());b.style.setProperty("--c",KC[k]);b.appendChild(el("b","",String(counts[k])));b.onclick=()=>{gfilter=k;drawGalaxyBoard()};chips.appendChild(b)});
  Object.keys(counts).filter(k=>!KC[k]).forEach(k=>{const b=el("button","chip"+(gfilter===k?" on":""),k.toLowerCase());b.appendChild(el("b","",String(counts[k])));b.onclick=()=>{gfilter=k;drawGalaxyBoard()};chips.appendChild(b)});
  const byId=new Map(posts.map(p=>[String(p.id),p])),replies=new Map();
  const parent=p=>{const d=p.data||{};const r=(p.kind==="CLAIM"||p.kind==="VERDICT")?d.bounty_id:d.reply_to;return r==null?null:String(r)};
  posts.forEach(p=>{const r=parent(p);if(r!==null&&byId.has(r)&&r!==String(p.id)){(replies.get(r)||replies.set(r,[]).get(r)).push(p)}});
  const isReply=p=>{const r=parent(p);return r!==null&&byId.has(r)&&r!==String(p.id)};
  let shown=0;
  for(const p of [...posts].sort((a,b)=>b.t-a.t)){
    if(isReply(p)&&!gfilter)continue;if(gfilter&&p.kind!==gfilter)continue;
    box.appendChild(postCard(p,false,now));
    if(!gfilter)(replies.get(String(p.id))||[]).sort((a,b)=>a.t-b.t).forEach(r=>box.appendChild(postCard(r,true,now)));
    if(++shown>=200)break}
  if(!shown&&g.available)box.appendChild(el("div","empty","The board is empty. Posts appear here as the agents trade, warn and call for revenge."))}
// ---- leaderboard
function drawBoard(){
  const b=S.leaderboard,box=$("boardtable");box.replaceChildren();
  $("boardnote").textContent=b.available?"":"The game's highscore is unavailable: showing what the logs say.";
  const wrap=el("div","tablewrap"),t=el("table");const head=el("tr");(b.available?["Player","Alliance","General","Economy","Research","Military","Ships","Agent","Planets","Attacks","Net loot","Colonies"]:["Agent","Personality","Attacks","Net loot","Colonies"]).forEach(x=>head.appendChild(el("th","",x)));t.appendChild(head);
  const add=(cells,persona)=>{const r=el("tr",persona?"ours":"");if(persona)r.style.setProperty("--c",COLORS[persona]||COLORS.other);
    cells.forEach((c,i)=>{const td=el("td",typeof c==="number"?"n":"");
      if(i===0&&persona){const d=el("span","pd");d.style.setProperty("--c",COLORS[persona]||COLORS.other);td.append(d,String(c))}else td.textContent=typeof c==="number"?fmt(c):(c==null?"":c);
      if(typeof c==="number"&&c<0)td.style.color="var(--bad)";r.appendChild(td)});t.appendChild(r)};
  const rk=(r,k)=>{const v=k==="general"?{rank:r.rank,score:r.score}:(r.rankings&&r.rankings[k])||{};return v.rank==null?"":`#${v.rank}  (${fmt(v.score||0)})`};
  const key=$("bsort").value;
  const sortRank=r=>{const v=key==="general"?r.rank:(r.rankings&&r.rankings[key]&&r.rankings[key].rank);return v==null?1e9:v};
  if(b.available)b.rows.slice().sort((x,y)=>sortRank(x)-sortRank(y)).forEach(r=>add([r.username,r.alliance||"",rk(r,"general"),rk(r,"economy"),rk(r,"research"),rk(r,"military"),r.ships,r.agent?`${r.agent} (${r.persona||"?"})`:"",r.planets,r.attacks,r.net_loot,r.colonies],r.agent?(r.persona||"other"):null));
  else b.agents.forEach(a=>add([a.agent,a.persona||"?",a.attacks,a.net_loot,a.colonies],a.persona||"other"));
  wrap.appendChild(t);box.appendChild(wrap)}
function drawDiplo(){
  const box=$("diplotable");box.replaceChildren();const rows=(S.diplomacy&&S.diplomacy.rows)||[];
  if(!rows.length){box.appendChild(el("div","empty","No relationships yet. They appear as agents meet (needs the swarm; pacts need the board)."));return}
  const wrap=el("div","tablewrap"),t=el("table"),head=el("tr");["Agent","Other player","Step","Trust","Last change"].forEach(x=>head.appendChild(el("th","",x)));t.appendChild(head);
  rows.forEach(r=>{const tr=el("tr","");tr.append(el("td","",r.agent),el("td","",r.with),el("td","",r.step),el("td","n",String(Math.round(r.trust))),el("td","",r.t?when(r.t):""));t.appendChild(tr)});
  wrap.appendChild(t);box.appendChild(wrap);
  try{const sl=(S.diplomacy&&S.diplomacy.slots)||[];if(sl.length){const w2=el("div","tablewrap"),t2=el("table"),h2=el("tr");
    ["Agent","Pact slots used","Next pact expiry"].forEach(x=>h2.appendChild(el("th","",x)));t2.appendChild(h2);
    sl.forEach(r=>{const tr=el("tr","");tr.append(el("td","",r.agent),el("td","n",`${r.used}/${r.slots}`),el("td","",r.next_expiry?when(r.next_expiry):"-"));t2.appendChild(tr)});
    w2.appendChild(t2);box.appendChild(w2)}}catch(e){}}
function draw(){drawMap();drawNews();drawFeed();drawGalaxyBoard();drawDiplo();drawBoard();
  $("status").textContent=`${S.events_in_memory} events - last at ${S.latest_event?when(S.latest_event):"never"} - map ${S.map.available?"ok":"unavailable"}${S.llm?` - LLM ${S.llm.calls} calls${S.llm.failed?` (${S.llm.failed} failed)`:""}, avg ${S.llm.avg_latency} s`:""}`}
async function load(){try{const r=await fetch("/api/state");const j=await r.json();if(j.error){$("status").textContent="error: "+j.error}else{S=j;draw()}}catch(e){$("status").textContent="cannot reach the observatory"}
  setTimeout(load,((S&&S.refresh)||30)*1000)}
$("bsort").onchange=()=>S&&drawBoard();
["fa","fp","fk"].forEach(i=>$(i).onchange=()=>S&&drawFeed());$("ft").oninput=()=>S&&drawFeed();
load();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
