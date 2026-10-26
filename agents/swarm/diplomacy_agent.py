"""The network side of diplomacy.py: Agent methods for first contact and the ladder of relationships (mixed into Agent in agent.py).

What happens, in plain words:
  * Contact: a spy report, a battle, a board post, a probe or fleet we saw coming, or a neighbour within 2 systems makes another AGENT
    "known" (humans are left out). Log event `contact`. The first opinion comes from the highscore and the personality.
  * Proposals: an agent in `contact` (or `friendly` with enough trust, or `pact` with more trust and a shared threat) posts a HELLO on the
    galaxy board, one per ~3 hours, one open at a time. The addressed agent reads it, checks the HARD RULES in code, and only then asks the
    language model (ACCEPT / REFUSE / WAIT first word) or, with no model, a deterministic rule. It posts a REPLY. Nothing here waits for
    the model: a request is sent with a callback and its answer is picked up on a later round (like the diary in llm_agent.py).
  * Effects: a pact or alliance partner is never a raid target (`_without_allies` and `_try_attack` look at `_diplo_peace_ids`); an alliance
    partner's in-game alliance is preferred when applying (`_alli_free`).
Everything is fail-soft: an exception here is logged once (`diplo_error`) and the agent carries on.

Pact limits (README, Diplomacy): `pact_slots(persona)` caps simultaneous pact and alliance partners, a raider only pacts with someone it could
not profitably raid, a pact lasts PACT_LIFE (renewed by peaceful time, else back to `friendly`), and a ruthless agent may rarely break one.

Log events: contact, diplo_hello, diplo_answer, diplo_step, diplo_trust, diplo_announce, diplo_blocked, diplo_error, diplo_renewed, diplo_expired,
diplo_betrayal, diplo_declined, diplo_slots.
"""
from __future__ import annotations

import functools
import random
import time

from . import bounty as B
from . import diplomacy as D
from . import llm as L
from . import voice as V

