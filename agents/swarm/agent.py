"""One AI player: owns an account, looks at the game, decides, acts, sleeps."""
from __future__ import annotations

import json
import random
from dataclasses import replace
import secrets
import threading
import time
from pathlib import Path

from .brain import FIELD_ALARM, choose_building, choose_research, fields_left, pay
from . import colonise, doctrine, expedition, fullplanet, recycle
from .combat import tech_from_research
import random as _random
from . import targetlearn
from .bandit import outcome_of
from .military import (Memory, choose_strike, judge_trade, summarize_battle, choose_units, coord_of, pick_attack, pick_target, summarize_report)
from .client import ApiClient, ApiError
from .opponents import record_result, record_spy, summary as opp_summary
from .personas import Persona
from .defense import AGGRESSOR_MEMORY, GUARD_SECONDS, PEACE_WATCH_SECONDS, RECENT_RAID, ALERT_SECONDS, COOLDOWN_SECONDS as DEFEND_COOLDOWN, WATCH_SECONDS, choose_defense, defence_share, find_threats
from .trade import COOLDOWN_SECONDS, choose_sale, plan_deal
from .logistics_agent import LogisticsMixin
from .social_agent import SocialMixin
from .bounty_agent import BountyMixin
from .moons_agent import MoonsMixin
from .llm_agent import VoiceMixin
from .alliance_agent import AllianceMixin
from . import diplomacy as D
from .diplomacy_agent import DiplomacyMixin


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

    def agent_personas(self) -> dict[int, str]:
        """Persona name of every agent we know of, by game user id."""
        with self._lock:
            return {int(e["user_id"]): e.get("persona") for e in self._data.values() if e.get("user_id") is not None}

    def agent_names(self) -> dict[int, str]:
        """Agent name by game user id."""
        with self._lock:
            return {int(e["user_id"]): n for n, e in self._data.items() if e.get("user_id") is not None}

    def put(self, name: str, entry: dict) -> None:
        with self._lock:
            self._data[name] = entry
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)


