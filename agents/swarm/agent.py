"""One AI player: owns an account, looks at the game, decides, acts, sleeps."""
from __future__ import annotations

import json
import random
import secrets
import threading
import time
from pathlib import Path

from .brain import choose_building, choose_research, pay
from .combat import tech_from_research
from .military import (Memory, choose_strike, judge_trade, summarize_battle, choose_units, coord_of, pick_attack, pick_target, summarize_report)
from .client import ApiClient, ApiError
from .personas import Persona
from .defense import AGGRESSOR_MEMORY, GUARD_SECONDS, PEACE_WATCH_SECONDS, RECENT_RAID, ALERT_SECONDS, COOLDOWN_SECONDS as DEFEND_COOLDOWN, WATCH_SECONDS, choose_defense, defence_share, find_threats
from .trade import COOLDOWN_SECONDS, choose_sale, plan_deal


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

    def agent_user_ids(self) -> set[int]:
        """Game user ids of every agent we know of (so agents can be told apart from human players)."""
        with self._lock:
            return {int(e["user_id"]) for e in self._data.values() if e.get("user_id") is not None}

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
        attack_humans: bool = False, memory_dir: str | None = None,
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
        self.attack_humans = attack_humans   # False = only ever spy on / attack other agents
        self.user_id: int | None = None
        self.memory_path = Path(memory_dir) / f"{name}.json" if memory_dir else None
        self.memory = self._load_memory()
        self.ship_stats: dict[str, dict] = {}
        self._players: tuple[float, list[dict], int] | None = None  # (fetched at, highscore rows, own score)
        self._economy_saving = False
        self._last_battle_check = -1e12
        self.my_tech = None  # (weapons, shields, armour) levels, from /status
        self.alert_until = 0.0           # server time until which a hostile fleet keeps us alert
        self.defend_cooldown_until = 0.0
        self._seen_threats: set[int] = set()
        self.dark_matter = 0     # from /status each round
        self.now = 0.0           # server time of the current round
        self.stats = {"threats": 0, "defences": 0, "trades": 0, "ticks": 0, "builds": 0, "researches": 0, "refused": 0, "errors": 0,
                      "spies": 0, "reports": 0, "attacks": 0, "units": 0, "battles": 0, "lessons": 0}

    # ---- memory on disk ------------------------------------------------------------------------------
    def _load_memory(self) -> Memory:
        if self.memory_path and self.memory_path.exists():
            try:
                return Memory.from_dict(json.loads(self.memory_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass  # a damaged file must never stop an agent: start with a blank memory
        return Memory()

    def save_memory(self) -> None:
        if not self.memory_path:
            return
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.memory_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.memory.to_dict()), encoding="utf-8")
            tmp.replace(self.memory_path)
        except OSError:
            pass

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
            self.user_id = int(data["user"]["id"])
            self.store.put(self.name, {"email": email, "password": password, "token": data["token"],
                                       "persona": self.persona.name, "user_id": self.user_id})
            self.log("registered", player=data["user"]["username"], persona=self.persona.name)
            return

        self.client.token = entry["token"]
        try:
            me = self.client.get("/me")
            self.user_id = int(me["id"])
            if entry.get("user_id") != self.user_id:  # accounts made before user ids were stored
                self.store.put(self.name, {**entry, "user_id": self.user_id})
        except ApiError as e:
            if e.status != 401:
                raise
            data = self.client.post("/auth/login", {"email": entry["email"], "password": entry["password"],
                                                    "token_name": self.name})
            self.client.token = data["token"]
            self.store.put(self.name, {**entry, "token": data["token"]})
            self.user_id = entry.get("user_id")
            self.log("relogged")

    # ---- one round of play ---------------------------------------------------------------------------
    def tick(self) -> float:
        """Look, decide, act. Returns the number of seconds to wait before the next round."""
        self.stats["ticks"] += 1
        status = self.client.get("/status")
        hints: list[float] = []
        self.dark_matter = int(status["player"].get("dark_matter") or 0)
        server_time = status.get("server_time")
        self.now = time.time() if server_time is None else float(server_time)
        research_queue = status["research"]["queue"]
        planets = [p for p in status["planets"] if p["type"] == "planet"]
        if not planets:
            return self.persona.tick_seconds

        # Research is paid by one planet; use the one the game says is current, else the first.
        home = next((p for p in planets if p["id"] == status["player"]["current_planet_id"]), planets[0])
        research_busy = bool(research_queue)

        threats = self._watch()
        if self.now < self.alert_until:
            hints.append(WATCH_SECONDS)   # someone hostile is about: look often
        elif self._on_guard():
            hints.append(GUARD_SECONDS)   # robbed recently: the raider will come back
        else:
            hints.append(PEACE_WATCH_SECONDS)

        for planet in planets:
            have = dict(planet["resources"])
            if planet is home:
                self._prepare_defense(home, have, threats)   # before the economy spends the stock the raider is after
            self._economy_saving = False
            hints += self._plan_buildings(planet, have, research_busy)
            if planet is home:
                hints += self._plan_research(planet, have, research_queue)
                hints += self._plan_military(status, planet, have, self._economy_saving)

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
            self._economy_saving = True  # do not spend the savings on ships
            self.log("wait", planet=planet["id"], why=choice.reason, hint=_r(choice.wait_seconds))
            if self._maybe_trade(planet, choice, have):
                return [5.0]  # we just got what was missing: look again at once
            return [choice.wait_seconds if choice.wait_seconds else IDLE_RECHECK]
        return self._act("build", planet, choice, data["buildings"], have, f"/planets/{planet['id']}/buildings")

    # ---- defence -------------------------------------------------------------------------------------
    def _watch(self) -> list:
        """Foreign fleets heading for us. A hostile one keeps the agent alert for a while."""
        try:
            missions = self.client.get("/fleet").get("missions", [])
        except ApiError:
            return []
        threats = find_threats(missions)
        for t in threats:
            if t.id not in self._seen_threats:
                self._seen_threats.add(t.id)
                self.stats["threats"] += 1
                self.log("threat", kind=t.kind, mission=t.id, **{"from": ":".join(map(str, t.origin))}, arrives_in=_r(t.arrives_in))
        for t in threats:
            if t.is_attack:
                self.memory.aggressors[t.origin] = self.now
        if threats:
            self.alert_until = self.now + ALERT_SECONDS
        return threats

    def _on_guard(self) -> bool:
        return any(self.now - when < RECENT_RAID for when in self.memory.aggressors.values())

    def _prepare_defense(self, home: dict, have: dict, threats: list) -> None:
        """Turn part of the stock into defence units before the fleet lands: it would be stolen otherwise."""
        known = {c for c, when in self.memory.aggressors.items() if self.now - when < AGGRESSOR_MEMORY}
        share = defence_share(self.persona, threats, known, self._on_guard())
        if share <= 0 or self.now < self.defend_cooldown_until or home["unit_queue"]:
            return
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        coming = [x.arrives_in for x in threats if x.is_attack]
        pick = choose_defense(self.persona, units, have, share, min(coming) if coming else None)
        if pick is None:
            return
        name, amount = pick
        self.defend_cooldown_until = self.now + DEFEND_COOLDOWN
        try:
            self.client.post(f"/planets/{home['id']}/units", {"machine_name": name, "amount": amount})
        except ApiError as e:
            self.stats["refused"] += 1
            self.log("units_refused", what=name, code=e.code, msg=e.message[:100])
            return
        cost = next(u["cost_per_unit"] for u in units if u["machine_name"] == name)
        for r in ("metal", "crystal", "deuterium"):
            have[r] = have.get(r, 0) - cost.get(r, 0) * amount
        self.stats["defences"] += amount
        first = min(threats, key=lambda t: t.arrives_in)
        self.log("defend", what=name, amount=amount,
                 why=f"{first.kind} on its way from {':'.join(map(str, first.origin))}, arrives in {_r(first.arrives_in)} s")

    def _maybe_trade(self, planet: dict, choice, have: dict) -> bool:
        """Call the merchant when stuck waiting with a big surplus of something else (see trade.py). True = traded."""
        sell = choose_sale(self.persona, choice.goal_cost, have, planet["storage"], choice.wait_seconds,
                           self.dark_matter, self.now, self.memory.trade_cooldown_until)
        if sell is None:
            return False
        self.memory.trade_cooldown_until = self.now + COOLDOWN_SECONDS   # set first: never hammer, whatever happens
        why = choice.reason
        try:
            try:
                state = self.client.post("/merchant/call", {"type": sell})
                self.dark_matter = int(state["dark_matter"])
            except ApiError as e:
                if e.code != "merchant_already_active":
                    raise
                state = self.client.get("/merchant")   # one is already waiting (e.g. from an expedition): use him
            deal = plan_deal(state["merchant"], choice.goal_cost, have, planet["storage"])
            if deal is None:
                self.client.post("/merchant/dismiss", {})
                self.log("merchant_dismissed", planet=planet["id"], sold=state["merchant"]["type"], why="rates too poor for: " + why)
                return False
            result = self.client.post(f"/planets/{planet['id']}/merchant/trade",
                                      {"receive": deal.receive, "give_amount": deal.give_amount})
        except ApiError as e:
            self.log("trade_failed", code=e.code, msg=e.message, request=e.request_line)
            return False
        for r, n in result["gave"].items():
            have[r] = have.get(r, 0) - n
        for r, n in result["received"].items():
            have[r] = have.get(r, 0) + n
        self.dark_matter = int(result.get("dark_matter", self.dark_matter))
        self.stats["trades"] += 1
        self.log("trade", planet=planet["id"], gave=result["gave"], received=result["received"], why=why)
        return True

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

    # ---- military ------------------------------------------------------------------------------------
    def _plan_military(self, status: dict, home: dict, have: dict, saving: bool) -> list[float]:
        p = self.persona
        if max(p.aggression, p.scout_interest, p.army_share, p.probes_wanted) <= 0:
            return []
        now = float(status["server_time"])
        self.my_tech = tech_from_research(status["research"].get("levels"))
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        ships_here = {u["machine_name"]: u["amount"] for u in units if u["amount"] > 0 and u["category"] != "defense"}
        hints: list[float] = []

        hints += self._read_intel(now)
        self._read_battles(now)
        slots = status["fleet_slots"]
        if slots["used"] < slots["max"]:
            acted = False
            if p.aggression > 0 and self.memory.intel:
                acted = self._try_attack(home, now, ships_here)
            wants_to_spy = (p.aggression > 0 or p.scout_interest > 0) and len(self.memory.pending) < 2
            if not acted and wants_to_spy and ships_here.get("espionage_probe", 0) >= 1 \
                    and self.rng.random() < max(p.scout_interest, 0.5 * p.aggression):
                self._try_spy(home, now, ships_here)
        if self.memory.pending:
            hints.append(60.0)  # come back soon to read the report

        # Ships are bought from what is spare. While the economy is saving up for something, a militarily minded
        # agent still spends on its army some of the time (army_share), otherwise it would never get to it.
        may_buy = (not saving) or self.rng.random() < p.army_share
        if may_buy and not home["unit_queue"]:
            hints += self._build_units(home, units, have, ships_here)
        return hints

    def _allowed_target_ids(self) -> set[int] | None:
        if self.attack_humans:
            return None
        return self.store.agent_user_ids()

    def _get_players(self, now: float) -> tuple[list[dict], int]:
        """The ranking (cached for 5 minutes) and our own score."""
        if self._players is None or now - self._players[0] > 300:
            rows: list[dict] = []
            data = self.client.get("/highscore?type=general&per_page=100&page=1")
            rows += data["players"]
            self._players = (now, rows, int(data["me"]["general"]["score"]))
        return self._players[1], self._players[2]

    def _try_spy(self, home: dict, now: float, ships_here: dict) -> None:
        players, my_score = self._get_players(now)
        c = home["coordinates"]
        target = pick_target(self.persona, self.rng, coord_of(c["galaxy"], c["system"], c["position"]), my_score, players,
                             self.memory, now, allowed_ids=self._allowed_target_ids(), cooldown=self.persona.spy_cooldown_s,
                             exclude_user_id=self.user_id)
        if target is None:
            return
        galaxy, system, position = target["coord"]
        probes = min(ships_here.get("espionage_probe", 0), self.persona.probes_per_spy)
        try:
            self.client.post(f"/planets/{home['id']}/fleet", {
                "mission": "espionage", "galaxy": galaxy, "system": system, "position": position,
                "ships": {"espionage_probe": probes}})
        except ApiError as e:
            self.stats["refused"] += 1
            self.memory.avoid_until[target["coord"]] = now + 3600
            self.log("spy_refused", target=target["username"], code=e.code, msg=e.message[:120])
            return
        self.memory.spied[target["coord"]] = now
        self.memory.pending[target["coord"]] = now
        self.stats["spies"] += 1
        self.log("spy", target=target["username"], at=f"{galaxy}:{system}:{position}", probes=probes)

    def _read_intel(self, now: float) -> list[float]:
        """Read unread spy reports and keep the useful facts."""
        for c, since in list(self.memory.pending.items()):
            if now - since > 6 * 3600:  # probes were probably shot down: no report will come
                del self.memory.pending[c]
        if not self.memory.pending:
            return []
        data = self.client.get("/messages?category=espionage&unread_only=1&per_page=20")
        for row in data["messages"]:
            if not row.get("has_espionage_report"):
                continue
            report = self.client.get(f"/messages/{row['id']}")["message"].get("espionage_report")
            self.client.post(f"/messages/{row['id']}/read")
            if not report:
                continue
            t = report["target"]
            c = coord_of(t["galaxy"], t["system"], t["position"])
            intel = summarize_report(report)
            self.memory.intel[c] = {"at": now, "loot": intel.loot, "defenders": intel.defenders, "owner_id": intel.owner_id,
                                    "ships": intel.ships, "defense": intel.defense, "research": intel.research}
            self.memory.pending.pop(c, None)
            if intel.loot < self.persona.min_loot:  # not worth a visit: do not spy on it again for a while
                self.memory.avoid_until[c] = now + 4 * self.persona.spy_cooldown_s
            self.stats["reports"] += 1
            self.log("report", at=f"{c[0]}:{c[1]}:{c[2]}", loot=intel.loot, defenders=intel.defenders)
        return []

    def _read_battles(self, now: float) -> None:
        """Read new combat reports: log what happened and learn from the results of our own attacks."""
        if now - self._last_battle_check < 60:
            return
        self._last_battle_check = now
        data = self.client.get("/messages?category=combat&unread_only=1&per_page=20")
        for row in data["messages"]:
            if not row.get("has_battle_report"):
                continue
            report = self.client.get(f"/messages/{row['id']}")["message"].get("battle_report")
            self.client.post(f"/messages/{row['id']}/read")
            b = summarize_battle(report or {}, self.user_id)
            if b is None:
                continue
            if b["role"] == "attacker" and b["probes_only"]:
                self.log("probes_lost", at=b["at"], lost=b["my_loss"])  # shot down while spying
                continue
            self.stats["battles"] += 1
            verdict = judge_trade(b) if b["role"] == "attacker" else None
            self.log("battle", at=b["at"], role=b["role"], my_loss=b["my_loss"], their_loss=b["their_loss"],
                     loot=b["loot"], debris=b["debris"], rounds=b["rounds"], verdict=verdict,
                     mine=b["my_units"], theirs=b["their_units"])
            if b["role"] != "attacker":
                continue
            self.memory.battles = (self.memory.battles + [{"at": now, **{k: b[k] for k in ("at", "my_loss", "their_loss", "loot")}, "verdict": verdict}])[-30:]
            if verdict == "bad":
                self.stats["lessons"] += 1
                self.memory.risk_scale = max(0.2, self.memory.risk_scale * 0.6)
                if b["coord"] and None not in b["coord"]:
                    self.memory.avoid_until[tuple(b["coord"])] = now + 6 * 3600
                self.log("lesson", why=f"lost {b['my_loss']} for {b['loot'] + b['their_loss']} gained: "
                                       f"more careful now (risk x{self.memory.risk_scale:.2f}), avoiding {b['at']} for 6 h")
            elif verdict == "good":
                self.memory.risk_scale = min(1.5, self.memory.risk_scale * 1.1)

    def _try_attack(self, home: dict, now: float, ships_here: dict) -> bool:
        pick = pick_attack(self.persona, self.rng, self.memory, now, ships_here)
        if pick is None:
            return False
        coord, intel, reason = pick
        if not self.ship_stats:
            self.ship_stats = {s["machine_name"]: s for s in self.client.get("/ships")["ships"]}
        strike, go, sim_reason, kind = choose_strike(self.persona, self.rng, ships_here, self.ship_stats, intel,
                                                     self.my_tech, self.memory.risk_scale)
        where = f"{coord[0]}:{coord[1]}:{coord[2]}"
        if strike is None:
            return False
        if kind == "cargo":
            # Winning is pointless if the loot stays behind: ask for cargo ships and look again later.
            self.memory.need_cargo = True
            self.memory.intel[coord]["skip_until"] = now + 600
            self.log("attack_skipped", at=where, loot=intel.loot, why=sim_reason)
            return False
        self.memory.need_cargo = False
        if not go:
            self.memory.intel[coord]["skip_until"] = now + 1800
            self.log("attack_skipped", at=where, loot=intel.loot, why=sim_reason)
            return False
        if sim_reason:
            reason = f"{reason}; {sim_reason}"
        try:
            result = self.client.post(f"/planets/{home['id']}/fleet", {
                "mission": "attack", "galaxy": coord[0], "system": coord[1], "position": coord[2], "ships": strike})
        except ApiError as e:
            self.stats["refused"] += 1
            self.memory.avoid_until[coord] = now + 3600
            self.memory.intel.pop(coord, None)
            self.log("attack_refused", at=f"{coord[0]}:{coord[1]}:{coord[2]}", code=e.code, msg=e.message[:120])
            return False
        self.memory.attacked[coord] = now
        self.memory.intel.pop(coord, None)
        self.stats["attacks"] += 1
        self.log("attack", at=f"{coord[0]}:{coord[1]}:{coord[2]}", why=reason, ships=strike,
                 seconds=result["mission"]["seconds_remaining"], saw_ships=intel.ships, saw_defense=intel.defense,
                 saw_research=intel.research)
        return True

    def _build_units(self, home: dict, units: list[dict], have: dict, ships_here: dict) -> list[float]:
        pick = choose_units(self.persona, self.rng, units, have, ships_here, want_cargo=self.memory.need_cargo)
        if pick is None:
            return []
        name, amount = pick
        try:
            self.client.post(f"/planets/{home['id']}/units", {"machine_name": name, "amount": amount})
        except ApiError as e:
            self.stats["refused"] += 1
            self.log("units_refused", what=name, code=e.code, msg=e.message[:100])
            return [60.0]
        cost = next(u["cost_per_unit"] for u in units if u["machine_name"] == name)
        for r in ("metal", "crystal", "deuterium"):
            have[r] = have.get(r, 0) - cost.get(r, 0) * amount
        self.stats["units"] += amount
        self.log("units", what=name, amount=amount)
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
                self.save_memory()
                failures = 0
            except ApiError as e:
                failures += 1
                self.stats["errors"] += 1
                self.log("api_error", code=e.code, status=e.status, msg=e.message, request=e.request_line)
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
