"""A tiny stand-in for the OGameX API with the same response shapes, used to test agents without a server."""
from __future__ import annotations

import math
from urllib.parse import parse_qs, urlparse

from swarm.client import ApiError

BASE = {  # metal, crystal, deuterium, growth factor
    "metal_mine": (60, 15, 0, 1.5), "crystal_mine": (48, 24, 0, 1.6), "deuterium_synthesizer": (225, 75, 0, 1.5),
    "solar_plant": (75, 30, 0, 1.5), "robot_factory": (400, 120, 200, 2.0), "research_lab": (200, 400, 200, 2.0),
    "shipyard": (400, 200, 100, 2.0), "metal_store": (1000, 0, 0, 2.0), "crystal_store": (1000, 500, 0, 2.0),
    "deuterium_store": (1000, 1000, 0, 2.0),
}
TECH = {"energy_technology": (0, 800, 400, 2.0), "computer_technology": (0, 400, 600, 2.0),
        "combustion_drive": (400, 0, 600, 2.0), "laser_technology": (200, 100, 0, 2.0),
        "astrophysics": (4000, 8000, 4000, 1.75), "impulse_drive": (2000, 4000, 600, 2.0),
        "espionage_technology": (200, 1000, 200, 2.0)}


# Units: (category, metal, crystal, deuterium, cargo capacity). Costs are made cheap so tests stay short.
UNITS = {
    "espionage_probe": ("ship", 0, 300, 0, 5),
    "small_cargo": ("ship", 500, 500, 0, 5000),
    "large_cargo": ("ship", 1500, 1500, 0, 25000),
    "light_fighter": ("ship", 600, 200, 0, 50),
    "colony_ship": ("ship", 1000, 2000, 1000, 7500),
    "rocket_launcher": ("defense", 500, 0, 0, 0),
}


