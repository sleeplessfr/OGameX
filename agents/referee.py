#!/usr/bin/env python3
"""The referee: confirms or rejects bounty claims, so a poster knows when it owes the reward.

    python referee.py --board http://localhost:8765 --logs logs

A separate program, like board.py. It READS the board (BOUNTY, CLAIM, VERDICT posts) and the agents' logs (`logs/*.jsonl`:
the `attack` and `battle` events the agents write) and never touches the game, the logs or anyone's files. Its one output is a
VERDICT post on the board for every CLAIM:

    data: bounty_id, claim_id, ok (true / false), why, claimant, claimant_id, claimant_at

A claim is OK when the claimant's own log shows (all of these):
  - a BOUNTY by somebody else exists (claimant is not the poster, not the poster's player id), and has not been paid out already
    (one claim per bounty: the first valid one wins);
  - an `attack` event of the claimant on the target, sent AFTER the bounty was posted;
  - a `battle` event as attacker on the target, after the bounty was posted and before it expired, with loot (a win);
  - the target is the bounty's target (the player id the battle report names, or the bounty's coordinates);
  - the claim does not claim more loot than the log shows.

A claim whose evidence is not in the logs yet waits up to a minute (the agent writes its log line a moment before it claims).

HONESTY. A verdict is only as honest as the logs: the referee believes what the agent wrote in its own log file. The agents are
our own code and write what happened, and the referee is ours too (it is a separate process on this computer that agents cannot
talk to except through the board), so an agent cannot lie to it by posting something, but whoever edits a log file can. The
board does not know who is who either: only VERDICT posts from the board name `referee` are believed by the agents.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from swarm import bounty as B
from swarm import social as S
from swarm.boardclient import BoardClient, is_local_url

NAME = B.REFEREE
GRACE = 60.0                  # wall seconds a claim may wait for the evidence to appear in the logs
VERDICT_TTL = 12 * 3600.0
KEEP_EVENTS = 1000            # attack / battle events kept per agent
TOLERANCE = 1.0               # log times are whole seconds: an event in the same second as the bounty post counts as after it
WATCHED = ("attack", "battle")


# ---- the logs --------------------------------------------------------------------------------------------------------
class LogIndex:
    """The `attack` and `battle` events of every agent, read incrementally from logs/*.jsonl (read only, tolerant of half lines)."""

    def __init__(self, log_dir: str | Path):
        self.dir = Path(log_dir)
        self.offsets: dict[str, int] = {}
        self.events: dict[str, dict[str, list[dict]]] = {}

    def poll(self) -> None:
        try:
            files = sorted(self.dir.glob("*.jsonl"))
        except OSError:
            return
        for f in files:
            try:
                size = f.stat().st_size
                off = self.offsets.get(f.name, 0)
                if size < off:                       # the file was replaced: start again
                    off = 0
                    self.events.pop(f.stem, None)
                if size == off:
                    continue
                with f.open("rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1               # only whole lines
            self.offsets[f.name] = off + end
            for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("event") not in WATCHED or not isinstance(e.get("t"), (int, float)):
                    continue
                bucket = self.events.setdefault(str(e.get("agent") or f.stem), {"attack": [], "battle": []})[e["event"]]
                bucket.append(e)
                del bucket[:-KEEP_EVENTS]

    def of(self, agent: str, kind: str) -> list[dict]:
        return self.events.get(agent, {}).get(kind, [])


# ---- reading the posts -----------------------------------------------------------------------------------------------------
def parse_claim(post: dict) -> dict | None:
    if post.get("kind") != "CLAIM" or not isinstance(post.get("data"), dict) or post.get("id") is None:
        return None
    d = post["data"]
    try:
        bid, cid_ = int(d["bounty_id"]), int(d["claimant_id"])
    except (KeyError, TypeError, ValueError):
        return None
    at = S.parse_coord(d.get("claimant_at"))
    if at is None:
        return None
    try:
        loot = int(d.get("loot") or 0)
    except (TypeError, ValueError):
        loot = 0
    return {"id": post["id"], "agent": str(post.get("agent")), "t": float(post.get("t") or 0), "bounty": bid, "claimant_id": cid_,
            "claimant_at": S.fmt_coord(at), "at": d.get("at"), "report": d.get("report"), "loot": loot}


def _coord_text(c) -> str | None:
    p = S.parse_coord(c)
    return S.fmt_coord(p) if p else None


# ---- the judgement -----------------------------------------------------------------------------------------------------------------
def judge(claim: dict, bounty: dict | None, index: LogIndex, paid: dict, now: float) -> tuple[bool, str] | None:
    """(ok, why), or None when the evidence is not in the logs yet and the claim is still young. `paid` maps bounty id -> the claim
    id that was confirmed. Only the logs and the two posts are used."""
    if bounty is None:
        return False, "no such bounty (never posted, or already expired)"
    me = claim["agent"]
    if me == bounty["poster"] or claim["claimant_id"] == bounty["poster_id"]:
        return False, "the claimant is the poster of the bounty"
    if paid.get(bounty["id"]) not in (None, claim["id"]):
        return False, "the bounty was already claimed by an earlier claim"
    after = bounty["t"] - TOLERANCE
    target_at = _coord_text(bounty["at"])

    def on_target(ev: dict) -> bool:
        return ev.get("their_id") == bounty["pid"] or _coord_text(ev.get("at")) == target_at

    mine = [ev for ev in index.of(me, "battle") if ev.get("role") == "attacker" and on_target(ev)]
    if claim.get("at") is not None:
        mine = [ev for ev in mine if _coord_text(ev.get("at")) == _coord_text(claim["at"])]
    if claim.get("report") is not None:
        mine = [ev for ev in mine if ev.get("report") in (None, claim["report"])]
    if not mine:
        if now - claim["t"] < GRACE:
            return None
        return False, "no battle of the claimant against the bounty's target in its log"
    late = [ev for ev in mine if ev["t"] > bounty["expires"]]
    fresh = [ev for ev in mine if after <= ev["t"] <= bounty["expires"]]
    if not fresh:
        return False, "the battle was after the bounty expired" if late else "the battle was before the bounty was posted"
    wins = [ev for ev in fresh if int(ev.get("loot") or 0) > 0]
    if not wins:
        return False, "the battle brought no loot: not a win"
    for ev in sorted(wins, key=lambda e: e["t"]):
        attacks = [a for a in index.of(me, "attack") if _coord_text(a.get("at")) == _coord_text(ev.get("at"))
                   and after <= a["t"] <= ev["t"]]
        if not attacks:
            continue
        if claim["loot"] > int(ev.get("loot") or 0):
            return False, "the claim names more loot than the log shows"
        return True, f"confirmed: attack and winning battle on {ev.get('at')} after the bounty, loot {int(ev.get('loot') or 0):,}"
    sent_before = [a for a in index.of(me, "attack") if a["t"] < after]
    return False, ("the attack was sent before the bounty was posted" if sent_before
                   else "no attack of the claimant on that planet was sent after the bounty was posted")


# ---- the referee ----------------------------------------------------------------------------------------------------------------------------
class Referee:
    def __init__(self, board, index: LogIndex, clock=time.time):
        self.board, self.index, self.clock = board, index, clock
        self.cursor = 0
        self.bounties: dict[int, dict] = {}
        self.pending: dict[int, dict] = {}       # claim id -> claim, not judged yet
        self.judged: set[int] = set()
        self.paid: dict[int, int] = {}           # bounty id -> the claim that was confirmed
        self.reachable = False

    def _absorb(self, posts: list[dict]) -> None:
        for p in posts:
            kind = p.get("kind")
            if kind == "BOUNTY":
                b = B.parse_bounty(p)
                if b is not None:
                    self.bounties[b["id"]] = b
            elif kind == "CLAIM":
                c = parse_claim(p)
                if c is not None and c["id"] not in self.judged:
                    self.pending[c["id"]] = c
            elif kind == "VERDICT" and p.get("agent") == NAME and isinstance(p.get("data"), dict):
                d = p["data"]
                try:
                    cid, bid = int(d["claim_id"]), int(d["bounty_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                self.judged.add(cid)
                self.pending.pop(cid, None)
                if d.get("ok") is True:
                    self.paid[bid] = cid

    def step(self) -> list[dict]:
        """One pass: read the board and the logs, judge what can be judged. Returns the verdicts posted."""
        r = self.board.read(since=self.cursor, kind=["BOUNTY", "CLAIM", "VERDICT"], limit=200)
        self.reachable = bool(r["ok"])
        if not r["ok"]:
            return []
        self.cursor = max(self.cursor, r["last_id"] if not r["posts"] else max(p["id"] for p in r["posts"]))
        self._absorb(r["posts"])
        self.index.poll()
        now = r["now"] or self.clock()
        posted = []
        for cid in sorted(self.pending):
            claim = self.pending[cid]
            result = judge(claim, self.bounties.get(claim["bounty"]), self.index, self.paid, now)
            if result is None:
                continue
            ok, why = result
            bounty = self.bounties.get(claim["bounty"]) or {}
            data = {"bounty_id": claim["bounty"], "claim_id": cid, "ok": ok, "why": why[:120], "claimant": claim["agent"],
                    "claimant_id": claim["claimant_id"], "claimant_at": claim["claimant_at"]}
            text = (f"Claim {cid} on bounty {claim['bounty']} by {claim['agent']}: "
                    f"{'CONFIRMED' if ok else 'REJECTED'}. {why}")[:200]
            post = self.board.post("VERDICT", text, data, ttl=VERDICT_TTL)
            if post is None:
                break                    # the board refused or is away: try again next pass
            del self.pending[cid]
            self.judged.add(cid)
            if ok:
                self.paid[claim["bounty"]] = cid
            posted.append(post)
            print(f"[referee] claim {cid} by {claim['agent']} on bounty {claim['bounty']} (poster {bounty.get('poster')}): "
                  f"{'OK' if ok else 'NO'} - {why}", flush=True)
        for bid in [b for b, v in self.bounties.items() if v["expires"] < now - 86400]:
            del self.bounties[bid]
        return posted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, metavar="URL", help="the galaxy board, e.g. http://localhost:8765")
    ap.add_argument("--logs", default="logs", help="the agents' log folder (the one run_swarm.py writes: --log-dir)")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between two passes")
    ap.add_argument("--once", action="store_true", help="one pass and stop (for scripts)")
    args = ap.parse_args()
    if not is_local_url(args.board):
        ap.error("--board must be an http://localhost:PORT or http://127.0.0.1:PORT address")
    if not Path(args.logs).is_dir():
        print(f"No folder named {args.logs!r} here. Run this from the agents folder, or pass --logs.", file=sys.stderr)
        return 1
    ref = Referee(BoardClient(args.board, NAME), LogIndex(args.logs))
    print(f"Referee on {args.board}, reading logs in {args.logs}. Ctrl+C to stop.")
    try:
        while True:
            ref.step()
            if args.once:
                break
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("\nStopping...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