SCAN_EVERY = 600.0         # server seconds between two scans (neighbours, impressions, peaceful time, falling back)
READ_EVERY = 120.0         # ... between two reads of the board
READ_KINDS = ["HELLO", "REPLY", "WARNING", "REVENGE", "OFFER", "WANT", "ALLIANCE"]
HELLO_TTL = 3 * 3600       # board seconds a HELLO stays up
REPLY_TTL = 6 * 3600
JOB_WAIT = 90.0            # wall seconds a HELLO / announcement waits for the model before it goes out in plain words
ANSWER_WAIT = 1200.0       # wall seconds after which a model answer is given up (the rule answers instead)
WARNING_GAP = 2 * 3600.0   # a warning from the same agent raises trust at most this often


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def safe(default=None):
    """A diplomacy hook must never cost the agent its round: log one `diplo_error` per kind of problem and return `default`."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Exception as e:
                if self._logi_note(("diplo_error", fn.__name__, type(e).__name__)):
                    self.log("diplo_error", where=fn.__name__, type=type(e).__name__, msg=str(e)[:160])
                return default() if callable(default) else default
        return inner
    return wrap


class DiplomacyMixin:
    diplo_clock = staticmethod(time.time)       # wall clock for the model's waiting times (tests replace it)

    # ---- small helpers -------------------------------------------------------------------------------
    def _diplo_agents(self) -> dict[int, str]:
        """user id -> name of every agent we know of (never humans)."""
        fn = getattr(self.store, "agent_names", None)
        return fn() if fn else {}

    def _diplo_rt(self) -> dict:
        rt = self.__dict__.get("_diplo_runtime")
        if rt is None:
            rt = self.__dict__["_diplo_runtime"] = {"jobs": [], "seen": set(), "rng": random.Random(f"{self.name}:diplomacy")}
        return rt

    def _diplo_players(self) -> tuple[list[dict], int | None]:
        """The ranking: fetched at most every 5 minutes by _get_players; if that fails, whatever is cached."""
        try:
            rows, score = self._get_players(self.now)
            return rows, score
        except Exception:
            cached = self._players
            return (cached[1], cached[2]) if cached else ([], None)

    def _diplo_cached(self) -> tuple[list[dict], int | None]:
        return (self._players[1], self._players[2]) if self._players else ([], None)

    def _diplo_row(self, uid: int, rows: list[dict] | None = None) -> dict | None:
        rows = self._diplo_cached()[0] if rows is None else rows
        return next((r for r in rows if _int(r.get("user_id")) == uid), None)

    def _diplo_rel(self, uid) -> dict | None:
        return self.memory.diplomacy["rel"].get(str(uid))

    def _diplo_name(self, uid: int, rel: dict | None = None) -> str:
        return (rel or {}).get("name") or self._diplo_agents().get(uid) or str(uid)

    def _diplo_on(self) -> bool:
        return bool(getattr(self.llm, "enabled", False))

    def _diplo_voiced(self) -> bool:
        """The model may put hello / announce texts in the agent's own words (not for agents beyond --llm-voices; answers still use it)."""
        return self._diplo_on() and bool(getattr(self, "voiced", True))

    def _diplo_standing(self, uid: int) -> dict:
        rows, score = self._diplo_cached()
        return D.standing(score, self._diplo_row(uid, rows), rows)

    # ---- the hooks other parts of the agent call (all fail-soft) -----------------------------------------
    @safe()
    def _diplo_contact(self, uid, how: str) -> None:
        """We met this player. Only agents count (a human is never part of diplomacy)."""
        uid = _int(uid)
        if uid is None or uid == self.user_id:
            return
        agents = self._diplo_agents()
        if uid not in agents:
            return
        rel = D.add_contact(self.memory.diplomacy, uid, self.now, how, agents[uid])
        if rel is None:
            return
        self._diplo_impress(uid, rel)
        self.log("contact", **{"with": agents[uid]}, uid=uid, how=how, opinion=round(rel["opinion"]), role=rel["role"] or "unknown")

    def _diplo_impress(self, uid: int, rel: dict) -> None:
        """First impression from the cached highscore (a missing ranking just means: later)."""
        rows, score = self._diplo_cached()
        row = self._diplo_row(uid, rows)
        mp = (row or {}).get("main_planet") or {}
        if mp:
            rel["coord"] = ":".join(str(mp.get(k)) for k in ("galaxy", "system", "position"))
        D.apply_impression(rel, D.first_impression(self.persona, score, row, rows))

    @safe()
    def _diplo_battle(self, b: dict) -> None:
        """Called for every battle report we read: a raid on us is the heavy blow, a raid by us costs trust too."""
        uid = _int(b.get("their_id"))
        if uid is None or uid not in self._diplo_agents():
            return
        self._diplo_contact(uid, "battle")
        rel = self._diplo_rel(uid)
        if rel is None:
            return
        if b.get("role") == "defender":
            if b.get("probes_only"):
                rel["spied_us"] += 1
                return
            robbed = int(b.get("loot") or 0) >= 1000 or int(b.get("my_loss") or 0) >= 1000
            res = D.on_hit(rel, self.now, robbed)
            why = "BETRAYAL: they attacked us under a pact" if res["betrayal"] else "they attacked us"
        else:
            res = D.on_we_hit(rel, self.now)
            why = "we attacked them"
        self._diplo_after(uid, rel, res["change"], why, res["from"], res["to"], betrayal=res.get("betrayal", False))
        op = self.memory.diplomacy["open"]
        if op and op["to"] == uid:
            self.memory.diplomacy["open"] = None        # no proposal to someone who just fought us

    @safe()
    def _diplo_we_attacked(self, owner_id, betrayal: bool = False, **facts) -> None:
        uid = _int(owner_id)
        rel = self._diplo_rel(uid) if uid is not None else None
        if rel is None:
            return
        if betrayal:       # a deliberate raid on a pact partner: clearly logged, and at most one per BETRAY_EVERY
            self.memory.diplomacy["betray_at"] = self.now
            res = D.on_we_betray(rel, self.now)
            self.log("diplo_betrayal", **{"with": self._diplo_name(uid, rel)}, uid=uid, was=res["from"], **facts)
            self._diplo_after(uid, rel, res["change"], "we betrayed them", res["from"], res["to"], we_betrayed=True)
            return
        res = D.on_we_hit(rel, self.now)
        self._diplo_after(uid, rel, res["change"], "we attacked them", res["from"], res["to"])

    @safe()
    def _diplo_trade(self, partner: str, outcome: str) -> None:
        """A trade with `partner` (an agent name) finished: 'done' or 'cheated'."""
        uid = next((u for u, n in self._diplo_agents().items() if n == partner), None)
        if uid is None:
            return
        self._diplo_contact(uid, "trade")
        rel = self._diplo_rel(uid)
        if rel is None:
            return
        if outcome == "done":
            change = D.on_trade_done(rel, self.now)
            self._diplo_after(uid, rel, change, "completed trade")
        elif outcome == "cheated":
            change = D.on_cheat(rel, self.now)
            self._diplo_after(uid, rel, change, "cheated in a trade")

    @safe(set)
    def _diplo_peace_ids(self) -> set[int]:
        """Partners that are never spied on or raided: valid pacts and alliances, except the pact partners a ruthless agent may
        betray today (see _diplo_betray_open)."""
        return D.peace_ids(self.memory.diplomacy, self.now) - self._diplo_betray_open()

    @safe(set)
    def _diplo_betray_open(self) -> set[int]:
        """Pact partners (never an alliance step, never someone in our in-game alliance) that a ruthless agent may look at for a
        betrayal today: only on a lucky day (BETRAY_CHANCE, own dice), at most once per BETRAY_EVERY, not while on alert."""
        st = self.memory.diplomacy
        if D._num(getattr(self.persona, "aggression", 0.0), 0.0) < D.RUTHLESS_AT or self.now - st["betray_at"] < D.BETRAY_EVERY \
                or self.now < self.alert_until:
            return set()
        day = int(self.now // 86400)
        rt = self._diplo_rt()
        if rt.get("betray_day", (None, False))[0] != day:
            rt["betray_day"] = (day, D.betray_draw(self.name, day))
        if not rt["betray_day"][1]:
            return set()
        allies = self._ally_ids()
        return {int(k) for k, r in st["rel"].items() if r["step"] == "pact" and D.pact_valid(r, self.now) and int(k) not in allies}

    @safe(None)
    def _diplo_betrayal_target(self, coord, owner_id) -> int | None:
        """The pact partner this target belongs to, if we may consider betraying them today."""
        open_ = self._diplo_betray_open()
        for key in open_:
            rel = self._diplo_rel(key)
            if rel and (_int(owner_id) == key or (coord and rel["coord"] == ":".join(map(str, coord)))):
                return key
        return None

    def _diplo_betray_gate(self, intel) -> str | None:
        """Why this loot does not justify breaking a pact, or None."""
        if not D.betray_loot_ok(self.persona, intel.loot):
            return f"loot {intel.loot} is not worth breaking a pact"
        return None

    @safe(set)
    def _diplo_alliance_ids(self) -> set[int]:
        return D.alliance_ids(self.memory.diplomacy)

    @safe(False)
    def _diplo_protected(self, coord, owner_id) -> bool:
        """True if this target belongs to a pact or alliance partner: never attacked, whatever the target picker or a gamble says."""
        st = self.memory.diplomacy
        for key, rel in st["rel"].items():
            if D.pact_valid(rel, self.now) and (_int(owner_id) == int(key) or (coord and rel["coord"] == ":".join(map(str, coord)))):
                return True
        return False

    # ---- shared bookkeeping ------------------------------------------------------------------------------
    def _diplo_after(self, uid: int, rel: dict, change: float, why: str, frm: str | None = None, to: str | None = None, **extra) -> None:
        """Log a trust change (small ones rarely) and any step change, then let trust settle the ladder."""
        name = self._diplo_name(uid, rel)
        if D.loggable(rel, change, self.now):
            self.log("diplo_trust", **{"with": name}, uid=uid, delta=round(change, 1), why=why, trust=round(rel["trust"]), step=rel["step"])
        if frm and to and frm != to:
            self._diplo_stepped(uid, rel, frm, to, why, **extra)
        self._diplo_settle(uid, rel)

    def _diplo_settle(self, uid: int, rel: dict) -> None:
        events: list[str] = []
        renew_ok = True
        if rel["step"] == "pact" and D.is_raider(self.persona):      # a raider only keeps a pact with someone it could not profitably raid
            renew_ok = not D.could_raid(self.persona, self._diplo_pact_info(uid, rel))
        moves = D.settle(rel, self.now, renew_ok, events)
        name = self._diplo_name(uid, rel)
        for ev in events:
            self.log("diplo_renewed" if ev == "renewed" else "diplo_expired", **{"with": name}, uid=uid, trust=round(rel["trust"]),
                     until=round(rel["pact_until"]) if rel["pact_until"] else None, renewals=rel["renewals"])
        for frm, to in moves:
            self._diplo_stepped(uid, rel, frm, to, "pact expired" if "expired" in events else "trust")

    def _diplo_pact_info(self, uid: int, rel: dict) -> dict:
        """What this raider knows about a possible partner: score ratio (live ranking, else the first impression) and the last spy
        report (strength and loot of the planet, from Memory.opponents)."""
        rows, score = self._diplo_cached()
        ratio = D.standing(score, self._diplo_row(uid, rows), rows)["ratio"]
        info = {"ratio": ratio if ratio is not None else rel.get("ratio")}
        coord = coord_of(rel["coord"])
        rec = self.memory.opponents.get(coord) if coord else None
        if rec and rec.get("strength") is not None and rec.get("loot") is not None:
            info.update(strength=rec["strength"], loot=rec["loot"], age=self.now - rec["spied_at"] if rec.get("spied_at") else None)
        return info

    def _diplo_stepped(self, uid: int, rel: dict, frm: str, to: str, why: str, **extra) -> None:
        self.log("diplo_step", **{"with": self._diplo_name(uid, rel)}, uid=uid, trust=round(rel["trust"]), why=why, **{"from": frm, "to": to}, **extra)
        if to in D.PEACE_STEPS:
            self._diplo_drop_intel(uid, rel)

    def _diplo_drop_intel(self, uid: int, rel: dict) -> None:
        """A partner is never a target: forget spy reports on their planets (like allies, see _alli_drop_ally_intel)."""
        coord = coord_of(rel["coord"])
        for c, info in list(self.memory.intel.items()):
            if _int(info.get("owner_id")) == uid or c == coord:
                del self.memory.intel[c]

    def _diplo_flags(self, uid: int, rel: dict) -> tuple[bool, bool]:
        """(aggressor_recent, revenge_open) for the hard rules, from the memories other parts of the agent keep."""
        coord = coord_of(rel["coord"])
        recent = coord is not None and any(c == coord and self.now - stamp < D.HIT_GRUDGE for c, stamp in self.memory.aggressors.items())
        rec = self.memory.opponents.get(coord) if coord else None
        if rec and float(rec.get("robbed_at") or 0) > rel["robbed_at"]:
            rel["robbed_at"] = float(rec["robbed_at"])
        st = self.memory.social
        revenge = B.revenge_active(st, self.now)
        bounty = B.open_bounty(st, self._wall())
        open_ = any(x and (_int(x.get("pid")) == uid or (coord is not None and coord_of(x.get("coord") or x.get("target_at")) == coord))
                    for x in (revenge, bounty))
        return recent, open_

    def _diplo_hard(self, uid: int, rel: dict, proposed: str, check_cool: bool = True) -> str | None:
        recent, revenge = self._diplo_flags(uid, rel)
        info = self._diplo_pact_info(uid, rel) if proposed == "pact" and D.is_raider(self.persona) else None
        return D.hard_check(rel, proposed, self.now, self.persona, aggressor_recent=recent, revenge_open=revenge, check_cool=check_cool,
                            slots_used=D.pact_used(self.memory.diplomacy, self.now), pact_info=info)

    def _diplo_threat_count(self) -> int:
        n = sum(1 for stamp in self.memory.aggressors.values() if self.now - stamp < D.HIT_GRUDGE)
        return n + (1 if self.now < self.alert_until else 0)

    def _diplo_facts(self, uid: int, rel: dict, proposed: str, proposer: str, answerer: str) -> dict:
        rows, score = self._diplo_cached()
        row = self._diplo_row(uid, rows)
        return D.build_facts(rel, proposed, self.now, standing_=D.standing(score, row, rows), my_score=score,
                             their_score=(row or {}).get("score"), threats=self._diplo_threat_count(), proposer=proposer, answerer=answerer)

    # ---- the round -----------------------------------------------------------------------------------------
    def _plan_diplomacy(self, status: dict, planets: list[dict], threats: list) -> list[float]:
        """Called every round. Cheap when there is nothing to do. Never raises."""
        try:
            st = self.memory.diplomacy
            if not st["born"]:
                st["born"] = self.now
            self._diplo_coords = [(int(p["coordinates"]["galaxy"]), int(p["coordinates"]["system"]), int(p["coordinates"]["position"]))
                                  for p in planets if p.get("coordinates")]
            self._diplo_collect()
            self._diplo_seen_threats(threats)
            if self.now >= st["scan_at"]:
                st["scan_at"] = self.now + SCAN_EVERY
                self._diplo_scan()
            if self._board_on():
                if self.now >= st["read_at"]:
                    self._diplo_read()
                self._diplo_retry()
                self._diplo_propose()
        except Exception as e:
            if self._logi_note(("diplo_error", "plan", type(e).__name__)):
                self.log("diplo_error", where="plan", type=type(e).__name__, msg=str(e)[:160])
        return []

    def _diplo_seen_threats(self, threats: list) -> None:
        """A probe or fleet seen flying to us comes from a known planet: that is contact, and probes are counted."""
        if not threats:
            return
        rows, _ = self._diplo_cached()
        main = {}
        for r in rows:
            mp = r.get("main_planet") or {}
            if _int(r.get("user_id")) is not None and mp:
                main[(_int(mp.get("galaxy")), _int(mp.get("system")), _int(mp.get("position")))] = int(r["user_id"])
        seen = self._diplo_rt()["seen"]
        for t in threats:
            uid = main.get(tuple(t.origin))
            if uid is None or t.id in seen:
                continue
            if len(seen) > 500:
                seen.clear()
            seen.add(t.id)
            self._diplo_contact(uid, "attack seen" if t.is_attack else "spy probe seen")
            rel = self._diplo_rel(uid)
            if rel is not None and not t.is_attack:
                rel["spied_us"] += 1

    def _diplo_scan(self) -> None:
        st = self.memory.diplomacy
        rows, _ = self._diplo_players()
        agents = self._diplo_agents()
        if not agents:
            return
        for row in D.neighbours(getattr(self, "_diplo_coords", []), rows, self.user_id, set(agents)):
            self._diplo_contact(row["user_id"], "neighbour")
        by_uid = {_int(r.get("user_id")): r for r in rows}
        for key, rel in list(st["rel"].items()):
            uid = int(key)
            row = by_uid.get(uid)
            if row is not None:
                if not rel["imp"]:
                    self._diplo_impress(uid, rel)
                mp = row.get("main_planet") or {}
                if mp:
                    rel["coord"] = ":".join(str(mp.get(k)) for k in ("galaxy", "system", "position"))
            change = D.peace_tick(rel, self.now)
            self._diplo_after(uid, rel, change, "peaceful time")
        self._diplo_log_slots()
        op = st["open"]
        if op and self.now - op["at"] > D.OPEN_TIMEOUT:      # nobody answered: that is a refusal
            rel = self._diplo_rel(op["to"])
            if rel is not None:
                rel["cool_until"] = self.now + D.REFUSAL_COOL
            st["open"] = None
            self.log("diplo_answer", side="proposer", decision="TIMEOUT", reason="no answer", step=op["step"], **{"with": op["to_name"]})

    def _diplo_log_slots(self) -> None:
        """One `diplo_slots` line when the pact picture changes (and at least every 6 hours): slots used, available, expiry times."""
        st = self.memory.diplomacy
        pacts = [(k, r) for k, r in st["rel"].items() if D.pact_valid(r, self.now)]
        partners = [{"name": self._diplo_name(int(k), r), "step": r["step"], "until": round(r["pact_until"]) if r["step"] == "pact" and r["pact_until"] else None}
                    for k, r in pacts][:D.SLOTS_MAX]
        sig = (len(pacts), D.pact_slots(self.persona), tuple((p["name"], p["step"]) for p in partners))
        rt = self._diplo_rt()
        if rt.get("slots_sig") == sig and self.now - rt.get("slots_at", 0.0) < D.LOG_EVERY:
            return
        rt["slots_sig"], rt["slots_at"] = sig, self.now
        untils = [p["until"] for p in partners if p["until"]]
        self.log("diplo_slots", used=len(pacts), slots=sig[1], next_expiry=min(untils) if untils else None, partners=partners)

    # ---- reading the board -----------------------------------------------------------------------------
    def _diplo_read(self) -> None:
        st = self.memory.diplomacy
        r = self.board.read(since=st["cursor"], kind=READ_KINDS, limit=100)
        if not r["ok"]:
            st["read_at"] = self.now + 300
            return
        st["read_at"] = self.now + READ_EVERY
        posts = r["posts"]
        if posts:
            st["cursor"] = max(st["cursor"], max(p["id"] for p in posts))
        by_name = {n: u for u, n in self._diplo_agents().items()}
        for p in posts:
            uid = by_name.get(p.get("agent"))
            if uid is None or uid == self.user_id:
                continue
            self._diplo_contact(uid, "board post")
            rel = self._diplo_rel(uid)
            data = p.get("data") if isinstance(p.get("data"), dict) else {}
            kind = p.get("kind")
            if rel is None:
                continue
            if kind == "HELLO" and _int(data.get("to_id")) == self.user_id:
                self._diplo_incoming(p, uid, rel, r["now"])
            elif kind == "REPLY":
                self._diplo_reply(p, uid, rel, data)
            elif kind == "WARNING" and self.now - rel["warn_at"] >= WARNING_GAP:
                rel["warn_at"] = rel["warned_us_at"] = self.now      # they share what they know about raiders: a small step towards trust
                self._diplo_after(uid, rel, D.bump(rel, D.GAIN["warning"], self.now, soft=True), "shared a warning")

    # ---- being asked -------------------------------------------------------------------------------------
    def _diplo_incoming(self, post: dict, uid: int, rel: dict, board_now: float) -> None:
        st = self.memory.diplomacy
        pid = post["id"]
        rt = self._diplo_rt()
        if pid in st["handled"] or str(pid) in st["later"] or any(j.get("pid") == pid for j in rt["jobs"]):
            return
        data = post.get("data") or {}
        proposed = data.get("step")
        stale = bool(board_now) and float(post.get("t") or 0) < board_now - D.HELLO_FRESH
        if stale or proposed not in ("neutral", "pact", "alliance") or proposed != D.next_step(rel["step"]):
            self._diplo_handled(pid)       # old, odd, or it would skip a step: ignored (the proposer times out)
            return
        self._diplo_consider(pid, uid, proposed, 0)

    def _diplo_handled(self, pid) -> None:
        st = self.memory.diplomacy
        st["handled"] = (st["handled"] + [pid])[-100:]

    def _diplo_consider(self, pid, uid: int, proposed: str, tries: int) -> None:
        """Check the hard rules, then ask the model (or the rule). The answer is applied by _diplo_collect on a later round."""
        rel = self._diplo_rel(uid)
        if rel is None:
            return
        why = self._diplo_hard(uid, rel, proposed)
        if why:
            self._diplo_finish(pid, uid, proposed, "REFUSE", why, "hard rule", tries)
            return
        if self._diplo_on():
            proposer = self._diplo_name(uid, rel)
            facts = self._diplo_facts(uid, rel, proposed, proposer, self.name)
            job = {"kind": "answer", "pid": pid, "uid": uid, "step": proposed, "tries": tries, "started": self.diplo_clock(), "done": False,
                   "answer": None}
            if self._diplo_ask(job, "answer", D.answer_system(self._voice_state()["sheet"]), D.answer_prompt(facts), L.HIGH):
                return
        decision, reason = D.decide_rule(rel, proposed)
        self._diplo_finish(pid, uid, proposed, decision, reason, "rule", tries)

    def _diplo_ask(self, job: dict, purpose: str, system: str, prompt: str, priority: int) -> bool:
        """Queue a model request; its callback only stores the text in `job`. False if it could not be queued."""
        def done(answer):
            job["answer"] = answer
            job["done"] = True

        try:
            ok = self.llm.submit(self.name, purpose, system, prompt, done, priority)
        except Exception:
            ok = False
        if ok:
            self._diplo_rt()["jobs"].append(job)
        return bool(ok)

    def _diplo_collect(self) -> None:
        """Pick up what the model has answered since the last round."""
        rt = self._diplo_rt()
        if not rt["jobs"]:
            return
        now = self.diplo_clock()
        keep = []
        for job in rt["jobs"]:
            late = now - job["started"] > (ANSWER_WAIT if job["kind"] == "answer" else JOB_WAIT)
            if not (job["done"] or late):
                keep.append(job)
                continue
            try:
                if job["kind"] == "answer":
                    self._diplo_apply_answer(job)
                elif job["kind"] == "hello":
                    self._diplo_send_hello(job)
                else:
                    self._diplo_send_announce(job)
            except Exception as e:
                if self._logi_note(("diplo_error", job["kind"], type(e).__name__)):
                    self.log("diplo_error", where=job["kind"], type=type(e).__name__, msg=str(e)[:160])
        rt["jobs"] = keep

    def _diplo_apply_answer(self, job: dict) -> None:
        uid, proposed = job["uid"], job["step"]
        rel = self._diplo_rel(uid)
        if rel is None:
            return
        if job["answer"] is None:        # the model failed (down, budget, timeout): the deterministic rule answers instead
            decision, reason = D.decide_rule(rel, proposed)
            self._diplo_finish(job["pid"], uid, proposed, decision, reason, "rule (no model answer)", job["tries"])
            return
        decision, reason = D.parse_decision(job["answer"])
        self._diplo_finish(job["pid"], uid, proposed, decision, reason, "llm", job["tries"], raw=job["answer"])

    def _diplo_finish(self, pid, uid: int, proposed: str, decision: str, reason: str, source: str, tries: int, raw: str | None = None) -> None:
        """Apply a decision. The hard rules are checked once more here: an ACCEPT that breaks one becomes a REFUSE."""
        st = self.memory.diplomacy
        rel = self._diplo_rel(uid)
        if rel is None:
            return
        name = self._diplo_name(uid, rel)
        if decision == "ACCEPT":
            why = self._diplo_hard(uid, rel, proposed)
            if why:
                decision, reason, source = "REFUSE", why, "hard rule"
        digest = D.facts_digest(self._diplo_facts(uid, rel, proposed, name, self.name))
        fields = dict(side="answerer", decision=decision, reason=reason[:120], source=source, step=proposed, facts=digest, **{"with": name})
        if raw is not None:
            fields["raw"] = str(raw)[:1000]
        if decision == "WAIT":
            if tries + 1 > D.LATER_TRIES:        # waited long enough: that is a refusal (no reply, the proposer times out too)
                st["later"].pop(str(pid), None)
                self._diplo_handled(pid)
                rel["cool_until"] = self.now + D.REFUSAL_COOL
                self.log("diplo_answer", **{**fields, "decision": "WAIT", "reason": "still undecided: treated as a refusal"})
                return
            st["later"][str(pid)] = {"uid": uid, "step": proposed, "tries": tries + 1, "retry": self.now + D.LATER_RETRY, "name": name}
            self.log("diplo_answer", **fields)
            return
        reply = self._diplo_post_reply(pid, uid, proposed, decision, reason)
        if decision == "ACCEPT" and not reply:      # we must tell them before we change anything
            st["later"][str(pid)] = {"uid": uid, "step": proposed, "tries": tries, "retry": self.now + 300, "name": name,
                                     "decided": "ACCEPT", "reason": reason, "source": source}
            return
        st["later"].pop(str(pid), None)
        self._diplo_handled(pid)
        self.log("diplo_answer", **fields)
        if decision == "ACCEPT":
            self._diplo_advance(uid, rel, proposed, "accepted their proposal", announce=True)
        else:
            if self.now >= rel["cool_until"]:
                rel["cool_until"] = self.now + D.REFUSAL_COOL
            rel["opinion"] = max(-100.0, rel["opinion"] - 2.0)

    def _diplo_retry(self) -> None:
        """WAIT answers come back after a while; a decision whose reply could not be posted is posted again."""
        st = self.memory.diplomacy
        for key, e in list(st["later"].items()):
            if self.now < float(e.get("retry") or 0):
                continue
            pid = _int(key)
            uid = _int(e.get("uid"))
            if pid is None or uid is None or self._diplo_rel(uid) is None:
                del st["later"][key]
                continue
            if e.get("decided"):
                self._diplo_finish(pid, uid, e["step"], e["decided"], str(e.get("reason") or ""), str(e.get("source") or "retry"), int(e.get("tries") or 0))
            else:
                del st["later"][key]
                self._diplo_consider(pid, uid, e["step"], int(e.get("tries") or 0))

    def _diplo_post_reply(self, pid, uid: int, proposed: str, decision: str, reason: str) -> bool:
        text = V.clean(reason) or ("Agreed." if decision == "ACCEPT" else "No, not now.")
        data = {"reply_to": pid, "decision": decision, "from": self.user_id, "to_id": uid, "step": proposed}
        try:
            return self._board_post("REPLY", text[:200], data, ttl=REPLY_TTL) is not None
        except Exception:
            return False

    def _diplo_advance(self, uid: int, rel: dict, step: str, why: str, announce: bool = False) -> None:
        frm = rel["step"]
        rel["step"] = step
        if step == "pact":
            D.start_pact(rel, self.now)
        change = D.bump(rel, D.GAIN["accepted"], self.now)
        rel["opinion"] = min(100.0, rel["opinion"] + 5.0)
        self._diplo_stepped(uid, rel, frm, step, why)
        if D.loggable(rel, change, self.now):
            self.log("diplo_trust", **{"with": self._diplo_name(uid, rel)}, uid=uid, delta=round(change, 1), why=why, trust=round(rel["trust"]), step=step)
        if announce and step in D.PEACE_STEPS:
            self._diplo_announce(uid, rel, step)

    # ---- being the one who asks ----------------------------------------------------------------------------
    def _diplo_reply(self, post: dict, uid: int, rel: dict, data: dict) -> None:
        st = self.memory.diplomacy
        op = st["open"]
        if not op or uid != op["to"] or _int(data.get("reply_to")) != _int(op["post"]):
            return
        decision = str(data.get("decision") or "").upper()
        st["open"] = None
        name = self._diplo_name(uid, rel)
        fields = dict(side="proposer", decision=decision, reason=str(post.get("text") or "")[:120], step=op["step"], **{"with": name})
        if decision == "ACCEPT":
            why = self._diplo_hard(uid, rel, op["step"], check_cool=False)
            if why:
                self.log("diplo_answer", **{**fields, "decision": "ACCEPT (ignored)", "reason": why})
                return
            self.log("diplo_answer", **fields)
            self._diplo_advance(uid, rel, op["step"], "they accepted our proposal")
        else:
            rel["cool_until"] = self.now + D.REFUSAL_COOL
            rel["opinion"] = max(-100.0, rel["opinion"] - 2.0)
            self.log("diplo_answer", **fields)

    def _diplo_pick(self) -> tuple[int, dict, str] | None:
        """Whom to propose a step to: the best relation that is allowed one (hard rules, cool-off, opinion, persona)."""
        st = self.memory.diplomacy
        agents = self._diplo_agents()
        best = None
        for key, rel in st["rel"].items():
            uid = int(key)
            if uid not in agents:
                continue
            shared = self._diplo_threat_count() > 0 or self.now - rel["warned_us_at"] < 24 * 3600
            step = D.offer_step(rel, shared)
            if step is None or rel["opinion"] < -10:
                continue
            why = self._diplo_hard(uid, rel, step)
            if why:
                if step == "pact" and why in ("pact slots full", "looks like a target") and self._logi_note(("diplo_declined", uid, why, int(self.now // 86400))):
                    self.log("diplo_declined", **{"with": self._diplo_name(uid, rel)}, uid=uid, step=step, reason=why)
                continue
            score = rel["trust"] + rel["opinion"] / 2.0
            if best is None or score > best[0]:
                best = (score, uid, rel, step)
        return best[1:] if best else None

    def _diplo_propose(self) -> None:
        st = self.memory.diplomacy
        rt = self._diplo_rt()
        if st["open"] or any(j["kind"] == "hello" for j in rt["jobs"]) or self.now < st["next_hello_at"] or self.now < self.alert_until:
            return
        pick = self._diplo_pick()
        if pick is None:
            st["next_hello_at"] = self.now + 600
            return
        uid, rel, step = pick
        st["next_hello_at"] = self.now + D.HELLO_EVERY * rt["rng"].uniform(0.9, 1.2)    # set first: never hammer the board
        name = self._diplo_name(uid, rel)
        template = D.hello_template(self.name, name, step)
        job = {"kind": "hello", "uid": uid, "step": step, "template": template, "started": self.diplo_clock(), "done": False, "answer": None}
        if self._diplo_voiced() and self._diplo_ask(job, "hello", V.system_prompt(self._voice_state()["sheet"]), D.hello_prompt(template, step), L.NORMAL):
            return          # posted by _diplo_collect when the text is ready (or after JOB_WAIT in plain words)
        job["done"] = True
        self._diplo_send_hello(job)

    def _diplo_send_hello(self, job: dict) -> None:
        st = self.memory.diplomacy
        uid, step = job["uid"], job["step"]
        rel = self._diplo_rel(uid)
        if rel is None or st["open"]:
            return
        why = self._diplo_hard(uid, rel, step)     # things may have changed while the model was thinking
        if why or step != D.next_step(rel["step"]):
            return
        text = V.check(job["answer"], job["template"]) if job["answer"] else None
        name = self._diplo_name(uid, rel)
        data = {"to_id": uid, "to_name": name, "step": step, "from_id": self.user_id,
                "at": ":".join(map(str, self._diplo_coords[0])) if getattr(self, "_diplo_coords", None) else ""}
        post = self._board_post("HELLO", text or job["template"], data, ttl=HELLO_TTL)
        if post is None:
            st["next_hello_at"] = self.now + 900
            return
        st["open"] = {"to": uid, "to_name": name, "step": step, "post": post.get("id"), "at": self.now}
        self.log("diplo_hello", **{"with": name}, uid=uid, step=step, trust=round(rel["trust"]), text=(text or job["template"])[:160],
                 post=post.get("id"), via="llm" if text else "template")

    def _diplo_announce(self, uid: int, rel: dict, step: str) -> None:
        """A pact or alliance was agreed: a one-line public announcement (text only; the model may put it in the agent's own words)."""
        name = self._diplo_name(uid, rel)
        template = D.announce_template(self.name, name, step)
        job = {"kind": "announce", "uid": uid, "step": step, "template": template, "started": self.diplo_clock(), "done": False, "answer": None}
        if self._diplo_voiced() and self._diplo_ask(job, "announce", V.system_prompt(self._voice_state()["sheet"]), D.announce_prompt(template), L.NORMAL):
            return
        job["done"] = True
        self._diplo_send_announce(job)

    def _diplo_send_announce(self, job: dict) -> None:
        rel = self._diplo_rel(job["uid"])
        if rel is None or not self._board_on():
            return
        text = V.check(job["answer"], job["template"]) if job["answer"] else None
        name = self._diplo_name(job["uid"], rel)
        post = self._board_post("ALLIANCE", text or job["template"], {"announce": job["step"], "with": name, "with_id": job["uid"]})
        if post is not None:
            self.log("diplo_announce", **{"with": name}, step=job["step"], text=(text or job["template"])[:160], via="llm" if text else "template")


def coord_of(text):
    """'g:s:p' -> (g, s, p), else None."""
    try:
        g, s, p = (int(x) for x in str(text).split(":"))
    except (TypeError, ValueError):
        return None
    return (g, s, p)