class FakeGame:
    def __init__(self):
        self.now = 0.0
        self.res = {"metal": 500.0, "crystal": 500.0, "deuterium": 0.0}
        self.levels = {k: 0 for k in BASE}
        self.tech = {k: 0 for k in TECH}
        self.bq: dict | None = None   # {"name", "level", "end"}
        self.rq: dict | None = None
        self.post_log: list[tuple[str, dict]] = []
        self.token = None
        # --- fleets, messages, other players ---
        self.ships = {k: 0 for k, v in UNITS.items() if v[0] == "ship"}
        self.defense = {"rocket_launcher": 0}
        self.slots_max = 1
        self.missions: list[dict] = []
        self.messages: list[dict] = []
        self.next_message_id = 1
        self.others = [{"user_id": 2, "username": "Victim", "coord": (1, 1, 5), "score": 0,
                        "resources": {"metal": 40000, "crystal": 20000, "deuterium": 10000},
                        "ships": {"small_cargo": 1}, "defense": {"rocket_launcher": 0}}]
        self.attacks_resolved = 0
        self.dark_matter = 8000
        self.hostile: list[dict] = []   # foreign fleets heading for us, as GET /fleet shows them
        self.merchant: dict | None = None   # {"type", "receive": {res: {"rate", "per_unit_given"}}}
        self.merchant_quality = 0.9         # share of the fair value the merchant pays
        self.trades_done = 0
        self.battle_losses = 0
        self.class_stuff = False            # the /character-class endpoint exists
        self.char_class = None
        self.colony_stuff = False           # show astrophysics, colony ships... (off so older tests are unchanged)
        self.colonies: list[tuple] = []     # positions where a colony ship settled
        self.expedition_stuff = False       # expeditions: position 16, expedition slots, outcome messages (off so older tests are unchanged)
        self.exp_outcomes = ["resources"]   # outcome of each expedition in turn (cycled): resources, ships, dark_matter, nothing, delay, loss
        self.exp_done = 0
        self.is_discoverer = False
        self.field_max = 10 ** 6           # building fields on the planet; every building level uses one

    # -- economy --
    def energy_prod(self):
        return 20 * self.levels["solar_plant"] * 1.1 ** self.levels["solar_plant"]

    def energy_need(self):
        return (10 * self.levels["metal_mine"] * 1.1 ** self.levels["metal_mine"]
                + 10 * self.levels["crystal_mine"] * 1.1 ** self.levels["crystal_mine"]
                + 20 * self.levels["deuterium_synthesizer"] * 1.1 ** self.levels["deuterium_synthesizer"])

    def rate(self):
        need = self.energy_need()
        f = 1.0 if need <= 0 else min(1.0, self.energy_prod() / need)
        m, c, d = self.levels["metal_mine"], self.levels["crystal_mine"], self.levels["deuterium_synthesizer"]
        return {"metal": 30 + 30 * m * 1.1 ** m * f, "crystal": 15 + 20 * c * 1.1 ** c * f,
                "deuterium": 10 * d * 1.1 ** d * f}

    def storage(self):
        return {"metal": 10000 * 2 ** self.levels["metal_store"], "crystal": 10000 * 2 ** self.levels["crystal_store"],
                "deuterium": 10000 * 2 ** self.levels["deuterium_store"]}

    def advance(self, seconds: float):
        step = 5.0
        t = 0.0
        while t < seconds:
            dt = min(step, seconds - t)
            for r, per_hour in self.rate().items():
                self.res[r] = min(self.storage()[r], self.res[r] + per_hour * dt / 3600)
            self.now += dt
            t += dt
            for attr, store in (("bq", self.levels), ("rq", self.tech)):
                q = getattr(self, attr)
                if q and self.now >= q["end"]:
                    store[q["name"]] = q["level"]
                    setattr(self, attr, None)
            self._process_missions()

    # -- helpers --
    def cost(self, name, level_now, table):
        m, c, d, f = table[name]
        k = f ** level_now
        return {"metal": int(m * k), "crystal": int(c * k), "deuterium": int(d * k)}

    def paid(self, cost):
        return all(self.res[r] >= cost[r] for r in cost)

    def qdict(self, q, kind):
        if not q:
            return []
        return [{"queue_id": 1, "object_id": 1, "machine_name": q["name"], "title": q["name"], "active": True,
                 "seconds_remaining": int(q["end"] - self.now), "duration_seconds": q["dur"],
                 "target_level": q["level"]}]

    # -- fleets and messages --
    def _process_missions(self):
        for m in list(self.missions):
            if not m["arrived"] and self.now >= m["arrive"]:
                m["arrived"] = True
                if m["mission"] == "colonisation":
                    self.colonies.append(m["to"])
                    self.missions.remove(m)   # the ship is used up
                    continue
                if m["mission"] == "expedition":
                    self._expedition_arrives(m)
                    continue
                target = next(o for o in self.others if o["coord"] == m["to"])
                if m["mission"] == "espionage":
                    self._add_message("espionage_report", {
                        "target": {"galaxy": m["to"][0], "system": m["to"][1], "position": m["to"][2], "type": 1,
                                   "owner_id": target["user_id"]},
                        "resources": dict(target["resources"]), "ships": dict(target["ships"]),
                        "defense": dict(target["defense"]), "buildings": {}, "research": {}})
                else:
                    self.attacks_resolved += 1
                    sent = dict(m["ships"])
                    fighters = sum(n for k, n in sent.items() if k in ("light_fighter", "cruiser", "heavy_fighter"))
                    defenders = sum(target["ships"].values()) + sum(target["defense"].values())
                    lost = defenders > 0.8 * fighters  # the defence wins
                    if lost:
                        loot = {r: 0 for r in target["resources"]}
                        my_loss = sum(n * (UNITS[k][1] + UNITS[k][2]) for k, n in sent.items())
                        their_loss = (defenders // 2) * 1000
                        m["ships"] = {k: 0 for k in sent}  # nothing comes home
                    else:
                        loot = {r: target["resources"][r] // 2 for r in target["resources"]}
                        my_loss, their_loss = 0, 0
                    for r, v in loot.items():
                        target["resources"][r] -= v
                        self.res[r] += v
                    self.battle_losses += my_loss
                    self._add_message("combat_report", {
                        "target": {"galaxy": m["to"][0], "system": m["to"][1], "position": m["to"][2], "type": 1,
                                   "owner_id": target["user_id"]},
                        "attacker": {"player_id": 1, "resource_loss": my_loss, "units": sent},
                        "defender": {"player_id": target["user_id"], "resource_loss": their_loss},
                        "loot": loot, "debris": {"metal": my_loss // 2, "crystal": 0, "deuterium": 0}, "rounds": 5})
            if self.now >= m["back"]:
                self.missions.remove(m)
                for name, n in m["ships"].items():
                    self.ships[name] += n
                for r, v in (m.get("bring") or {}).items():
                    self.res[r] += v

    def _add_message(self, key, report, body=""):
        self.messages.append({"id": self.next_message_id, "key": key, "viewed": False, "report": report, "body": body})
        self.next_message_id += 1

    def _expedition_arrives(self, m):
        outcome = self.exp_outcomes[self.exp_done % len(self.exp_outcomes)]
        self.exp_done += 1
        key, body = {
            "resources": ("expedition_gain_resources", "Your scouts found a rich asteroid.\n\nMetal 12,345 have been captured."),
            "ships": ("expedition_gain_ships", "A derelict fleet joins you.\n\nThe following ships are now part of the fleet:\n"
                                              "Small Cargo: 3\nLight Fighter: 12"),
            "dark_matter": ("expedition_gain_dark_matter", "Strange matter.\n\n(150 Dark Matter)"),
            "nothing": ("expedition_failed", "Nothing but void."),
            "delay": ("expedition_failed_and_delay", "A storm delays you."),
            "loss": ("expedition_loss_of_fleet", "The last message was: a black hole."),
        }[outcome]
        if outcome == "resources":
            m["bring"] = {"metal": 12345}
        elif outcome == "ships":
            m["ships"] = dict(m["ships"], small_cargo=m["ships"].get("small_cargo", 0) + 3, light_fighter=m["ships"].get("light_fighter", 0) + 12)
        elif outcome == "dark_matter":
            self.dark_matter += 150
        elif outcome == "delay":
            m["back"] += 7200
        elif outcome == "loss":
            m["ships"] = {k: 0 for k in m["ships"]}
        self._add_message(key, None, body)

    def _units_list(self):
        items = []
        for name, (cat, mc, cc, dc, cap) in UNITS.items():
            if name == "colony_ship" and not self.colony_stuff:
                continue
            cost = {"metal": mc, "crystal": cc, "deuterium": dc}
            fits = [int(self.res[r] // v) for r, v in cost.items() if v > 0]
            amount = self.ships.get(name, 0) if cat == "ship" else self.defense.get(name, 0)
            met = self.levels["shipyard"] >= (4 if name == "colony_ship" else 1)
            items.append({"id": 1, "machine_name": name, "title": name, "category": cat, "amount": amount,
                          "cost_per_unit": {**cost, "energy": 0}, "seconds_per_unit": 10, "requirements_met": met,
                          "character_class_met": True, "max_buildable": min(fits) if (met and fits) else 0})
        return items

    def _handle_new_get(self, path):
        url = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path
        if route == "/fleet":
            return {"missions": [dict(m, seconds_remaining=max(0, m["arrive_at"] - self.now)) for m in self.hostile]}
        if route == "/merchant":
            return self._merchant_state()
        if route == "/planets/10/units":
            return {"planet_id": 10, "units": self._units_list(), "queue": []}
        if route.startswith("/galaxy/"):
            _, _, g, s = route.split("/")
            taken = {o["coord"] for o in self.others} | {(1, 3, 8)}
            return {"galaxy": int(g), "system": int(s), "positions": [
                {"position": p, "planet": ({"id": 99, "name": "x"} if (int(g), int(s), p) in taken else None)}
                for p in range(1, 16)]}
        if route == "/character-class":
            if not self.class_stuff:
                raise ApiError(404, "not_found", route)
            return {"class": self.char_class, "change_cost": 0 if self.char_class is None else 500000,
                    "dark_matter": self.dark_matter, "options": []}
        if route == "/ships":
            return {"ships": [{"machine_name": n, "cargo_capacity": v[4]} for n, v in UNITS.items() if v[0] == "ship"]}
        if route == "/highscore":
            rows = [{"rank": 1, "user_id": o["user_id"], "username": o["username"], "score": o["score"], "is_me": False,
                     "main_planet": {"galaxy": o["coord"][0], "system": o["coord"][1], "position": o["coord"][2]},
                     "alliance_tag": None, "total_ships": None} for o in self.others]
            rows.append({"rank": 2, "user_id": 1, "username": "Fake", "score": 0, "is_me": True,
                         "main_planet": {"galaxy": 1, "system": 3, "position": 8}, "alliance_tag": None, "total_ships": None})
            return {"type": "general", "players": rows, "total_players": len(rows),
                    "me": {"general": {"rank": 2, "score": 0}}}
        if route == "/messages":
            rows = [m for m in self.messages if not (q.get("unread_only") == "1" and m["viewed"])
                    and (q.get("category") != "espionage" or m["key"] == "espionage_report")
                    and (q.get("category") != "combat" or m["key"] == "combat_report")
                    and (q.get("category") != "expeditions" or m["key"].startswith("expedition_"))]
            return {"messages": [{"id": m["id"], "key": m["key"], "viewed": m["viewed"], "body": m.get("body", ""),
                                  "has_espionage_report": m["key"] == "espionage_report",
                                  "has_battle_report": m["key"] == "combat_report"} for m in rows]}
        if route.startswith("/messages/"):
            m = next((m for m in self.messages if m["id"] == int(route.rsplit("/", 1)[1])), None)
            if m is None:
                raise ApiError(404, "message_not_found", route)
            return {"message": {"id": m["id"], "key": m["key"],
                                "espionage_report": m["report"] if m["key"] == "espionage_report" else None,
                                "battle_report": m["report"] if m["key"] == "combat_report" else None}}
        return None

    BASE_RATE = {"metal": 3.0, "crystal": 2.0, "deuterium": 1.0}

    def _merchant_state(self):
        return {"dark_matter": self.dark_matter, "call_cost": 3500, "merchant": self.merchant}

    def _handle_merchant_post(self, path, body):
        if path == "/merchant/call":
            if self.merchant:
                raise ApiError(422, "merchant_already_active", "already")
            if self.dark_matter < 3500:
                raise ApiError(422, "insufficient_dark_matter", "no")
            sell = body["type"]
            self.dark_matter -= 3500
            self.merchant = {"type": sell, "receive": {
                r: {"rate": self.BASE_RATE[r] * self.merchant_quality,
                    "per_unit_given": self.BASE_RATE[r] * self.merchant_quality / self.BASE_RATE[sell]}
                for r in self.BASE_RATE if r != sell}}
            return self._merchant_state()
        if path == "/merchant/dismiss":
            self.merchant = None
            return self._merchant_state()
        if path == "/planets/10/merchant/trade":
            if not self.merchant:
                raise ApiError(422, "no_active_merchant", "none")
            sell = self.merchant["type"]
            cost, got = 0, {}
            for r, n in body["receive"].items():
                if r == sell:
                    raise ApiError(422, "validation_failed", "same resource")
                cost += n / self.merchant["receive"][r]["per_unit_given"]
                got[r] = n
            cost = int(cost) + 1
            if cost > body["give_amount"] or cost > self.res[sell]:
                raise ApiError(422, "trade_failed", "too expensive")
            self.res[sell] -= cost
            for r, n in got.items():
                self.res[r] += n
            self.merchant = None
            self.trades_done += 1
            return {"gave": {sell: cost}, "received": got, "dark_matter": self.dark_matter}
        return None

    def _handle_new_post(self, path, body):
        merchant = self._handle_merchant_post(path, body)
        if merchant is not None:
            return merchant
        if path == "/character-class":
            if self.char_class is not None:
                raise ApiError(422, "class_change_refused", "costs dark matter")
            self.char_class = body["class"]
            return {"class": self.char_class}
        if path == "/planets/10/units":
            name, amount = body["machine_name"], int(body["amount"])
            item = next(u for u in self._units_list() if u["machine_name"] == name)
            if not item["requirements_met"] or amount > item["max_buildable"] or amount < 1:
                raise ApiError(422, "not_enough_resources", name)
            for r in ("metal", "crystal", "deuterium"):
                self.res[r] -= item["cost_per_unit"][r] * amount
            if item["category"] == "ship":
                self.ships[name] += amount
            else:
                self.defense[name] += amount
            return {"requested": amount, "ordered": amount, "queue": []}
        if path == "/planets/10/fleet":
            if len(self.missions) >= self.slots_max:
                raise ApiError(422, "mission_rejected", "Maximum number of fleets reached.")
            to = (body["galaxy"], body["system"], body["position"])
            if body["mission"] == "expedition":
                if not self.expedition_stuff or to[2] != 16 or self.tech["astrophysics"] < 1:
                    raise ApiError(422, "mission_rejected", "Fleets cannot be sent to this target. You have to research Astrophysics first.")
                if not 1 <= int(body.get("holding_hours", 0)) <= self.tech["astrophysics"]:
                    raise ApiError(422, "invalid_holding_time", "holding_hours for an expedition must be between 1 and your astrophysics level.")
                if sum(1 for x in self.missions if x["mission"] == "expedition") >= self._expedition_max():
                    raise ApiError(422, "mission_rejected", "You are conducting too many expeditions at the same time.")
            elif body["mission"] != "colonisation" and not any(o["coord"] == to for o in self.others):
                raise ApiError(422, "mission_rejected", "No planet there.")
            for n, v in body["ships"].items():
                if self.ships.get(n, 0) < v:
                    raise ApiError(422, "mission_rejected", "Not enough units on the planet to send the fleet.")
            for n, v in body["ships"].items():
                self.ships[n] -= v
            m = {"mission": body["mission"], "to": to, "ships": dict(body["ships"]), "arrived": False,
                 "arrive": self.now + 60, "back": self.now + 120}
            self.missions.append(m)
            return {"mission": {"id": len(self.post_log), "mission": body["mission"], "seconds_remaining": 60}}
        if path.startswith("/messages/") and path.endswith("/read"):
            mid = int(path.split("/")[2])
            for m in self.messages:
                if m["id"] == mid:
                    m["viewed"] = True
            return {"message": {"id": mid, "viewed": True}}
        return None

    def _expedition_max(self):
        return math.isqrt(self.tech["astrophysics"]) + (2 if self.is_discoverer else 0)

    # -- API --
    def get(self, path):
        extra = self._handle_new_get(path)
        if extra is not None:
            return extra
        if path == "/universe":
            return {"name": "fake"}
        if path == "/me":
            return {"id": 1}
        if path == "/status":
            rate = self.rate()
            return {
                "server_time": int(self.now),
                "player": {"id": 1, "username": "Fake", "current_planet_id": 10, "dark_matter": self.dark_matter},
                "planets": [{
                    "id": 10, "name": "Home", "type": "planet", "coordinates": {"galaxy": 1, "system": 3, "position": 8},
                    "resources": {r: int(v) for r, v in self.res.items()}, "storage": self.storage(),
                    "production_per_hour": {r: int(v) for r, v in rate.items()},
                    "energy": int(self.energy_prod() - self.energy_need()),
                    "fields": {"used": sum(self.levels.values()), "max": self.field_max},
                    "building_queue": self.qdict(self.bq, "b"), "unit_queue": []}],
                "research": {"levels": {k: v for k, v in self.tech.items()}, "queue": self.qdict(self.rq, "r")},
                "fleet_slots": {"used": len(self.missions), "max": self.slots_max,
                                **({"expedition_used": sum(1 for x in self.missions if x["mission"] == "expedition"),
                                    "expedition_max": self._expedition_max()} if self.expedition_stuff else {})},
                "unread_messages": 0}
        if path == "/planets/10/buildings":
            items = []
            for name in BASE:
                lvl = self.levels[name]
                cost = self.cost(name, lvl, BASE)
                items.append({"id": 1, "machine_name": name, "title": name, "level": lvl, "next_level": lvl + 1,
                              "cost": cost, "duration_seconds": 60 * (lvl + 1), "requirements_met": True,
                              "can_afford": self.paid(cost)})
            return {"planet_id": 10, "buildings": items, "queue": self.qdict(self.bq, "b"), "queue_full": self.bq is not None}
        if path == "/planets/10/research":
            items = []
            for name in TECH:
                if name in ("astrophysics", "impulse_drive", "espionage_technology") and not (self.colony_stuff or self.expedition_stuff):
                    continue
                lvl = self.tech[name]
                cost = self.cost(name, lvl, TECH)
                items.append({"id": 1, "machine_name": name, "title": name, "level": lvl, "next_level": lvl + 1,
                              "cost": cost, "duration_seconds": 90 * (lvl + 1), "requirements_met": self.levels["research_lab"] > 0,
                              "can_afford": self.paid(cost)})
            return {"planet_id": 10, "research": items, "queue": self.qdict(self.rq, "r"),
                    "queue_full": self.rq is not None, "research_lab_upgrading": bool(self.bq and self.bq["name"] == "research_lab")}
        raise ApiError(404, "not_found", path)

    def post(self, path, body=None):
        body = body or {}
        self.post_log.append((path, body))
        extra = self._handle_new_post(path, body)
        if extra is not None:
            return extra
        name = body.get("machine_name")
        if path == "/planets/10/buildings":
            if name not in BASE:
                raise ApiError(422, "unknown_object", name)
            if name == "research_lab" and self.rq:
                raise ApiError(422, "research_in_progress", name)
            if self.bq:
                raise ApiError(422, "queue_full", name)
            cost = self.cost(name, self.levels[name], BASE)
            if sum(self.levels.values()) >= self.field_max:
                self.field_refusals = getattr(self, "field_refusals", 0) + 1
                raise ApiError(422, "not_enough_fields", name)
            if not self.paid(cost):
                raise ApiError(422, "not_enough_resources", name)
            for r, v in cost.items():
                self.res[r] -= v
            dur = 60 * (self.levels[name] + 1)
            self.bq = {"name": name, "level": self.levels[name] + 1, "end": self.now + dur, "dur": dur}
            return {"queue": self.qdict(self.bq, "b")}
        if path == "/planets/10/research":
            if self.rq:
                raise ApiError(422, "queue_full", name)
            if self.bq and self.bq["name"] == "research_lab":
                raise ApiError(422, "research_lab_upgrading", name)
            cost = self.cost(name, self.tech[name], TECH)
            if not self.paid(cost):
                raise ApiError(422, "not_enough_resources", name)
            for r, v in cost.items():
                self.res[r] -= v
            dur = 90 * (self.tech[name] + 1)
            self.rq = {"name": name, "level": self.tech[name] + 1, "end": self.now + dur, "dur": dur}
            return {"queue": self.qdict(self.rq, "r")}
        raise ApiError(404, "not_found", path)
