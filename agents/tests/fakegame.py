"""A tiny stand-in for the OGameX API with the same response shapes, used to test agents without a server."""
from __future__ import annotations

import math

from swarm.client import ApiError

BASE = {  # metal, crystal, deuterium, growth factor
    "metal_mine": (60, 15, 0, 1.5), "crystal_mine": (48, 24, 0, 1.6), "deuterium_synthesizer": (225, 75, 0, 1.5),
    "solar_plant": (75, 30, 0, 1.5), "robot_factory": (400, 120, 200, 2.0), "research_lab": (200, 400, 200, 2.0),
    "shipyard": (400, 200, 100, 2.0), "metal_store": (1000, 0, 0, 2.0), "crystal_store": (1000, 500, 0, 2.0),
    "deuterium_store": (1000, 1000, 0, 2.0),
}
TECH = {"energy_technology": (0, 800, 400, 2.0), "computer_technology": (0, 400, 600, 2.0),
        "combustion_drive": (400, 0, 600, 2.0), "laser_technology": (200, 100, 0, 2.0)}


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

    # -- API --
    def get(self, path):
        if path == "/universe":
            return {"name": "fake"}
        if path == "/me":
            return {"id": 1}
        if path == "/status":
            rate = self.rate()
            return {
                "server_time": int(self.now),
                "player": {"id": 1, "username": "Fake", "current_planet_id": 10},
                "planets": [{
                    "id": 10, "name": "Home", "type": "planet",
                    "resources": {r: int(v) for r, v in self.res.items()}, "storage": self.storage(),
                    "production_per_hour": {r: int(v) for r, v in rate.items()},
                    "energy": int(self.energy_prod() - self.energy_need()),
                    "building_queue": self.qdict(self.bq, "b"), "unit_queue": []}],
                "research": {"levels": {k: v for k, v in self.tech.items()}, "queue": self.qdict(self.rq, "r")},
                "fleet_slots": {}, "unread_messages": 0}
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
        name = body.get("machine_name")
        if path == "/planets/10/buildings":
            if name not in BASE:
                raise ApiError(422, "unknown_object", name)
            if name == "research_lab" and self.rq:
                raise ApiError(422, "research_in_progress", name)
            if self.bq:
                raise ApiError(422, "queue_full", name)
            cost = self.cost(name, self.levels[name], BASE)
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