class Agent(LogisticsMixin, SocialMixin, BountyMixin, MoonsMixin, VoiceMixin, AllianceMixin, DiplomacyMixin):
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
        self._spend_mode = False           # the home planet is full (or its terraformer chain is out of reach): surplus goes to ships, defence, colonies
        self._buildings_seen: dict[int, list] = {}   # latest building list per planet (the full-planet plan reads the chain from it)
        self._plan_logged: dict[int, float] = {}     # planet -> server time of its last full_planet_plan event
        self._colony_radius = colonise.SEARCH_SYSTEMS   # systems searched either side of home; grows when nothing was free
        self._economy_saving = False
        self._economy_forced = False       # ... and the wait is for a must-do item (solar plant, a goal), not a choice to save up
        self._economy_reserve: dict = {}   # cost of the item the economy is saving for: military purchases must leave it alone
        self._last_battle_check = -1e12
        self.my_tech = None  # (weapons, shields, armour) levels, from /status
        self.planets_owned = 1
        self.memory_levels: dict = {}   # research levels from the latest /status (for the doctrine)
        self.alert_until = 0.0           # server time until which a hostile fleet keeps us alert
        self._defend_cd: dict[int, float] = {}   # planet id -> no new defence order before this time
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
        self._voice_notice(event, fields)   # llm_agent.py: a notable event may become a diary line (does nothing without --llm)

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

        self.planets_owned = len(planets)
        self._choose_class()
        goal_buildings, goal_research = self._colony_goals(status, home)
        threats = self._watch()
        hints += self._plan_moons(status, planets)    # moons: sensor phalanx (build, scan); no moon = nothing
        threats = threats + self._phalanx_threats(threats)   # attacks the phalanx saw coming count like the ones /fleet shows
        hints += self._plan_alliance(status, planets, threats)   # alliances (off without sociability or alliance endpoints): found/join, share intel, warn allies
        hints += self._plan_social(status, planets)   # galaxy board (off without --board): warnings raise the guard, traders swap goods
        hints += self._plan_diplomacy(status, planets, threats)   # first contact and relationships (diplomacy.py): hello/answer on the board, pacts
        if self.now < self.alert_until:
            hints.append(WATCH_SECONDS)   # someone hostile is about: look often
        elif self._on_guard():
            hints.append(GUARD_SECONDS)   # robbed recently: the raider will come back
        else:
            hints.append(PEACE_WATCH_SECONDS)
        hints += self._save_fleet(status, planets, threats)   # logistics: dodge a raid we cannot win, bring the fleet home later

        self._spend_mode = self._full_home(home, status)
        for planet in planets:
            have = dict(planet["resources"])
            if planet is home:
                self._prepare_defense(home, have, threats)   # before the economy spends the stock the raider is after
            else:   # a colony an attack is flying to arms itself too (a new colony is the softest target there is)
                mine = [t for t in threats if t.is_attack and t.target is not None and t.target == self._coord_tuple(planet)]
                if mine:
                    self._prepare_defense(planet, have, mine)
            self._economy_saving = False
            self._economy_reserve = {}
            self._economy_forced = False
            hints += self._plan_buildings(planet, have, research_busy, goals=goal_buildings if planet is home else None)
            if planet is home:
                hints += self._plan_research(planet, have, research_queue, goals=goal_research)
                colony_first = self._spend_mode and self.memory.expanding   # the colony ship comes before the ships that would block the shipyard
                if colony_first:
                    hints += self._plan_colony(status, planet, have, goal_research)
                hints += self._plan_military(status, planet, have, self._economy_saving, self._economy_reserve)
                hints += self._plan_recycle(status, planet, have)
                hints += self._plan_expedition(status, planet)
                if not colony_first:
                    hints += self._plan_colony(status, planet, have, goal_research)
        self._note_full_planets(status, planets, home)
        hints += self._plan_supply(status, planets)   # logistics: feed a starved colony from the richest planet

        return self._sleep_time(hints)

    def _plan_buildings(self, planet: dict, have: dict, research_busy: bool, goals: dict | None = None) -> list[float]:
        queue = planet["building_queue"]
        if queue:
            return [q["seconds_remaining"] for q in queue if q.get("active")] or []
        data = self.client.get(f"/planets/{planet['id']}/buildings")
        self._remember_lab(planet, data["buildings"])
        self._buildings_seen[planet["id"]] = data["buildings"]
        if data["queue_full"]:
            return []
        choice = choose_building(self.persona, self.rng, planet, data["buildings"],
                                 research_busy=research_busy, units_busy=bool(planet["unit_queue"]), have=have, goals=goals)
        if choice.machine_name is None:
            self._economy_saving = not choice.full   # a full planet is not saving for anything: ships and defence still work
            self._economy_reserve = dict(choice.goal_cost or {}) if self._economy_saving else {}
            self._economy_forced = bool(choice.forced) and self._economy_saving
            self.log("wait", planet=planet["id"], why=choice.reason, hint=_r(choice.wait_seconds), cost=choice.goal_cost,
                     store=planet.get("storage"))
            if self._maybe_trade(planet, choice, have):
                return [5.0]  # we just got what was missing: look again at once
            return [choice.wait_seconds if choice.wait_seconds else IDLE_RECHECK]
        return self._act("build", planet, choice, data["buildings"], have, f"/planets/{planet['id']}/buildings")

    # ---- full planets (swarm/fullplanet.py) -----------------------------------------------------------
    def _full_home(self, home: dict, status: dict) -> bool:
        """True when the home planet can grow no more by building: no free field, or the terraformer chain no longer fits."""
        left = fields_left(home)
        if left is None:
            return False
        if left <= 0:
            return True
        buildings = self._buildings_seen.get(home["id"])
        return left <= FIELD_ALARM and bool(buildings) and fullplanet.chain_exists(buildings) and not fullplanet.reachable(buildings, left)

    def _note_full_planets(self, status: dict, planets: list[dict], home: dict) -> None:
        """Once an hour and planet (running low or full): the full_planet_plan event, saying what the agent decided about it."""
        self._any_planet_full = any((fields_left(pl) is not None and fields_left(pl) <= 0) for pl in planets)   # a full colony also wants a new colony
        for planet in planets:
            left = fields_left(planet)
            buildings = self._buildings_seen.get(planet["id"])
            if left is None or left > FIELD_ALARM or buildings is None or self.now - self._plan_logged.get(planet["id"], -1e12) < fullplanet.PLAN_EVERY:
                continue
            self._plan_logged[planet["id"]] = self.now
            plan = fullplanet.plan(left=left, buildings=buildings, research_levels=status["research"].get("levels") or {},
                                   planets_owned=self.planets_owned, expanding=bool(self.memory.expanding),
                                   spending=self._spend_mode and planet is home)
            self.log("full_planet_plan", planet=planet["id"], free=left, decision=plan["decision"], reachable=plan["reachable"],
                     need=plan["need"], planets=self.planets_owned, why=plan["why"])

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

    def _coord_tuple(self, planet: dict) -> tuple[int, int, int] | None:
        c = planet.get("coordinates") or {}
        try:
            return (int(c["galaxy"]), int(c["system"]), int(c["position"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _prepare_defense(self, home: dict, have: dict, threats: list) -> None:
        """Turn part of the stock into defence units before the fleet lands: it would be stolen otherwise."""
        known = {c for c, when in self.memory.aggressors.items() if self.now - when < AGGRESSOR_MEMORY}
        share = defence_share(self.persona, threats, known, self._on_guard())
        if share <= 0 or self.now < self._defend_cd.get(home["id"], 0.0) or home["unit_queue"]:
            return
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        coming = [x.arrives_in for x in threats if x.is_attack]
        pick = choose_defense(self.persona, units, have, share, min(coming) if coming else None)
        if pick is None:
            return
        name, amount = pick
        self._defend_cd[home["id"]] = self.now + DEFEND_COOLDOWN
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
        if threats:
            first = min(threats, key=lambda t: t.arrives_in)
            why = f"{first.kind} on its way from {':'.join(map(str, first.origin))}, arrives in {_r(first.arrives_in)} s"
        else:
            why = "on guard after a recent raid: rearming"
        self.log("defend", what=name, amount=amount, planet=home["id"], why=why)

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

    def _plan_research(self, planet: dict, have: dict, research_queue: list, goals: dict | None = None) -> list[float]:
        if research_queue:
            return [q["seconds_remaining"] for q in research_queue if q.get("active")] or []
        left = fields_left(planet)
        urgent = left is not None and left <= FIELD_ALARM   # running out of fields (or full): research is the way out, whatever the persona's taste
        if self.rng.random() > self.persona.research_share and not goals and not urgent:
            return []
        if self.lab_level is None:
            self._remember_lab(planet, self.client.get(f"/planets/{planet['id']}/buildings")["buildings"])
        if not self.lab_level:
            return []
        data = self.client.get(f"/planets/{planet['id']}/research")
        if data["research_lab_upgrading"] or data["queue_full"]:
            return []
        choice = choose_research(self.persona, self.rng, planet, data["research"], have=have, goals=goals)
        if choice.machine_name is None:
            return [choice.wait_seconds if choice.wait_seconds else IDLE_RECHECK]
        return self._act("research", planet, choice, data["research"], have, f"/planets/{planet['id']}/research")

    def _act(self, kind: str, planet: dict, choice, items: list[dict], have: dict, path: str) -> list[float]:
        item = next(i for i in items if i["machine_name"] == choice.machine_name)
        try:
            result = self.client.post(path, {"machine_name": choice.machine_name})
        except ApiError as e:
            if e.code == "shipyard_busy":     # ships are being built: not a refusal worth reporting, just try again soon
                self.log("busy", kind=kind, what=choice.machine_name)
                return [60.0]
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
    def _plan_military(self, status: dict, home: dict, have: dict, saving: bool, reserve: dict | None = None) -> list[float]:
        p = self.persona
        spend = self._spend_mode
        if max(p.aggression, p.scout_interest, p.army_share, p.probes_wanted) <= 0 and not spend:
            return []
        buyer = replace(p, unit_weights=fullplanet.spending_weights(p.unit_weights)) if spend else p   # a full planet also buys defence
        now = float(status["server_time"])
        self.my_tech = tech_from_research(status["research"].get("levels"))
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        ships_here = {u["machine_name"]: u["amount"] for u in units if u["amount"] > 0 and u["category"] != "defense"}
        hints: list[float] = []
        keep = self._colony_ship_reserve(units, ships_here) if spend else None   # what a full planet keeps back for its colony ship

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
        # A fighting persona (doctrine.py) spends from the SURPLUS instead: what the item it is saving for does not need.
        # The reserve is never touched, and it no longer sits on a rich stock because one thing is dear.
        if doctrine.applies(p):
            ranking = self._doctrine_ranking(status["research"].get("levels") or {})
            share = self._army_share(now)
            if spend:
                share = max(share, fullplanet.FULL_SHARE)
            if ranking:
                self._doctrine_note(ranking, units, now)
            budget = have
            if saving and doctrine.is_poor(have):
                if self.rng.random() >= p.army_share:   # a planet just starting up has no real surplus: the old coin flip
                    return hints
            elif saving:
                # Waiting for a must-do item: only the surplus may be spent. Saving up by choice: the old coin flip
                # (army_share) may spend from the whole stock; when it says no the surplus is spent anyway.
                lucky = (not self._economy_forced) and self.rng.random() < p.army_share
                budget = have if lucky else doctrine.surplus(have, reserve)
            if keep:
                budget = doctrine.surplus(budget, keep)
            if not home["unit_queue"]:
                hints += self._build_units(home, units, have, ships_here, budget=budget, share=share, ranking=ranking, persona=buyer)
            return hints
        may_buy = (not saving) or self.rng.random() < p.army_share
        if may_buy and not home["unit_queue"]:
            hints += self._build_units(home, units, have, ships_here, share=fullplanet.FULL_SHARE if spend else None, persona=buyer,
                                       budget=doctrine.surplus(have, keep) if keep else None)
        return hints

    def _colony_ship_reserve(self, units: list[dict], ships_here: dict) -> dict | None:
        """Twice the price of a colony ship, kept out of the ship spending while a full planet is about to build one."""
        m = self.memory
        if not m.expanding or m.colony_sent_at or ships_here.get("colony_ship", 0) >= 1:
            return None
        item = next((u for u in units if u["machine_name"] == "colony_ship"), None)
        if item is None:
            return None
        return {r: 2 * int((item.get("cost_per_unit") or {}).get(r, 0)) for r in ("metal", "crystal", "deuterium")}

    # ---- doctrine (swarm/doctrine.py) ----------------------------------------------------------------
    def _doctrine_ranking(self, research_levels: dict) -> list[tuple[str, float]] | None:
        """Warships ranked by the battle simulator against the defence the spy reports showed. None = the old fixed weights:
        used until some report showed a defence or targets have been out of reach."""
        if not doctrine.applies(self.persona):
            return None
        defenders, source = self._doctrine_defenders()
        if not source.startswith("average") and doctrine.pressure(self.memory.doctrine, self.now) < doctrine.MIN_SKIPS:
            return None
        their = next((tech_from_research(i.get("research")) for i in sorted(self.memory.intel.values(), key=lambda i: -i.get("at", 0))
                      if isinstance(i, dict) and i.get("research")), None)
        return doctrine.rank_ships(doctrine.candidates(self.persona), defenders, tech_from_research(research_levels), their)

    def _doctrine_defenders(self) -> tuple[dict, str]:
        return doctrine.typical_defenders(self.memory.intel, self.now)

    def _army_share(self, now: float) -> float:
        """The persona's army_share, raised while its targets are out of reach (army_push event when it goes up)."""
        st = self.memory.doctrine
        share, why = doctrine.army_share_now(self.persona, st, now)
        if share > self.persona.army_share and abs(share - (st.get("share") or self.persona.army_share)) >= 0.05:
            self.log("army_push", share=share, base=self.persona.army_share, why=why)
        st["share"] = share
        return share

    def _doctrine_note(self, ranking: list[tuple[str, float]], units: list[dict], now: float) -> None:
        """Log `doctrine` when the best buildable ship or the ship to aim for changes."""
        st = self.memory.doctrine
        usable = {u["machine_name"] for u in units if u["requirements_met"] and u["character_class_met"]}
        chosen = next((s for s, _ in ranking if s in usable), None)
        aim = doctrine.aim_ship(ranking, self.memory_levels)
        if (chosen, aim) == (st.get("choice"), st.get("aim")):
            return
        defenders, source = self._doctrine_defenders()
        st.update(choice=chosen, aim=aim, table=[[s, sc] for s, sc in ranking])
        mix = ", ".join(f"{n} {k}" for k, n in sorted(defenders.items(), key=lambda kv: -kv[1])[:4])
        self.log("doctrine", chosen=chosen, aim=aim, table=[[s, sc] for s, sc in ranking],
                 why=f"against {mix} ({source}): best ship that can be built now is {chosen}, best to aim for is {aim}")

    # ---- expeditions ---------------------------------------------------------------------------------
    def _plan_expedition(self, status: dict, home: dict) -> list[float]:
        """Read the reports of finished expeditions, then maybe send a new one (rules and odds in swarm/expedition.py)."""
        if not expedition.wants_expeditions(self.persona):
            return []
        st = self.memory.expedition
        if st["inflight"] and self.now - st["last_read"] >= 120:
            st["last_read"] = self.now
            self._read_expeditions()
        astro = int((status["research"].get("levels") or {}).get("astrophysics", 0))
        stay = expedition.can_consider(self.persona, now=self.now, state=st, astro=astro, fleet_slots=status["fleet_slots"],
                                       on_guard=self._on_guard(), alert=self.now < self.alert_until)
        if stay:
            self._expedition_skipped(stay)
            return []
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        ships_here = {u["machine_name"]: u["amount"] for u in units if u["amount"] > 0 and u["category"] != "defense"}
        if not self.ship_stats:
            self.ship_stats = {s["machine_name"]: s for s in self.client.get("/ships")["ships"]}
        cargo_of = {n: int(s.get("cargo_capacity") or 0) for n, s in self.ship_stats.items()}
        try:
            top = max([int(r.get("score") or 0) for r in self._get_players(self.now)[0]] or [0])
        except ApiError:
            top = 0
        d = expedition.decide(self.persona, self.rng, ships_here=ships_here, cargo_of=cargo_of, top_score=top, state=st)
        if not d.ships:
            st["next_at"] = self.now + d.retry_in
            self._expedition_skipped(d.why or "no fleet to send")
            return []
        c = home["coordinates"]
        try:
            result = self.client.post(f"/planets/{home['id']}/fleet", {
                "mission": "expedition", "galaxy": c["galaxy"], "system": c["system"], "position": 16,
                "ships": d.ships, "holding_hours": d.holding_hours})
        except ApiError as e:
            self.stats["refused"] += 1
            st["next_at"] = self.now + expedition.RETRY_SECONDS
            self.log("expedition_refused", code=e.code, msg=e.message[:120])
            return []
        cost_of = {u["machine_name"]: sum((u.get("cost_per_unit") or {}).get(r, 0) for r in ("metal", "crystal", "deuterium")) for u in units}
        expedition.record_sent(st, self.now, d.ships, expedition.fleet_value(d.ships, cost_of), d.retry_in)
        self.log("expedition_sent", at=f"{c['galaxy']}:{c['system']}:16", ships=d.ships, hours=d.holding_hours, why=d.why,
                 seconds=result["mission"]["seconds_remaining"])
        return []

    def _expedition_skipped(self, why: str) -> None:
        """Say why an expedition-minded agent stayed home, but only when the reason changes or after an hour (the report counts them)."""
        key = why.split(":")[0]
        last = getattr(self, "_exp_skip", None)
        if last and last[0] == key and self.now - last[1] < 3600:
            return
        self._exp_skip = (key, self.now)
        self.log("expedition_skipped", why=why)

    def _read_expeditions(self) -> None:
        data = self.client.get("/messages?category=expeditions&unread_only=1&per_page=20")
        for row in reversed(data["messages"]):   # oldest first
            self.client.post(f"/messages/{row['id']}/read")
            res = expedition.parse_message(row.get("key", ""), row.get("body", ""))
            self.log("expedition_result", **expedition.record_result(self.memory.expedition, self.now, res))

    # ---- character class -----------------------------------------------------------------------------
    def _choose_class(self) -> None:
        """Once per agent: take the free first choice of character class that suits the personality."""
        m = self.memory
        if m.class_done or not self.persona.character_class:
            return
        try:
            state = self.client.get("/character-class")
            if state.get("class") is None and state.get("change_cost", 1) == 0:
                self.client.post("/character-class", {"class": self.persona.character_class})
                self.log("class", chosen=self.persona.character_class)
            m.class_done = True
        except ApiError as e:
            if e.status == 404:      # an older game without the endpoint: never ask again
                m.class_done = True
            self.log("class_refused", code=e.code, msg=e.message[:100])

    # ---- colonies ------------------------------------------------------------------------------------
    def _colony_goals(self, status: dict, home: dict) -> tuple[dict | None, dict | None]:
        """Decide whether to found a colony and, if so, which levels the home planet must reach first."""
        m = self.memory
        b2, r2 = recycle.goals(self.persona, m.debris, self.now, status["research"].get("levels") or {})
        b1, r1 = self._colony_goals_only(status, home)
        levels = status["research"].get("levels") or {}
        self.memory_levels = levels
        # fighters whose targets stay out of reach also work towards the best warship and better weapons (doctrine.py)
        b3, r3 = doctrine.research_goals(self.persona, m.doctrine, self.now, self._doctrine_ranking(levels), levels)
        if b1 is None and not (b2 or r2) and not (b3 or r3):
            return None, None
        buildings = {**(b2 or {}), **(b1 or {})}
        research = {**(r2 or {}), **(r1 or {})}
        for name, lv in b3.items():
            buildings[name] = max(lv, buildings.get(name, 0))
        for name, lv in r3.items():
            research[name] = max(lv, research.get(name, 0))
        return buildings, research

    def _colony_goals_only(self, status: dict, home: dict) -> tuple[dict | None, dict | None]:
        m = self.memory
        full = (fields_left(home) is not None and fields_left(home) <= 0) or getattr(self, "_any_planet_full", False)
        if m.colony_sent_at or (self.persona.expansion <= 0 and not full):
            return None, None
        if not m.expanding:
            if self.now < m.colony_until:
                return None, None
            if full:    # nothing else to grow with: colonies, whatever the persona's appetite and planet limit
                if self.planets_owned >= colonise.FULL_PLANET_CAP:
                    return None, None
                why = ("no free fields on the home planet" if fields_left(home) is not None and fields_left(home) <= 0
                       else "a colony planet has no free field") + ": a colony is the way to grow"
            elif colonise.wants_colony(self.persona, home, self.planets_owned, self.rng):
                why = f"only {fields_left(home)} free fields on the home planet"
            else:
                return None, None
            m.expanding = True
            self.log("expand", why=why, planets=self.planets_owned, astrophysics_needed=colonise.astro_needed(self.planets_owned))
        return colonise.goals(self.planets_owned, status["research"].get("levels") or {})

    def _plan_colony(self, status: dict, home: dict, have: dict, missing_research: dict | None) -> list[float]:
        m = self.memory
        if m.colony_sent_at:
            return self._follow_colony(home)
        if not m.expanding or missing_research:
            return []          # still researching: the research and building goals do the work
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        ships = {u["machine_name"]: u["amount"] for u in units if u["amount"] > 0 and u["category"] != "defense"}
        if ships.get("colony_ship", 0) >= 1:
            slots = status["fleet_slots"]
            if slots["used"] >= slots["max"]:
                return [60.0]
            return self._send_colony(status, home)
        item = next((u for u in units if u["machine_name"] == "colony_ship"), None)
        if item is None or home["unit_queue"]:
            return [60.0] if home["unit_queue"] else []
        if not item.get("requirements_met", True):
            return [IDLE_RECHECK]      # the shipyard goal is being built
        if item.get("max_buildable", 1) < 1:
            return [IDLE_RECHECK]
        try:
            self.client.post(f"/planets/{home['id']}/units", {"machine_name": "colony_ship", "amount": 1})
        except ApiError as e:
            self.stats["refused"] += 1
            self.log("units_refused", what="colony_ship", code=e.code, msg=e.message[:100])
            return [60.0]
        for r in ("metal", "crystal", "deuterium"):
            have[r] = have.get(r, 0) - item["cost_per_unit"].get(r, 0)
        self.log("units", what="colony_ship", amount=1, why="founding a colony")
        return [60.0]

    def _send_colony(self, status: dict, home: dict) -> list[float]:
        m = self.memory
        c = home["coordinates"]
        home_coord = (c["galaxy"], c["system"], c["position"])
        astro = int((status["research"].get("levels") or {}).get("astrophysics", 0))
        views = []
        for g, s in colonise.systems_to_scan(home_coord, self._colony_radius):
            try:
                views.append(self.client.get(f"/galaxy/{g}/{s}"))
            except ApiError:
                continue
        pick = colonise.pick_target(self.persona, self.rng, home_coord, views, astro)
        if pick is None:
            m.colony_until = self.now + 3600
            self._colony_radius = min(self._colony_radius + 6, 36)   # look further next time
            self.log("colony_failed", why=f"no free position within {self._colony_radius - 6 if self._colony_radius > 6 else 6} systems of home")
            return []
        (g, s, p), why = pick
        try:
            result = self.client.post(f"/planets/{home['id']}/fleet", {
                "mission": "colonisation", "galaxy": g, "system": s, "position": p, "type": "planet",
                "ships": {"colony_ship": 1}})
        except ApiError as e:
            self.stats["refused"] += 1
            m.colony_until = self.now + 1800
            self.log("colony_failed", at=f"{g}:{s}:{p}", code=e.code, why=e.message[:120])
            return []
        m.colony_sent_at = self.now
        m.colony_planets_before = self.planets_owned
        self.log("colony_sent", at=f"{g}:{s}:{p}", why=why, seconds=result["mission"]["seconds_remaining"])
        return [min(120.0, float(result["mission"]["seconds_remaining"]) + 5)]

    def _follow_colony(self, home: dict) -> list[float]:
        m = self.memory
        if self.planets_owned > m.colony_planets_before:
            m.colony_sent_at, m.expanding = 0.0, False
            m.colony_until = self.now + 1800
            self.log("colony_founded", planets=self.planets_owned)
            return []
        if self.now - m.colony_sent_at < 120:
            return [60.0]
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        if any(u["machine_name"] == "colony_ship" and u["amount"] > 0 for u in units):   # it came back: the position was taken
            m.colony_sent_at = 0.0
            m.colony_until = self.now + 900
            self.log("colony_failed", why="the ship came back")
            return []
        if self.now - m.colony_sent_at > 6 * 3600:   # lost track of it: start over
            m.colony_sent_at, m.expanding = 0.0, False
            self.log("colony_failed", why="no sign of the ship or the colony")
            return []
        return [60.0]

    def _allowed_target_ids(self) -> set[int] | None:
        return self._without_allies(None if self.attack_humans else self.store.agent_user_ids())   # never an ally

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
                             exclude_user_id=self.user_id, focus=self._bounty_focus())
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
            self._diplo_contact(intel.owner_id, "spy report")   # diplomacy: we know this player now
            self.memory.intel[c] = {"at": now, "loot": intel.loot, "defenders": intel.defenders, "owner_id": intel.owner_id,
                                    "ships": intel.ships, "defense": intel.defense, "research": intel.research}
            self.memory.pending.pop(c, None)
            if "debris" in report:
                recycle.remember(self.memory.debris, c, recycle.total_of(report["debris"]), now, add=False)
            record_spy(self.memory.opponents, c, now, intel.loot, intel.ships, intel.defense)
            if intel.loot < self.persona.min_loot:  # not worth a visit: do not spy on it again for a while
                self.memory.avoid_until[c] = now + 4 * self.persona.spy_cooldown_s
            self.stats["reports"] += 1
            self.log("report", at=f"{c[0]}:{c[1]}:{c[2]}", loot=intel.loot, defenders=intel.defenders,
                     strength=self.memory.opponents[c].get("strength"))
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
            b["report_id"] = row["id"]
            self._social_battle(b)   # galaxy board: tell the others when we were robbed
            self._diplo_battle(b)    # diplomacy: contact, and a raid on us (or by us) costs trust; under a pact it is a betrayal
            verdict = judge_trade(b) if b["role"] == "attacker" else None
            margin = outcome = None
            if b["role"] == "attacker" and b["coord"] and None not in b["coord"]:
                margin = self.memory.arm_for.pop(tuple(b["coord"]), None)
                if margin is not None:
                    outcome = outcome_of(verdict, b["loot"] + b["their_loss"], b["my_loss"])
                    self.memory.bandit.update(margin, outcome)
            tkey = ratio = None
            if b["role"] == "attacker" and b["coord"] and None not in b["coord"]:
                tf = self.memory.target_for.pop(tuple(b["coord"]), None)
                if tf:
                    tkey, ratio = tf["key"], targetlearn.ratio_of(b["loot"], b["their_loss"], b["my_loss"], tf["loot"])
                    targetlearn.update(self.memory.targets, tkey, ratio)
            self.log("battle", at=b["at"], role=b["role"], margin=margin, outcome=outcome, tkey=tkey,
                     ratio=None if ratio is None else round(ratio, 2), group=targetlearn.group_of(self.name), my_loss=b["my_loss"], their_loss=b["their_loss"],
                     loot=b["loot"], debris=b["debris"], rounds=b["rounds"], verdict=verdict,
                     mine=b["my_units"], theirs=b["their_units"], their_id=b["their_id"], report=row["id"])
            self._bounty_battle(b)   # galaxy board: end a joined revenge, claim a bounty (the referee reads the line above)
            recycle.remember(self.memory.debris, b["coord"], b["debris"], now, add=True)   # a new debris field to harvest
            if b["role"] != "attacker":
                continue
            self.memory.battles = (self.memory.battles + [{"at": now, **{k: b[k] for k in ("at", "my_loss", "their_loss", "loot")}, "verdict": verdict}])[-30:]
            if b["coord"] and None not in b["coord"]:
                rec = record_result(self.memory.opponents, tuple(b["coord"]), now, verdict, b["loot"] + b["their_loss"], b["my_loss"])
                self.log("opponent", **opp_summary(tuple(b["coord"]), rec))
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
        group = targetlearn.group_of(self.name)
        pick = pick_attack(self.persona, self.rng, self.memory, now, ships_here, focus=self._bounty_focus(),
                           learn_rng=_random.Random(f"{self.name}:tgt:{int(now // 30)}") if group == "learned" else None)
        if pick is None:
            return False
        coord, intel, reason = pick
        betray = None
        if self._diplo_protected(coord, intel.owner_id):   # a pact or alliance partner is never raided, whatever else says go
            betray = self._diplo_betrayal_target(coord, intel.owner_id)   # ... except a pact a ruthless agent may break today (diplomacy.py)
            why = "pact or alliance partner" if betray is None else self._diplo_betray_gate(intel)
            if why:
                self.memory.intel.pop(coord, None)
                self.log("diplo_blocked", at=f"{coord[0]}:{coord[1]}:{coord[2]}", why=why)
                return False
        if not self.ship_stats:
            self.ship_stats = {s["machine_name"]: s for s in self.client.get("/ships")["ships"]}
        # How much to over-estimate the defenders is learned (bandit.py); its own random stream keeps the agent's other choices unchanged.
        margin = self.memory.bandit.choose(_random.Random(f"{self.name}:{self.memory.bandit.pulls}"))
        sim_info: dict = {}
        strike, go, sim_reason, kind = choose_strike(self.persona, self.rng, ships_here, self.ship_stats, intel,
                                                     self.my_tech, self.memory.risk_scale, margin, info=sim_info)
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
            if doctrine.out_of_reach(sim_reason):
                doctrine.note_skip(self.memory.doctrine, now)   # counted: a run of these makes a raider spend more on its army
            self.memory.intel[coord]["skip_until"] = now + 1800
            self.log("attack_skipped", at=where, loot=intel.loot, why=sim_reason)
            return False
        if betray is not None and (sim_info.get("win_chance") or 0.0) < D.BETRAY_WIN_CHANCE:   # breaking a pact needs a clear win
            self.memory.intel[coord]["skip_until"] = now + 1800
            self.log("attack_skipped", at=where, loot=intel.loot, why=f"no clear win ({sim_info.get('win_chance')}) to break a pact for")
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
        self._diplo_we_attacked(intel.owner_id, betrayal=betray is not None, at=where, loot=intel.loot,
                                win=sim_info.get("win_chance"))   # diplomacy: a raid costs trust with that player (a pact broken on purpose: betrayal)
        doctrine.note_attack(self.memory.doctrine, now)
        self.memory.arm_for[coord] = margin
        tkey = targetlearn.key_of(intel.loot, intel.defenders, intel.ally is not None)
        self.memory.target_for[coord] = {"key": tkey, "loot": int(intel.loot)}
        self.memory.intel.pop(coord, None)
        self.stats["attacks"] += 1
        self.log("attack", at=f"{coord[0]}:{coord[1]}:{coord[2]}", margin=margin, tkey=tkey, group=group, why=reason, ships=strike,
                 seconds=result["mission"]["seconds_remaining"], saw_ships=intel.ships, saw_defense=intel.defense,
                 saw_research=intel.research, intel_age=None if intel.at is None else round(now - intel.at),
                 intel_source="ally" if intel.ally is not None else "own", composition_known=intel.composition_known,
                 sim_win_chance=sim_info.get("win_chance"))
        return True

    def _build_units(self, home: dict, units: list[dict], have: dict, ships_here: dict, *, budget: dict | None = None,
                     share: float | None = None, ranking: list | None = None, persona: Persona | None = None) -> list[float]:
        """Order ships. `budget` (default: all of `have`) is what may be spent: a saving agent passes only its surplus."""
        pick = choose_units(persona or self.persona, self.rng, units, have if budget is None else budget, ships_here,
                            want_cargo=self.memory.need_cargo, ranking=ranking, share=share)
        if pick is None:
            return []
        name, amount = pick
        if self._spend_mode:
            amount = min(amount, fullplanet.FULL_BATCH)   # a short queue: the shipyard must soon be free for the colony ship / nanite factory
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

    # ---- recycling -----------------------------------------------------------------------------------
    _recycle_at = 0.0   # server time before which the recycling code does not look again (it costs requests)

    def _plan_recycle(self, status: dict, home: dict, have: dict) -> list[float]:
        """Debris remembered from battle and spy reports (swarm/recycle.py): buy recyclers when it is big enough, send them."""
        m = self.memory
        recycle.forget_old(m.debris, self.now)
        if not m.debris or self.now < self._recycle_at:
            return []
        self._recycle_at = self.now + 60.0
        units = self.client.get(f"/planets/{home['id']}/units")["units"]
        item = next((u for u in units if u["machine_name"] == recycle.RECYCLER), None)
        if item is None:       # a game without recyclers
            return []
        if not self.ship_stats:
            self.ship_stats = {s["machine_name"]: s for s in self.client.get("/ships")["ships"]}
        cargo = int(self.ship_stats.get(recycle.RECYCLER, {}).get("cargo_capacity") or recycle.DEFAULT_CARGO)
        owned = int(item.get("amount", 0))
        c = home["coordinates"]
        slots = status["fleet_slots"]
        pick = recycle.pick_field(m.debris, self.now, coord_of(c["galaxy"], c["system"], c["position"]))
        if owned >= 1 and pick and slots["used"] < slots["max"]:
            return self._send_recyclers(home, have, pick[0], pick[1], recycle.fleet_for(pick[1], owned, cargo), cargo)
        if home["unit_queue"]:
            return []
        n, why = recycle.recyclers_to_buy(self.persona, item, have, recycle.remembered_total(m.debris, self.now), owned, cargo)
        if n < 1:
            return []
        try:
            self.client.post(f"/planets/{home['id']}/units", {"machine_name": recycle.RECYCLER, "amount": n})
        except ApiError as e:
            self.stats["refused"] += 1
            self.log("units_refused", what=recycle.RECYCLER, code=e.code, msg=e.message[:100])
            return [60.0]
        for r in ("metal", "crystal", "deuterium"):
            have[r] = have.get(r, 0) - item["cost_per_unit"].get(r, 0) * n
        self.stats["units"] += n
        self.log("units", what=recycle.RECYCLER, amount=n, why=why)
        return []

    def _send_recyclers(self, home: dict, have: dict, coord: tuple, amount: int, n: int, cargo: int) -> list[float]:
        m, where = self.memory, f"{coord[0]}:{coord[1]}:{coord[2]}"
        body = {"mission": "recycle", "galaxy": coord[0], "system": coord[1], "position": coord[2], "type": "debris_field",
                "ships": {recycle.RECYCLER: n}}

        def skip(why: str, **extra) -> list[float]:
            m.debris[coord]["sent_until"] = self.now + 1800     # do not ask again every round
            self.log("recycle_skipped", at=where, debris=amount, why=why, **extra)
            return []
        try:
            check = self.client.post(f"/planets/{home['id']}/fleet/check", body)
        except ApiError as e:
            return skip(e.message[:120], code=e.code)
        possible = (check.get("missions") or {}).get("recycle") or {}
        if not possible.get("possible", True):
            m.debris.pop(coord, None)       # the game says no such field (any more)
            self.log("recycle_skipped", at=where, debris=amount, why=str(possible.get("reason"))[:120])
            return []
        haul = min(amount, int(check.get("cargo_capacity") or n * cargo))
        fuel = int(check.get("deuterium_consumption") or 0)
        ok, why = recycle.worth_trip(haul, fuel, int(have.get("deuterium", 0)))
        if not ok:
            return skip(why)
        try:
            result = self.client.post(f"/planets/{home['id']}/fleet", body)
        except ApiError as e:
            self.stats["refused"] += 1
            return skip(e.message[:120], code=e.code)
        seconds = int(result["mission"]["seconds_remaining"])
        recycle.after_sending(m.debris, coord, haul, self.now, 2.0 * seconds)
        have["deuterium"] = have.get("deuterium", 0) - fuel
        self.log("recycle_sent", at=where, ships=n, debris=amount, haul=haul, fuel=fuel, seconds=seconds, why=why)
        return [2.0 * seconds + 5]

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
            if self.routine is not None and self._routine_offline(stop, speedup):
                continue                       # offline: no API calls; we slept (at most a minute) and look at the stop file again
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
            if self.routine is not None:
                delay = self._routine_delay(delay)
            stop.wait(delay / max(speedup, 0.01))

    # ---- activity routine (routine.py; off unless run_swarm.py gives the agent one) -----------------------------------
    routine = None                  # a swarm.routine.Routine, or None = always online (the default)
    routine_clock = staticmethod(time.time)
    _routine_was_online = False
    _routine_announced = False
    ROUTINE_CHUNK = 60.0            # an offline agent wakes at least this often (real seconds) to look at the stop file

    def _routine_offline(self, stop: threading.Event, speedup: float) -> bool:
        """True if the agent is offline right now (it then sleeps and makes no API call). Never raises: a broken routine = online."""
        try:
            now = self.routine_clock()
            if self.routine.online(now):
                if not self._routine_was_online:
                    self._routine_was_online = True
                    self.log("online", minutes=round((self.routine.session_end(now) - now) / 60.0, 1))
                return False
            if self._routine_was_online or not self._routine_announced:
                self._routine_was_online = False
                self._routine_announced = True
                self.log("offline", back_in_min=round(self.routine.seconds_until_online(now) / 60.0, 1))
            wait = min(self.routine.seconds_until_online(now) + 0.01, self.ROUTINE_CHUNK)
            stop.wait(max(wait, 0.01) / max(speedup, 0.01))
            return True
        except Exception:
            return False

    def _routine_delay(self, delay: float) -> float:
        """The pause after a round: unchanged inside the session; if it would outlast the session, only up to the end of it
        (the loop then sees the agent is offline and sleeps until the next session)."""
        try:
            return self.routine.clip(self.routine_clock(), delay)
        except Exception:
            return delay


def _r(x):
    return None if x is None else round(x)
