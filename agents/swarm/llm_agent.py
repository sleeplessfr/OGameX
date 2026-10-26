"""The voice of an agent (mixed into Agent in agent.py). Everything here is skipped unless the process was given a language model
(`agent.llm`, set by run_swarm.py when `--llm URL` is used). It changes no decision: only the TEXT of board posts and a diary.

Hooks (two one-line calls elsewhere):
  * `Agent.log()` calls `_voice_notice(event, fields)`: a notable event may become a diary entry.
  * `SocialMixin._board_post()` calls `_voice_text(kind, text, data)`: the rewritten text if one is ready, else the original.
The model is asked in a background worker (llm.py), never in the agent's round; at most one voice request per agent per
5 minutes. Diary lines wait (a few at most) for their turn.

Log events: voice_post (a board text was rewritten: kind, original, text), diary (text, about).
"""
from __future__ import annotations

import threading
import time

from . import llm as L
from . import voice as V

VOICE_EVERY = 300.0       # seconds between two voice requests of one agent
GRACE = 20.0              # seconds a board post may wait for its rewrite (a local model needs 6-12 s; a rewrite that arrives later is never used)
READY_KEEP = 20
READY_TTL = 3600.0
PENDING_DIARY = 3
BIG_RAID = 20000          # loot that makes a won raid worth a diary line


def _n(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def notable(event: str, f: dict) -> str | None:
    """A plain sentence for an event that deserves a diary line, else None."""
    if event == "battle":
        if f.get("role") == "defender" and _n(f.get("loot")) > 0:
            return f"I was robbed of {_n(f.get('loot')):,} resources and lost {_n(f.get('my_loss')):,} in value."
        if f.get("role") == "attacker" and _n(f.get("loot")) >= BIG_RAID and f.get("verdict") != "bad":
            return f"My raid at {f.get('at')} was a success: I looted {_n(f.get('loot')):,}."
    elif event == "colony_founded":
        return f"I founded a new colony; I now own {_n(f.get('planets'))} planets."
    elif event == "expedition_result":
        return f"My expedition came back with this outcome: {f.get('outcome')}."
    elif event == "fleet_saved":
        return "I got my fleet out of the way of a raid I could not win."
    elif event == "trade_offer" and f.get("action") == "done":
        return f"A trade with {f.get('with')} was completed."
    elif event == "trade":
        return "I traded with the merchant."
    return None


class VoiceMixin:
    llm = None              # an LLMClient (swarm/llm.py); None = the feature is off
    voice_grace = GRACE
    voice_every = VOICE_EVERY
    voice_clock = staticmethod(time.time)
    voiced = True           # False (run_swarm --llm-voices N, for the agents beyond the first N): template texts, no diary, no rewrite

    def _voice_on(self) -> bool:
        return bool(getattr(self.llm, "enabled", False)) and bool(self.voiced)

    def _voice_state(self) -> dict:
        v = self.memory.voice
        if not isinstance(v.get("sheet"), dict):
            v["sheet"] = V.make_sheet(self.name, self.persona)
        if not isinstance(v.get("diary"), list):
            v["diary"] = []
        return v

    def _voice_runtime(self) -> dict:
        rt = self.__dict__.get("_voice_rt")
        if rt is None:
            rt = self.__dict__["_voice_rt"] = {"lock": threading.Lock(), "last": -1e18, "ready": {}, "inflight": {}, "diary_wait": []}
        return rt

    def _voice_slot(self) -> bool:
        """True (and the slot is taken) if this agent may send a voice request now."""
        rt = self._voice_runtime()
        now = self.voice_clock()
        with rt["lock"]:
            if now - rt["last"] < self.voice_every:
                return False
            rt["last"] = now
            return True

    # ---- board posts ---------------------------------------------------------------------------------------------
    def _voice_text(self, kind: str, text: str, data: dict) -> str:
        """The text for a board post: a ready rewrite, else the original (and a rewrite is asked for in the background)."""
        try:
            if not self._voice_on() or kind not in V.VOICE_KINDS:
                return text
            rt = self._voice_runtime()
            key = (kind, text)
            now = self.voice_clock()
            with rt["lock"]:
                rt["ready"] = {k: v for k, v in rt["ready"].items() if now - v[1] < READY_TTL}
                hit = rt["ready"].pop(key, None)
                waiting = rt["inflight"].get(key)
            if hit is None and waiting is None:
                if not self._voice_slot():
                    return text
                waiting = threading.Event()
                with rt["lock"]:
                    rt["inflight"][key] = waiting
                self._voice_ask_rewrite(kind, text, data, key, waiting)
            if hit is None and waiting is not None and self.voice_grace > 0:
                waiting.wait(self.voice_grace)
                with rt["lock"]:
                    hit = rt["ready"].pop(key, None)
            if hit is None:
                return text
            self.log("voice_post", kind=kind, original=text[:160], text=hit[0])
            return hit[0]
        except Exception:
            return text

    def _voice_ask_rewrite(self, kind, text, data, key, event) -> None:
        sheet = self._voice_state()["sheet"]
        rt = self._voice_runtime()

        def done(answer):
            ok = V.check(answer, text, data, require_coords=True)
            with rt["lock"]:
                rt["inflight"].pop(key, None)
                if ok and ok != text:
                    rt["ready"][key] = (ok, self.voice_clock())
                    while len(rt["ready"]) > READY_KEEP:
                        rt["ready"].pop(next(iter(rt["ready"])))
            event.set()

        prio = L.HIGH if kind in ("WARNING", "REVENGE", "BOUNTY", "CLAIM") else L.NORMAL
        if not self.llm.submit(self.name, f"rewrite:{kind}", V.system_prompt(sheet), V.rewrite_prompt(kind, text), done, prio):
            done(None)

    # ---- the diary -----------------------------------------------------------------------------------------------
    def _voice_notice(self, event: str, fields: dict) -> None:
        """Called for every logged event. Never raises."""
        try:
            if not self._voice_on():
                return
            sentence = notable(event, fields)
            rt = self._voice_runtime()
            if sentence:
                with rt["lock"]:
                    rt["diary_wait"] = (rt["diary_wait"] + [sentence])[-PENDING_DIARY:]
            if rt["diary_wait"] and self._voice_slot():
                with rt["lock"]:
                    first = rt["diary_wait"].pop(0)
                self._voice_ask_diary(first)
        except Exception:
            pass

    def _voice_ask_diary(self, sentence: str) -> None:
        sheet = self._voice_state()["sheet"]

        def done(answer):
            ok = V.check(answer, sentence)
            if not ok:
                return
            entry = {"t": int(time.time()), "text": ok, "about": sentence[:120]}
            diary = self._voice_state()["diary"]
            diary.append(entry)
            del diary[:-V.DIARY_KEEP]
            self.log("diary", text=ok, about=sentence[:120])

        prio = L.HIGH if sentence.startswith(("I was robbed", "My raid")) else L.NORMAL
        if not self.llm.submit(self.name, "diary", V.system_prompt(sheet), V.diary_prompt(sentence), done, prio):
            done(None)
