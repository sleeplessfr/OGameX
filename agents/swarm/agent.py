"""One AI player: owns an account, looks at the game, decides, acts, sleeps."""
from __future__ import annotations

import json
import random
import secrets
import threading
import time
from pathlib import Path

from .brain import choose_building, choose_research, pay
from .client import ApiClient, ApiError
from .personas import Persona


# When an agent is stuck with nothing to do and no idea how long it will take, look again after this long.
IDLE_RECHECK = 300.0


class TokenStore:
    """Remembers each agent's account (email, password, token) in a JSON file so restarts reuse the same players."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, name: str) -> dict | None:
        with self._lock:
            return dict(self._data[name]) if name in self._data else None

    def put(self, name: str, entry: dict) -> None:
        with self._lock:
            self._data[name] = entry
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)


class Agent:
    def __init__(
        self, name: str, persona: Persona, base_url: str, store: TokenStore, rng: random.Random,
        *, log_dir: str | None = None, email_domain: str = "example.com", client: ApiClient | None = None,
    ):
        self.name = name
        self.persona = persona
        self.store = store
        self.rng = rng
        self.client = client or ApiClient(base_url)
        self.email_domain = email_domain
        self.log_path = Path(log_dir) / f"{name}.jsonl" if log_dir else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.lab_level: int | None = None
        self.stats = {"ticks": 0, "builds": 0, "researches": 0, "refused": 0, "errors": 0}

    # ---- logging -------------------------------------------------------------------------------------
    def log(self, event: str, **fields) -> None:
        line = {"t": int(time.time()), "agent": self.name, "event": event, **fields}
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{self.name}] {event} {detail}", flush=True)

    # ---- account -------------------------------------------------------------------------------------
    def ensure_session(self) -> None:
        entry = self.store.get(self.name)
        if entry is None:
            email = f"{self.name}@{self.email_domain}"
            password = secrets.token_urlsafe(18)
            data = self.client.post("/auth/register", {"email": email, "password": password, "token_name": self.name})
            self.client.token = data["token"]
            self.store.put(self.name, {"email": email, "password": password, "token": data["token"],
                                       "persona": self.persona.name})
            self.log("registered", player=data["user"]["username"], persona=self.persona.name)
            return

        self.client.token = entry["token"]
        try:
            self.client.get("/me")
        except ApiError as e:
            if e.status != 401:
                raise
            data = self.client.post("/auth/login", {"email": entry["email"], "password": entry["password"],
                                                    "token_name": self.name})
            self.client.token = data["token"]
            self.store.put(self.name, {**entry, "token": data["token"]})
            self.log("relogged")

    # ---- one round of play ---------------------------------------------------------------------------
    def tick(self) -> float:
        """Look, decide, act. Returns the number of seconds to wait before the next round."""
        self.stats["ticks"] += 1
        status = self.client.get("/status")
        hints: list[float] = []
        research_queue = status["research"]["queue"]
        planets = [p for p in status["planets"] if p["type"] == "planet"]
        if not planets:
            return self.persona.tick_seconds

        # Research is paid by one planet; use the one the game says is current, else the first.
        home = next((p for p in planets if p["id"] == status["player"]["current_planet_id"]), planets[0])
        research_busy = bool(research_queue)

        for planet in planets:
            have = dict(planet["resources"])
            hints += self._plan_buildings(planet, have, research_busy)
            if planet is home:
                hints += self._plan_research(planet, have, research_queue)

        return self._sleep_time(hints)

    def _plan_buildings(self, planet: dict, have: dict, research_busy: bool) -> list[float]:
        queue = planet["building_queue"]
        if queue:
            return [q["seconds_remaining"] for q in queue if q.get("active")] or []
        data = self.client.get(f"/planets/{planet['id']}/buildings")
        self._remember_lab(planet, data["buildings"])
        if data["queue_full"]:
            return []
        choice = choose_building(self.persona, self.rng, planet, data["buildings"],
                                 research_busy=research_busy, units_busy=bool(planet["unit_queue"]), have=have)
        if choice.machine_name is None:
            self.log("wait", planet=planet["id"], why=choice.reason, hint=_r(choice.wait_seconds))
            return [choice.wait_seconds if choice.wait_seconds else IDLE_RECHECK]
        return self._act("build", planet, choice, data["buildings"], have, f"/planets/{planet['id']}/buildings")

    def _plan_research(self, planet: dict, have: dict, research_queue: list) -> list[float]:
        if research_queue:
            return [q["seconds_remaining"] for q in research_queue if q.get("active")] or []
        if self.rng.random() > self.persona.research_share:
            return []
        if self.lab_level is None:
            self._remember_lab(planet, self.client.get(f"/planets/{planet['id']}/buildings")["buildings"])
        if not self.lab_level:
            return []
        data = self.client.get(f"/planets/{planet['id']}/research")
        if data["research_lab_upgrading"] or data["queue_full"]:
            return []
        choice = choose_research(self.persona, self.rng, planet, data["research"], have=have)
        if choice.machine_name is None:
            return [choice.wait_seconds if choice.wait_seconds else IDLE_RECHECK]
        return self._act("research", planet, choice, data["research"], have, f"/planets/{planet['id']}/research")

    def _act(self, kind: str, planet: dict, choice, items: list[dict], have: dict, path: str) -> list[float]:
        item = next(i for i in items if i["machine_name"] == choice.machine_name)
        try:
            result = self.client.post(path, {"machine_name": choice.machine_name})
        except ApiError as e:
            self.stats["refused"] += 1
            self.log("refused", kind=kind, what=choice.machine_name, code=e.code, msg=e.message)
            return [30.0]
        pay(item["cost"], have)
        self.stats["builds" if kind == "build" else "researches"] += 1
        started = next((q for q in result["queue"] if q["machine_name"] == choice.machine_name), None)
        self.log(kind, planet=planet["id"], what=choice.machine_name, level=item["next_level"],
                 why=choice.reason, seconds=started["duration_seconds"] if started else None)
        if started and started.get("active"):
            return [started["duration_seconds"]]
        return []

    def _remember_lab(self, planet: dict, buildings: list[dict]) -> None:
        lab = next((b for b in buildings if b["machine_name"] == "research_lab"), None)
        if lab is not None and planet["type"] == "planet":
            self.lab_level = max(self.lab_level or 0, lab["level"])

    def _sleep_time(self, hints: list[float]) -> float:
        usable = [h for h in hints if h and h > 0]
        base = (min(usable) + 2.0) if usable else self.persona.tick_seconds
        base = max(20.0, min(base, 600.0))
        return base * self.rng.uniform(0.85, 1.25)  # jitter, so agents do not all knock at the same moment

    # ---- main loop -----------------------------------------------------------------------------------
    def run(self, stop: threading.Event, *, max_ticks: int = 0, speedup: float = 1.0, stop_file: str | None = None) -> None:
        failures = 0
        while not stop.is_set():
            if stop_file and Path(stop_file).exists():
                self.log("stop_file_found")
                return
            try:
                if not self.client.token:
                    self.ensure_session()
                delay = self.tick()
                failures = 0
            except ApiError as e:
                failures += 1
                self.stats["errors"] += 1
                self.log("api_error", code=e.code, status=e.status, msg=e.message)
                if e.status == 401:
                    self.client.token = None  # log in again next round
                delay = float(e.details.get("retry_after", 30)) if e.status == 429 else min(300.0, 10.0 * 2 ** failures)
            except Exception as e:  # network down, bad JSON, ... never let one agent kill the swarm
                failures += 1
                self.stats["errors"] += 1
                self.log("error", type=type(e).__name__, msg=str(e)[:200])
                delay = min(300.0, 10.0 * 2 ** failures)
            if failures >= 10:
                self.log("giving_up")
                return
            if max_ticks and self.stats["ticks"] >= max_ticks:
                return
            stop.wait(delay / max(speedup, 0.01))


def _r(x):
    return None if x is None else round(x)
