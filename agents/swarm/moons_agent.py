"""The network side of moons.py: Agent methods for building a sensor phalanx and scanning with it (mixed into Agent in agent.py).

Needs the helpers of LogisticsMixin (_logi_units, _logi_stats, _logi_note). Does nothing for an agent without a moon.
The jump gate is not used by agents (the API has it, see docs/API.md).

Log events: moon_seen, phalanx_built, phalanx_scan, phalanx_warning, and the quiet ones: phalanx_skipped (why a scan or a
delivery did not happen), moon_fed (resources carried to the moon). A moon building order is logged as an ordinary `build`.
"""
from __future__ import annotations

from . import logistics as L
from . import moons as M
from .client import ApiError
from .defense import ALERT_SECONDS, Threat

REFUSED_SCAN = ("out_of_range", "no_planet_at_target", "target_is_moon", "target_is_admin", "target_is_own")


def _coord(p: dict) -> tuple[int, int, int]:
    return M.coord_from(p.get("coordinates") or {})


class MoonsMixin:
    # ---- entry point (called once per round) ---------------------------------------------------------
    def _plan_moons(self, status: dict, planets: list[dict]) -> list[float]:
        """Per moon: build the lunar base / sensor phalanx, carry in what it lacks, scan neighbours."""
        st = self.memory.moons
        M.prune(st, self.now)
        moons = [p for p in status["planets"] if p.get("type") == "moon"]
        if not moons or st.get("off") or not planets:
            return []
        hints: list[float] = []
        for moon in moons[:3]:
            if moon["id"] not in st["seen"]:
                st["seen"].append(moon["id"])
                self.log("moon_seen", moon=moon["id"], at="%d:%d:%d" % _coord(moon), fields=moon.get("fields"), resources=moon.get("resources"))
            try:
                hints += self._moon_build(status, planets, moon)
                hints += self._moon_scan(status, planets, moon)
            except ApiError as e:
                if e.status in (401, 429):
                    raise
                if e.status in (404, 405) and e.code == "http_error":   # a game without the moon endpoints: never ask again
                    st["off"] = True
                    self.log("phalanx_skipped", moon=moon["id"], why="this game has no moon endpoints")
                    return hints
                st["scan_next"] = self.now + 600      # never let a moon problem (even a server error) break the rest of the round
                st["build_next"][str(moon["id"])] = self.now + 600
                self.stats["refused"] += 1
                self.log("phalanx_skipped", moon=moon["id"], code=e.code, why=e.message[:120])
        return hints

    # ---- building ------------------------------------------------------------------------------------
    def _moon_build(self, status: dict, planets: list[dict], moon: dict) -> list[float]:
        st, mid = self.memory.moons, str(moon["id"])
        active = [q["seconds_remaining"] for q in (moon.get("building_queue") or []) if q.get("active")]
        if moon.get("building_queue"):
            return active[:1]
        if self.now < float(st["build_next"].get(mid, 0.0)):
            return []
        data = self.client.get(f"/planets/{moon['id']}/buildings")
        level = next((int(b["level"]) for b in data["buildings"] if b["machine_name"] == "sensor_phalanx"), 0)
        known = st["levels"].get(mid)
        if known is not None and level > int(known):
            self.log("phalanx_built", moon=moon["id"], level=level, range=M.scan_range(level))
        st["levels"][mid] = level
        richest = max((M.stock_value(p["resources"]) for p in planets), default=0.0)
        item = None if data.get("queue_full") else M.choose_moon_build(self.persona, data["buildings"], richest)
        if item is None:
            st["build_next"][mid] = self.now + 600
            return []
        what = item["machine_name"]
        why = f"sensor phalanx level {M.wanted_level(self.persona, richest)} wanted" + ("" if what == "sensor_phalanx" else f": lunar base first")
        if not item.get("can_afford"):
            st["build_next"][mid] = self.now + 120
            self._moon_feed(status, planets, moon, M.shortage(item, moon["resources"]), f"for {what} level {item['next_level']}")
            return [120.0]
        try:
            result = self.client.post(f"/planets/{moon['id']}/buildings", {"machine_name": what})
        except ApiError as e:
            if e.status in (401, 429) or e.status >= 500:
                raise
            self.stats["refused"] += 1
            st["build_next"][mid] = self.now + 600
            self.log("refused", kind="build", what=what, code=e.code, msg=e.message)
            return []
        started = next((q for q in result["queue"] if q["machine_name"] == what), None)
        self.stats["builds"] += 1
        st["build_next"][mid] = self.now + 30
        self.log("build", planet=moon["id"], what=what, level=item["next_level"], why=why, seconds=started["duration_seconds"] if started else None)
        return [started["duration_seconds"]] if started and started.get("active") else []

    def _moon_feed(self, status: dict, planets: list[dict], moon: dict, need: dict, why: str) -> None:
        """Carry what the moon lacks from the planet it belongs to (a moon produces nothing) in cargo ships we already have."""
        st, mid = self.memory.moons, str(moon["id"])
        if not need or self.now < float(st["feed_until"].get(mid, 0.0)):
            return
        slots = status.get("fleet_slots") or {}
        if slots.get("used", 0) >= slots.get("max", 1):
            return

        def skip(reason: str) -> None:
            st["feed_until"][mid] = self.now + 600
            if self._logi_note(("moonskip", mid, reason[:40])):
                self.log("phalanx_skipped", moon=moon["id"], why=f"cannot bring {why}: {reason}")

        src = next((p for p in planets if _coord(p) == _coord(moon)), None) or max(planets, key=lambda p: M.stock_value(p["resources"]))
        ships_here = L.mobile_ships(self._logi_units(src["id"]))
        if not any(ships_here.get(n, 0) > 0 for n in L.CARGOS):
            return skip("no cargo ships")
        ships, capacity = L.pick_cargo(ships_here, self._logi_stats(), sum(need.values()))
        g, s, p = _coord(moon)
        body = {"mission": "transport", "galaxy": g, "system": s, "position": p, "type": "moon", "ships": ships}
        try:
            check = self.client.post(f"/planets/{src['id']}/fleet/check", body)
        except ApiError as e:
            if e.status in (401, 429) or e.status >= 500:
                raise
            return skip(e.message[:80])
        entry = (check.get("missions") or {}).get("transport") or {}
        if not entry.get("possible"):
            return skip(str(entry.get("reason") or "transport not possible")[:80])
        fuel = int(check.get("deuterium_consumption") or 0)
        amounts = M.feed_amounts(src["resources"], need, fuel, int(check.get("cargo_capacity") or capacity))
        if amounts is None:
            return skip("the planet cannot spare it or the ships cannot carry it")
        try:
            mission = self.client.post(f"/planets/{src['id']}/fleet", {**body, "resources": amounts})["mission"]
        except ApiError as e:
            if e.status in (401, 429) or e.status >= 500:
                raise
            return skip(f"refused: {e.code}")
        for r, v in amounts.items():
            src["resources"][r] = src["resources"].get(r, 0) - v
        src["resources"]["deuterium"] = src["resources"].get("deuterium", 0) - fuel
        seconds = float(mission.get("seconds_remaining") or 60)
        st["feed_until"][mid] = self.now + max(M.FEED_COOLDOWN, 2 * seconds + 30)
        self.log("moon_fed", moon=moon["id"], source=src["id"], resources=amounts, fuel=fuel, seconds=seconds, why=why)

    # ---- scanning ------------------------------------------------------------------------------------
    def _moon_scan(self, status: dict, planets: list[dict], moon: dict) -> list[float]:
        st, mid = self.memory.moons, str(moon["id"])
        level = int(st["levels"].get(mid, 0) or 0)
        if level < 1 or moon.get("building_queue"):
            return []
        alert = self.now < self.alert_until or self._on_guard()
        deuterium = float((moon.get("resources") or {}).get("deuterium", 0) or 0)
        ok, _ = M.may_scan(self.persona, st, self.now, max(deuterium, M.SCAN_COST), alert)   # due and within the hourly budget?
        if not ok:
            return []
        if deuterium < M.SCAN_COST:
            if M.is_military(self.persona) or alert:       # a peaceful agent pays for scans only when it has a reason
                self._moon_feed(status, planets, moon, M.scan_shortage(moon["resources"]), "deuterium for phalanx scans")
            return []
        own = {_coord(p) for p in status["planets"]}
        pick = M.pick_scan_target(self.persona, st, self.now, _coord(moon), level, own, self.memory.aggressors, self._scan_targets(),
                                  self._scan_players(), 1.2 if self.persona.character_class == "discoverer" else 1.0)
        if pick is None:
            st["scan_next"] = self.now + 300
            return []
        target, why = pick
        try:
            data = self.client.post(f"/planets/{moon['id']}/phalanx", {"galaxy": target[0], "system": target[1], "position": target[2]})
        except ApiError as e:
            if e.code in REFUSED_SCAN:
                M.mark_nogo(st, target, self.now)
                st["scan_next"] = self.now + 5
                if self._logi_note(("scanrefused", target, e.code)):
                    self.log("phalanx_skipped", moon=moon["id"], at="%d:%d:%d" % target, code=e.code, why=e.message[:100])
                return []
            if e.code == "not_enough_deuterium":
                st["scan_next"] = self.now + 300
                return []
            if e.code in ("no_sensor_phalanx", "not_a_moon"):
                st["levels"][mid] = 0
                st["build_next"][mid] = 0.0
                return []
            raise
        fleets = data.get("fleets") or []
        M.record_scan(st, self.now, target, alert)
        warnings, away = M.read_scan(fleets, own, target, self.now)
        fresh = M.remember_scan(st, self.now, target, warnings, away)
        moon["resources"]["deuterium"] = float(data.get("deuterium_left", deuterium - M.SCAN_COST))
        self.log("phalanx_scan", moon=moon["id"], at="%d:%d:%d" % target, why=why, player=(data.get("target") or {}).get("player_name"),
                 fleets=len(fleets), hostile=len(warnings), away=bool(away), cost=data.get("scan_cost", M.SCAN_COST), left=data.get("deuterium_left"))
        for w in fresh:
            self.log("phalanx_warning", moon=moon["id"], attacker="%d:%d:%d" % w["origin"], planet="%d:%d:%d" % w["to"], ships=w["ships"],
                     lands_in=round(w["lands"] - self.now), owner=w.get("owner_id"))
        return [M.scan_gap(alert)]

    def _scan_targets(self) -> dict:
        """Planets a raider might hit soon, with what they would give: fresh spy reports and known opponents."""
        out = {c: float(i.get("loot", 0) or 0) for c, i in self.memory.intel.items()}
        for c, rec in self.memory.opponents.items():
            out.setdefault(c, float(rec.get("loot", 0) or 0))
        return out

    def _scan_players(self) -> list[dict]:
        try:
            return self._get_players(self.now)[0]
        except ApiError:
            return []

    # ---- what the scans found, fed into the existing defence ----------------------------------------
    def _phalanx_threats(self, real: list) -> list:
        """Attacks the phalanx saw coming, as Threats for the defence orders (not the ones /fleet already shows)."""
        out = []
        for w in M.active_warnings(self.memory.moons, self.now):
            if any(t.origin == w["origin"] and abs(t.arrives_in - w["arrives_in"]) < 60 for t in real if t.is_attack):
                continue
            out.append(Threat(-w["id"], "attack", w["arrives_in"], w["origin"]))
            self.memory.aggressors[w["origin"]] = self.now
        if out:
            self.alert_until = max(self.alert_until, self.now + ALERT_SECONDS)
        return out

    def _phalanx_attacks(self, planets: list[dict], real: list) -> list:
        """The same warnings as Attack objects for fleet saving, aimed at our planet on those coordinates."""
        out = []
        for w in M.active_warnings(self.memory.moons, self.now):
            if any(a.origin == w["origin"] and abs(a.arrives_in - w["arrives_in"]) < 60 for a in real):
                continue
            planet = next((p for p in planets if _coord(p) == w["to"]), None)
            out.append(L.Attack(-w["id"], planet["id"] if planet else None, w["to"], w["origin"], w["arrives_in"]))
        return out
