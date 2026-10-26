"""The network side of logistics.py: Agent methods for supply trips and fleet saving (mixed into Agent in agent.py).

Log events: supply_sent, supply_skipped, fleet_saved, fleet_save_failed, fleet_stays, fleet_returned.
"""
from __future__ import annotations

from . import logistics as L
from .client import ApiError

LOOKOUT_SECONDS = 90.0   # an agent with a colony and a fleet looks at least this often: a raid can land within minutes


class LogisticsMixin:
    # ---- helpers -------------------------------------------------------------------------------------
    def _logi_stats(self) -> dict:
        if not self.ship_stats:
            self.ship_stats = {s["machine_name"]: s for s in self.client.get("/ships")["ships"]}
        return self.ship_stats

    def _logi_units(self, planet_id: int) -> list[dict]:
        return self.client.get(f"/planets/{planet_id}/units")["units"]

    def _logi_check(self, planet_id: int, target: dict, ships: dict, mission: str) -> tuple[bool, str, int | None, float, int]:
        """Dry run: (possible, why not, fuel, seconds, cargo capacity). Fuel None = the check itself failed."""
        c = target["coordinates"]
        try:
            r = self.client.post(f"/planets/{planet_id}/fleet/check", {
                "galaxy": c["galaxy"], "system": c["system"], "position": c["position"], "type": "planet", "ships": ships})
        except ApiError as e:
            return False, e.message[:100], None, 0.0, 0
        entry = (r.get("missions") or {}).get(mission) or {}
        return (bool(entry.get("possible")), str(entry.get("reason") or ""), int(r.get("deuterium_consumption") or 0),
                float(r.get("duration_seconds") or 0), int(r.get("cargo_capacity") or 0))

    def _logi_send(self, planet_id: int, mission: str, target: dict, ships: dict, resources: dict) -> dict:
        c = target["coordinates"]
        body = {"mission": mission, "galaxy": c["galaxy"], "system": c["system"], "position": c["position"],
                "type": "planet", "ships": ships}
        if resources:
            body["resources"] = resources
        return self.client.post(f"/planets/{planet_id}/fleet", body)["mission"]

    def _logi_note(self, key) -> bool:
        """True the first time `key` is seen (keeps the log free of one line per 20 seconds)."""
        seen = self.__dict__.setdefault("_logi_seen", set())
        if key in seen:
            return False
        if len(seen) > 500:
            seen.clear()
        seen.add(key)
        return True

    # ---- supply --------------------------------------------------------------------------------------
    def _plan_supply(self, status: dict, planets: list[dict]) -> list[float]:
        """Send surplus from the richest planet to a starved colony, in cargo ships we already have."""
        m = self.memory
        if len(planets) < 2 or self.now < m.supply_next_at or self.now < self.alert_until or m.away:
            return []
        slots = status.get("fleet_slots") or {}
        if slots.get("used", 0) >= slots.get("max", 1):
            return []
        route = L.pick_supply_route(planets)
        if route is None:
            return []
        src, dst = route
        amounts = L.supply_amounts(src, dst)
        if L.stock_value(amounts) < L.MIN_SEND_VALUE:
            return []
        units = self._logi_units(src["id"])
        ships_here = {u["machine_name"]: int(u["amount"]) for u in units if u.get("category") != "defense" and int(u.get("amount") or 0) > 0}
        where = {"from": src["id"], "to": dst["id"]}
        if not any(ships_here.get(n, 0) > 0 for n in L.CARGOS):
            build = None if src.get("unit_queue") else L.choose_cargo_build(self.persona, units, src["resources"], ships_here, L.SUPPLY_BUILDERS)
            if build is None:
                m.supply_next_at = self.now + 600
                self.log("supply_skipped", why="no cargo ships on the richest planet", **where)
                return []
            name, amount = build
            try:
                self.client.post(f"/planets/{src['id']}/units", {"machine_name": name, "amount": amount})
            except ApiError as e:
                self.stats["refused"] += 1
                m.supply_next_at = self.now + 600
                self.log("units_refused", what=name, code=e.code, msg=e.message[:100])
                return []
            m.supply_next_at = self.now + 60
            self.stats["units"] += amount
            self.log("units", what=name, amount=amount, why="cargo ships to supply a starved colony")
            return [60.0]
        stats = self._logi_stats()
        ships, capacity = L.pick_cargo(ships_here, stats, sum(amounts.values()))
        amounts = L.fit_to_capacity(amounts, capacity)
        possible, why, fuel, seconds, _ = self._logi_check(src["id"], dst, ships, "transport")
        if fuel is None or not possible:
            m.supply_next_at = self.now + 600
            self.log("supply_skipped", why=f"transport not possible: {why}", **where)
            return []
        have_deut = int(src["resources"].get("deuterium", 0) or 0)
        if have_deut < fuel:
            m.supply_next_at = self.now + 600
            self.log("supply_skipped", why=f"not enough deuterium for the flight ({fuel} needed)", **where)
            return []
        amounts["deuterium"] = min(amounts["deuterium"], have_deut - fuel)
        value = int(L.stock_value(amounts))
        if value < L.MIN_SEND_VALUE or not L.fuel_ok(amounts, fuel):
            m.supply_next_at = self.now + 600
            self.log("supply_skipped", why=f"fuel {fuel} deuterium for cargo worth {value}: not worth the trip", **where)
            return []
        amounts = {r: v for r, v in amounts.items() if v > 0}
        try:
            mission = self._logi_send(src["id"], "transport", dst, ships, amounts)
        except ApiError as e:
            self.stats["refused"] += 1
            m.supply_next_at = self.now + 600
            self.log("supply_skipped", why=f"refused: {e.code} {e.message[:100]}", **where)
            return []
        for r, v in amounts.items():
            src["resources"][r] = src["resources"].get(r, 0) - v
        src["resources"]["deuterium"] = src["resources"].get("deuterium", 0) - fuel
        m.supply_next_at = self.now + max(L.SUPPLY_COOLDOWN, 2 * float(mission.get("seconds_remaining") or seconds))
        self.log("supply_sent", resources=amounts, value=value, fuel=fuel, ships=ships,
                 seconds=mission.get("seconds_remaining"), **where)
        return []

    # ---- fleet saving --------------------------------------------------------------------------------
    def _save_fleet(self, status: dict, planets: list[dict], threats: list) -> list[float]:
        """Fleet at a planet that is about to be attacked and cannot win: deploy it to another own planet; bring it back later."""
        m = self.memory
        hints: list[float] = []
        coming = any(getattr(t, "is_attack", False) for t in threats)
        if not coming and not m.away:
            return [LOOKOUT_SECONDS] if len(planets) >= 2 and self.persona.army_share > 0 else []
        if len(planets) < 2:
            return []
        try:
            attacks = L.incoming_attacks(self.client.get("/fleet").get("missions", []))
        except ApiError:
            return [30.0]
        attacks += self._phalanx_attacks(planets, attacks)   # raids a sensor phalanx saw coming (moons_agent.py)
        used = [int((status.get("fleet_slots") or {}).get("used", 0))]
        maxed = int((status.get("fleet_slots") or {}).get("max", 1))
        for p in planets:
            hit = L.attacks_on(p, attacks)
            if hit:
                hints += self._flee_from(p, hit, planets, attacks, used, maxed)
        hints += self._bring_home(planets, attacks, used, maxed)
        return hints

    def _flee_from(self, p: dict, hit: list, planets: list[dict], attacks: list, used: list, maxed: int) -> list[float]:
        m = self.memory
        units = self._logi_units(p["id"])
        ships = L.mobile_ships(units)
        if L.fleet_value(ships) < L.MIN_FLEET_VALUE:
            return []
        first = min(hit, key=lambda a: a.arrives_in)
        key = first.id
        strength = (m.opponents.get(first.origin) or {}).get("strength")
        holds, sim = L.can_win(ships, L.defence_on(units), strength, self.my_tech, self.rng)
        if holds:
            if self._logi_note(("stay", key)):
                self.log("fleet_stays", planet=p["id"], why=sim)
            return []
        refuge = L.pick_refuge(p, planets, attacks, first.origin)
        if refuge is None:
            if self._logi_note(("norefuge", key)):
                self.log("fleet_save_failed", planet=p["id"], why="no other planet that is safe")
            return [LOOKOUT_SECONDS / 3]
        if used[0] >= maxed:
            if self._logi_note(("slots", key)):
                self.log("fleet_save_failed", planet=p["id"], why="no free fleet slot")
            return []
        stats = self._logi_stats()
        capacity = sum(L.cap_of(stats, n) * k for n, k in ships.items())
        possible, why, fuel, seconds, _ = self._logi_check(p["id"], refuge, ships, "deployment")
        if fuel is None or not possible:
            if self._logi_note(("impossible", key)):
                self.log("fleet_save_failed", planet=p["id"], why=f"deployment not possible: {why}")
            return []
        cargo = L.flee_cargo(p["resources"], capacity, fuel)
        try:
            try:
                mission = self._logi_send(p["id"], "deployment", refuge, ships, cargo)
            except ApiError:
                cargo = {}
                mission = self._logi_send(p["id"], "deployment", refuge, ships, cargo)   # maybe the hold or the fuel was the problem
        except ApiError as e:
            self.stats["refused"] += 1
            if self._logi_note(("refused", key)):
                self.log("fleet_save_failed", planet=p["id"], why=f"refused: {e.code} {e.message[:100]}")
            return []
        used[0] += 1
        until = L.danger_until(hit, self.now)
        entry = next((a for a in m.away if a.get("at_id") == p["id"]), None)
        if entry is None:
            entry = {"home_id": p["id"], "home_coord": p["coordinates"], "tries": 0}
            m.away.append(entry)
        entry.update(at_id=refuge["id"], at_coord=refuge["coordinates"], ships=ships, resources=cargo, sent_at=self.now,
                     danger_until=max(until, float(entry.get("danger_until", 0))))
        for r, v in cargo.items():
            p["resources"][r] = p["resources"].get(r, 0) - v
        self.log("fleet_saved", planet=p["id"], to=refuge["id"], ships=ships, resources=cargo, value=L.fleet_value(ships),
                 attacker="%d:%d:%d" % first.origin, arrives_in=round(first.arrives_in), why=sim,
                 seconds=mission.get("seconds_remaining"))
        return [LOOKOUT_SECONDS / 3]

    def _bring_home(self, planets: list[dict], attacks: list, used: list, maxed: int) -> list[float]:
        m = self.memory
        hints: list[float] = []
        by_id = {p["id"]: p for p in planets}
        for entry in list(m.away):
            home, at = by_id.get(entry.get("home_id")), by_id.get(entry.get("at_id"))
            if home is None or at is None or L.lost_track(entry, self.now):
                m.away.remove(entry)
                self.log("fleet_returned", ok=False, why="lost track of the fleet or one of the planets", home=entry.get("home_id"))
                continue
            if not L.ready_to_return(entry, self.now, attacks, planets):
                hints.append(max(20.0, min(60.0, float(entry.get("danger_until", 0)) - self.now)))
                continue
            present = L.mobile_ships(self._logi_units(at["id"]))
            ships = {n: min(present.get(n, 0), int(k)) for n, k in (entry.get("ships") or {}).items() if present.get(n, 0) > 0}
            if not ships:
                hints.append(30.0)    # not there yet (the deployment is still flying)
                continue
            if used[0] >= maxed:
                hints.append(30.0)
                continue
            possible, why, fuel, seconds, _ = self._logi_check(at["id"], home, ships, "deployment")
            if fuel is None or not possible:
                entry["tries"] = int(entry.get("tries", 0)) + 1
                hints.append(60.0)
                if entry["tries"] >= 10:
                    m.away.remove(entry)
                    self.log("fleet_returned", ok=False, why=f"cannot send it home: {why}", home=home["id"])
                continue
            stats = self._logi_stats()
            capacity = sum(L.cap_of(stats, n) * k for n, k in ships.items())
            bring = {r: min(int(v), int(at["resources"].get(r, 0) or 0)) for r, v in (entry.get("resources") or {}).items()}
            cargo = L.flee_cargo(bring, capacity, fuel)
            try:
                try:
                    mission = self._logi_send(at["id"], "deployment", home, ships, cargo)
                except ApiError:
                    cargo = {}
                    mission = self._logi_send(at["id"], "deployment", home, ships, cargo)
            except ApiError as e:
                entry["tries"] = int(entry.get("tries", 0)) + 1
                hints.append(60.0)
                if entry["tries"] >= 10:
                    m.away.remove(entry)
                    self.log("fleet_returned", ok=False, why=f"refused: {e.code} {e.message[:100]}", home=home["id"])
                continue
            used[0] += 1
            m.away.remove(entry)
            self.log("fleet_returned", ok=True, ships=ships, resources=cargo, home=home["id"], back_from=at["id"],
                     seconds=mission.get("seconds_remaining"))
        return hints
