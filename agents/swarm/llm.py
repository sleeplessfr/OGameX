"""A client for an OpenAI-compatible chat endpoint (LM Studio's local server, for instance). Standard library only.

The language model is SLOW and SHARED: every agent lives in the same process and the same GPU. So:

* One background worker thread sends the requests, one at a time (a queue with priorities: battle, revenge and bounty
  talk go before routine chatter). Agents never wait for it: they `submit()` a request with a callback and carry on;
  the answer is picked up on a later round.
* A request that waited too long in the queue is dropped (its callback gets None).
* A global budget of requests per hour (`max_per_hour`).
* EVERY failure (server down, timeout, bad JSON, empty answer, budget used up) gives None. Nothing here raises.
* Every call is logged, one JSON line, in `<log_dir>/llm.jsonl`. These lines carry "kind": "llm_call" and no "event" key on
  purpose, so the observatory and summarize_logs do not mistake them for agent events (they read the file themselves).
* url "fake" answers from templates without any server: for the tests and for trying the plumbing without LM Studio.

The LLM never touches the game: what it returns is text, and the callers validate it (voice.py).
"""
from __future__ import annotations

import heapq
import itertools
import json
import threading
import time
import urllib.request
from pathlib import Path

HIGH, NORMAL = 0, 5            # priorities: lower goes first
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_PER_HOUR = 120
DEFAULT_MAX_WAIT = 600.0       # a queued request older than this is dropped
QUEUE_LIMIT = 200
FAKE = "fake"


class LLMClient:
    enabled = True

    def __init__(self, url: str, model: str = "", timeout: float = DEFAULT_TIMEOUT, max_per_hour: int = DEFAULT_MAX_PER_HOUR,
                 log_dir: str | Path | None = "logs", max_wait: float = DEFAULT_MAX_WAIT, clock=time.time):
        self.url = url.rstrip("/")
        self.fake = self.url == FAKE
        self.model = model or "local-model"
        self.timeout = float(timeout)
        self.max_per_hour = int(max_per_hour)
        self.max_wait = float(max_wait)
        self.clock = clock
        self.log_path = Path(log_dir) / "llm.jsonl" if log_dir else None
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._sent: list[float] = []        # times of the calls that count against the budget
        self._worker: threading.Thread | None = None
        self._stop = False
        self._log_lock = threading.Lock()
        self.counts = {"ok": 0, "failed": 0, "dropped": 0}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the server lives on this computer

    # ---- the public door ---------------------------------------------------------------------------------------
    def submit(self, agent: str, purpose: str, system: str, prompt: str, callback, priority: int = NORMAL, max_wait: float | None = None) -> bool:
        """Queue a request. `callback(answer_or_None)` is called from the worker thread. False if the queue is full. Never raises."""
        try:
            with self._cv:
                if self._stop or len(self._heap) >= QUEUE_LIMIT:
                    return False
                heapq.heappush(self._heap, (priority, next(self._seq), self.clock(), self.max_wait if max_wait is None else max_wait,
                                            agent, purpose, system, prompt, callback))
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="llm-worker", daemon=True)
                    self._worker.start()
                self._cv.notify()
            return True
        except Exception:
            return False

    def ask(self, agent: str, purpose: str, system: str, prompt: str, priority: int = NORMAL, wait: float | None = None) -> str | None:
        """Blocking convenience (tests, tools): submit and wait for the answer. None on any failure or when `wait` runs out."""
        box: list = []
        done = threading.Event()

        def cb(answer):
            box.append(answer)
            done.set()

        if not self.submit(agent, purpose, system, prompt, cb, priority):
            return None
        done.wait(self.timeout + 5 if wait is None else wait)
        return box[0] if box else None

    def queued(self) -> int:
        with self._cv:
            return len(self._heap)

    def close(self) -> None:
        with self._cv:
            self._stop = True
            dropped, self._heap = self._heap, []
            self._cv.notify_all()
        for item in dropped:
            self._finish(item[8], None)

    # ---- the worker -------------------------------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap and not self._stop:
                    self._cv.wait(timeout=30)
                    if not self._heap and not self._stop:
                        self._worker = None       # idle for a while: let the thread go; submit() starts a new one
                        return
                if self._stop:
                    return
                priority, _, queued_at, max_wait, agent, purpose, system, prompt, callback = heapq.heappop(self._heap)
            answer = None
            try:
                answer = self._one(agent, purpose, system, prompt, priority, queued_at, max_wait)
            except Exception as e:      # whatever happened, the worker must survive
                self._log(agent, purpose, prompt, None, 0.0, None, f"internal {type(e).__name__}")
            self._finish(callback, answer)

    @staticmethod
    def _finish(callback, answer) -> None:
        try:
            callback(answer)
        except Exception:
            pass

    def _budget_left(self) -> bool:
        now = self.clock()
        self._sent = [t for t in self._sent if now - t < 3600]
        return len(self._sent) < self.max_per_hour

    def _one(self, agent, purpose, system, prompt, priority, queued_at, max_wait) -> str | None:
        waited = self.clock() - queued_at
        if waited > max_wait:
            self.counts["dropped"] += 1
            self._log(agent, purpose, prompt, None, 0.0, None, f"dropped: waited {waited:.0f} s", priority=priority)
            return None
        if not self._budget_left():
            self.counts["dropped"] += 1
            self._log(agent, purpose, prompt, None, 0.0, None, "budget: no requests left this hour", priority=priority)
            return None
        self._sent.append(self.clock())
        started = time.monotonic()
        tokens, error, answer = None, None, None
        try:
            if self.fake:
                answer = fake_answer(system, prompt)
            else:
                answer, tokens = self._http(system, prompt)
                if not (answer or "").strip():     # some reasoning models now and then return an empty message: one more try
                    answer, tokens = self._http(system, prompt)
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:100]}"
        answer = answer.strip() if isinstance(answer, str) else answer
        if error is None and not answer:
            answer, error = None, "empty answer"
        latency = time.monotonic() - started
        self.counts["failed" if error else "ok"] += 1
        self._log(agent, purpose, prompt, answer, latency, tokens, error, priority=priority)
        return answer or None

    def _http(self, system: str, prompt: str) -> tuple[str, int | None]:
        body = json.dumps({"model": self.model, "temperature": 0.8, "max_tokens": 600, "reasoning_effort": "low", "stream": False,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}).encode("utf-8")
        req = urllib.request.Request(self.url + "/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        with self._opener.open(req, timeout=self.timeout) as r:
            reply = json.loads(r.read().decode("utf-8"))
        text = reply["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("no text in the answer")
        usage = reply.get("usage") if isinstance(reply, dict) else None
        tokens = usage.get("total_tokens") if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int) else None
        return text, tokens

    def _log(self, agent, purpose, prompt, answer, latency, tokens, error=None, priority=None) -> None:
        if not self.log_path:
            return
        line = {"t": int(time.time()), "kind": "llm_call", "agent": agent, "purpose": purpose, "prompt": prompt[:2000],
                "answer": answer, "latency": round(latency, 3), "ok": error is None}
        if tokens is not None:
            line["tokens"] = tokens
        if error:
            line["error"] = error
        if priority is not None:
            line["priority"] = priority
        try:
            with self._log_lock:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line) + "\n")
        except OSError:
            pass


# ---- the canned mode ---------------------------------------------------------------------------------------------
OPENERS = {"ruthless": "No mercy.", "opportunistic": "Business is business.", "peaceful": "Quiet times.", "cautious": "Careful now.",
           "patient": "Slow and steady.", "impatient": "Enough waiting.", "greedy": "Mine, all mine.", "sociable": "Friends,",
           "curious": "Interesting.", "restless": "Onwards."}


def fake_answer(system: str, prompt: str) -> str:
    """Template answers for `--llm fake`. Understands the tasks voice.py asks for (rewrite, diary) and the diplomacy ones (hello and
    announce like a rewrite, answer with a canned decision); keeps the numbers of the input unchanged."""
    if prompt.startswith("TASK: answer"):                  # diplomacy: a canned but repeatable ACCEPT / REFUSE / WAIT (diplomacy.fake_decision)
        from . import diplomacy
        canned = diplomacy.fake_answer(prompt)
        if canned:
            return canned
    tone = ""
    for line in system.splitlines():
        if line.upper().startswith("TONE:"):
            tone = line.split(":", 1)[1].strip().split(",")[0].strip().lower()
    opener = OPENERS.get(tone, "Noted.")
    for line in prompt.splitlines():
        if line.startswith("TEXT:"):                      # task: rewrite a board post
            return f"{opener} {line[5:].strip()}"
        if line.startswith("EVENT:"):                     # task: a diary entry
            return f"{opener} {line[6:].strip()}"
    return opener


def make_llm(url: str | None, model: str = "", timeout: float = DEFAULT_TIMEOUT, max_per_hour: int = DEFAULT_MAX_PER_HOUR,
             log_dir: str | Path | None = "logs"):
    """A client for `url`, or None when the feature is off (url empty)."""
    return LLMClient(url, model, timeout, max_per_hour, log_dir) if url else None
