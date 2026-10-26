"""The network side of alliance.py: Agent methods for founding, joining and living in an alliance (mixed into Agent in agent.py).

Everything is off for a persona with no sociability, and off for good after the game answers 404 to GET /alliance (a game
without the alliance endpoints): the agent then plays exactly as before. The alliance is looked at every couple of minutes, not every
round, and the agent's own random stream is never touched (a private one is used), so the other choices of an agent do not change.

Talking to allies goes through the alliance chat (POST /chat/send with alliance=true), which every member may use. The circular
message (POST /alliance/broadcast) needs a rank with a permission that members of a new alliance do not have, so it is not used.

Log events: alliance_founded, alliance_applied, alliance_accepted (by the leader), alliance_rejected, alliance_joined, alliance_left,
alliance_kicked, intel_shared, intel_used, ally_warned, and the quiet alliance_skipped (why something did not happen).
"""
from __future__ import annotations

import random

from . import alliance as A
from . import social as S
from .client import ApiError

NO_ENDPOINT = ("http_error", "not_found")   # a 404 that is not one of our own errors: the route does not exist


class AllianceMixin:
    _alli_random = None

    # ---- small helpers -------------------------------------------------------------------------------
    def _alli_rng(self) -> random.Random:
        if self._alli_random is None:
            self._alli_random = random.Random(f"{self.name}:alliance")
        return self._alli_random

    def _ally_ids(self) -> set[int]:
        return {int(x) for x in self.memory.alliance.get("allies", [])}

    def _without_allies(self, ids: set[int] | None) -> set[int] | None:
        """The target ids an agent may attack, minus its allies. None (anyone) stays None while there are no allies."""
        allies = self._ally_ids()
        if not allies:
            return ids
        if ids is None:
            ids = {int(p["user_id"]) for p in (self._players[1] if self._players else [])}
        return ids - allies

    def _alli_skipped(self, why: str, **extra) -> None:
        if self._logi_note(("alliance_skipped", why[:40])):
            self.log("alliance_skipped", why=why[:140], **extra)

    def _alli_rows(self) -> dict[int, dict]:
        """Highscore rows by user id (cached by _get_players)."""
        players, _ = self._get_players(self.now)
        return {int(p["user_id"]): p for p in players}

    @staticmethod
    def _alli_coord(row: dict | None):
        if not row:
            return None
        c = row.get("main_planet") or {}
        try:
            return (int(c["galaxy"]), int(c["system"]), int(c["position"]))
        except (KeyError, TypeError, ValueError):
            return None

    # ---- entry point (called once per round) ---------------------------------------------------------
    def _plan_alliance(self, status: dict, planets: list[dict], threats: list) -> list[float]:
        """Warn allies of attacks now; every few minutes look at the alliance and act. Never raises except for 401/429."""
        st = self.memory.alliance
        if st["off"] or self.persona.sociability <= 0 or not planets:
            return []
        try:
            if not st["born"]:
                st["born"] = self.now
            home = next((p for p in planets if p["id"] == status["player"].get("current_planet_id")), planets[0])
            c = home["coordinates"]
            coord = (int(c["galaxy"]), int(c["system"]), int(c["position"]))
            if st["id"] is not None:
                self._alli_warn(threats, coord)
            if self.now < st["next_at"]:
                return []
            st["next_at"] = self.now + A.CHECK_EVERY
            mine = self.client.get("/alliance").get("alliance")
            if mine is None:
                self._alli_free(coord)
            else:
                self._alli_member(mine, coord)
        except ApiError as e:
            if e.status in (401, 429):
                raise
            if e.status == 404 and e.code in NO_ENDPOINT and (not e.request_line or e.request_line.startswith("GET /alliance")):
                st["off"] = True
                self.log("alliance_skipped", why="this game has no alliance endpoints")
                return []
            st["next_at"] = self.now + 300
            self.stats["refused"] += 1
            self._alli_skipped(e.message, code=e.code)
        except Exception as e:   # a bug on the alliance side must never cost the agent its round
            if self._logi_note(("alliance_error", type(e).__name__)):
                self.log("alliance_error", type=type(e).__name__, msg=str(e)[:160])
        return []

    # ---- no alliance: found one or apply -------------------------------------------------------------
    def _alli_free(self, coord) -> None:
        st = self.memory.alliance
        if st["id"] is not None:   # we were in one: kicked out, or it was disbanded
            self.log("alliance_left", alliance=st["tag"], reason="kicked or disbanded")
            st["applied"][str(st["id"])] = self.now
            st["cooldown_until"] = self.now + 3600
        st.update(id=None, tag=None, founder=False, allies=[], ally_coords={}, cursor=0)
        age = self.now - st["born"]
        if age < A.JOIN_MIN_AGE or self.now < st["cooldown_until"] or self.now < st["apply_until"]:
            return
        data = self.client.get("/alliances?per_page=100")
        alliances = data.get("alliances") or []
        rows = self._alli_rows()
        personas = self.store.agent_personas()
        me_persona = self.persona.name

        candidates = []
        for a in alliances:
            if not a.get("open") or int(a.get("member_count", 0)) >= A.MAX_MEMBERS:
                continue
            if self.now - float(st["applied"].get(str(a["id"]), -1e18)) < A.REAPPLY_AFTER:
                continue
            candidates.append(a)
        candidates.sort(key=lambda a: -int(a.get("member_count", 0)))
        detailed = []
        for a in candidates[:4]:
            info = self.client.get(f"/alliances/{a['id']}")["alliance"]
            ids = [int(m["user_id"]) for m in info.get("members", [])]
            detailed.append({"id": a["id"], "tag": a["tag"], "open": True, "member_count": len(ids),
                             "coords": [c for c in (self._alli_coord(rows.get(u)) for u in ids) if c],
                             "personas": [personas.get(u) for u in ids]})
        pick = A.pick_alliance(me_persona, coord, detailed, self.memory.opponents, self.memory.aggressors, self.now,
                               bar=A.APPLY_MIN_SCORE - 0.1 * self.persona.sociability)
        if pick is not None:
            cand, score = pick
            text = A.application_text(me_persona, self.name, coord)
            try:
                self.client.post(f"/alliances/{cand['id']}/apply", {"text": text})
            except ApiError as e:
                self._alli_refused("apply", e)
                return
            st["applied"][str(cand["id"])] = self.now
            st["apply_until"] = self.now + A.APPLY_EVERY
            self.log("alliance_applied", alliance=cand["tag"], alliance_id=cand["id"], score=score, members=cand["member_count"], text=text)
            return

        agent_ids = self.store.agent_user_ids()
        ours = [a for a in alliances if int(a.get("founder_user_id", 0)) in agent_ids] if agent_ids else alliances
        _, my_score = self._get_players(self.now)
        ok, why = A.can_found(self.persona, points=my_score, age=age, agent_count=max(1, len(agent_ids)), alliances=len(ours),
                              now=self.now, cooldown_until=st["cooldown_until"])
        if not ok or self._alli_rng().random() >= A.FOUND_CHANCE:
            st["apply_until"] = self.now + 300
            return
        tag, name = A.make_identity(me_persona, A.agent_number(self.name), [a["tag"] for a in alliances], [a["name"] for a in alliances], st["seq"])
        try:
            data = self.client.post("/alliance", {"tag": tag, "name": name})
        except ApiError as e:
            if e.code in ("tag_taken", "name_taken"):
                st["seq"] += 1
            self._alli_refused("found", e)
            return
        info = data["alliance"]
        st.update(id=int(info["id"]), tag=info["tag"], founder=True, joined_at=self.now, active_at=self.now, cursor=0)
        self.log("alliance_founded", alliance=info["tag"], name=info["name"], alliance_id=info["id"], points=my_score)
        self._alli_advertise(info["tag"], info["id"], coord, 1)

    def _alli_refused(self, what: str, e: ApiError) -> None:
        st = self.memory.alliance
        if e.status in (401, 429):
            raise e
        self.stats["refused"] += 1
        if e.code == "alliance_cooldown":
            st["cooldown_until"] = self.now + 3600
        elif e.code == "already_in_alliance":
            st["next_at"] = self.now
        else:
            st["apply_until"] = self.now + 600
        self._alli_skipped(f"{what}: {e.message}", code=e.code)

    # ---- in an alliance ------------------------------------------------------------------------------
    def _alli_member(self, mine: dict, coord) -> None:
        st = self.memory.alliance
        me = int(self.user_id or 0)
        members = mine.get("members") or []
        joined = st["id"] != mine["id"]
        if joined:
            st.update(id=int(mine["id"]), tag=mine["tag"], joined_at=self.now, active_at=self.now, cursor=0, read_at=0.0, handled=[])
            self.log("alliance_joined", alliance=mine["tag"], alliance_id=mine["id"], members=len(members))
        if len(members) != int(st.get("size", 0)):
            st["active_at"] = self.now      # somebody came or went: the alliance is alive
        st["size"] = len(members)
        st["founder"] = bool(mine.get("is_founder"))
        rows = self._alli_rows()
        st["allies"] = [int(m["user_id"]) for m in members if int(m["user_id"]) != me]
        st["ally_coords"] = {str(u): A.fmt_coord(c) for u in st["allies"] if (c := self._alli_coord(rows.get(u)))}
        self._alli_drop_ally_intel()

        if self.now >= st["read_at"]:
            st["read_at"] = self.now + A.READ_EVERY
            self._alli_read(me)
        why = A.should_leave(is_founder=st["founder"], robbed_by=st["robbed_by"], now=self.now, joined_at=st["joined_at"], active_at=st["active_at"])
        if why:
            self._alli_leave(why)
            return
        if st["founder"]:
            self._alli_lead(mine, coord, rows)
        self._alli_share(me)

    def _alli_drop_ally_intel(self) -> None:
        """Forget spy reports on planets of allies: an ally is never a target."""
        st = self.memory.alliance
        allies, coords = self._ally_ids(), {A.parse_coord(c) for c in st["ally_coords"].values()}
        for c, info in list(self.memory.intel.items()):
            if info.get("owner_id") in allies or c in coords:
                del self.memory.intel[c]

    def _alli_leave(self, why: str) -> None:
        st = self.memory.alliance
        try:
            self.client.post("/alliance/leave")
        except ApiError as e:
            self._alli_refused("leave", e)
            return
        self.log("alliance_left", alliance=st["tag"], reason=why)
        st["applied"][str(st["id"])] = self.now      # not back to the same one for a while
        st.update(id=None, tag=None, founder=False, allies=[], ally_coords={}, cursor=0, cooldown_until=self.now + 3600, robbed_by={})

    def _alli_say(self, text: str) -> bool:
        try:
            self.client.post("/chat/send", {"alliance": True, "text": text})
            return True
        except ApiError as e:
            if e.status in (401, 429):
                raise
            self._alli_skipped(f"chat: {e.message}", code=e.code)
            return False

    # ---- allies' news --------------------------------------------------------------------------------
    def _alli_read(self, me: int) -> None:
        st = self.memory.alliance
        since = f"&since_id={int(st['cursor'])}" if st["cursor"] else ""    # the game wants since_id >= 1
        data = self.client.get(f"/chat?alliance=1&limit=50{since}")
        messages = data.get("messages") or []
        if not messages:
            return
        st["cursor"] = max(int(st["cursor"]), max(int(m["id"]) for m in messages))
        if any(m.get("from_user_id") != me for m in messages):
            st["active_at"] = self.now
        for w in A.warnings_from_chat(messages, me):
            st["attackers"][A.fmt_coord(w["FROM"])] = self.now
            if w["FROM"] not in self.memory.aggressors:
                self.memory.aggressors[w["FROM"]] = S.guard_stamp(self.now)    # an ally was hit: that raider may come for us next
        if len(st["attackers"]) > 100:
            st["attackers"] = dict(sorted(st["attackers"].items(), key=lambda kv: kv[1])[-50:])
        if self.persona.aggression <= 0:
            return
        own = {A.parse_coord(c) for c in st["ally_coords"].values()}
        for c, info in A.intel_from_chat(messages, me, self.now, self._ally_ids(), self.persona.min_loot):
            if c in own:
                continue
            old = self.memory.intel.get(c)
            if old and float(old.get("at", 0)) >= info["at"]:
                continue
            self.memory.intel[c] = info
            self.log("intel_used", at=A.fmt_coord(c), loot=info["loot"], defenders=info["defenders"], age=round(self.now - info["at"]),
                     ally=info.get("from_ally"))

    def _alli_share(self, me: int) -> None:
        st = self.memory.alliance
        if not A.may_share(st, self.now) or not st["allies"]:
            return
        pick = A.pick_intel_to_share(self.memory.intel, st["shared"], self.now, self.persona.min_loot, self._ally_ids(),
                                     {A.parse_coord(c) for c in st["ally_coords"].values()})
        if pick is None:
            return
        c, info = pick
        age = self.now - float(info["at"])
        text = A.fmt_intel(c, info.get("owner_id"), info.get("loot", 0), info.get("defenders"), age)
        st["share_at"] = self.now + A.SHARE_GAP      # set first: never hammer the chat, whatever happens
        if self._alli_say(text):
            st["shared"][A.fmt_coord(c)] = float(info["at"])
            if len(st["shared"]) > 100:
                st["shared"] = dict(sorted(st["shared"].items(), key=lambda kv: kv[1])[-50:])
            self.log("intel_shared", at=A.fmt_coord(c), loot=info.get("loot"), defenders=info.get("defenders"), age=round(age))

    def _alli_warn(self, threats: list, coord) -> None:
        """An attack is on its way to us: tell the allies (at most one warning a minute). An ally's attack makes us remember who."""
        st = self.memory.alliance
        coords = {A.parse_coord(c): int(u) for u, c in st["ally_coords"].items()}
        for t in threats:
            if t.is_attack and t.origin in coords:
                st["robbed_by"][str(coords[t.origin])] = self.now
        if not st["allies"] or not A.may_warn(st, self.now):
            return
        for t in sorted((x for x in threats if x.is_attack and x.id not in st["warned"]), key=lambda x: x.arrives_in):
            st["warn_at"] = self.now + A.WARN_GAP
            st["warned"] = (st["warned"] + [t.id])[-50:]
            if self._alli_say(A.fmt_warn(coord, t.origin, t.arrives_in)):
                self.log("ally_warned", mission=t.id, **{"from": A.fmt_coord(t.origin)}, eta=round(t.arrives_in), allies=len(st["allies"]))
            return

    # ---- leading -------------------------------------------------------------------------------------
    def _alli_lead(self, mine: dict, coord, rows: dict) -> None:
        st = self.memory.alliance
        members = mine.get("members") or []
        for uid in A.members_to_kick(True, st["robbed_by"], st["attackers"], st["ally_coords"], self.now)[:1]:
            try:
                self.client.post("/alliance/kick", {"user_id": uid})
            except ApiError as e:
                self._alli_refused("kick", e)
                break
            self.log("alliance_kicked", alliance=st["tag"], member=uid, reason="attacked an ally")
            st["robbed_by"].pop(str(uid), None)
            return
        left = len(members)
        for app in (mine.get("applications") or [])[:3]:
            if int(app["id"]) in st["handled"]:
                continue
            uid = int(app["user_id"])
            ok, why = A.decide_application(member_count=left, applicant_coord=self._alli_coord(rows.get(uid)), opponents=self.memory.opponents,
                                           aggressors=self.memory.aggressors, attackers=st["attackers"], now=self.now)
            path = f"/alliance/applications/{app['id']}/" + ("accept" if ok else "reject")
            st["handled"] = (st["handled"] + [int(app["id"])])[-50:]
            try:
                self.client.post(path)
            except ApiError as e:
                if e.status in (401, 429):
                    raise
                self._alli_skipped(f"{'accept' if ok else 'reject'}: {e.message}", code=e.code)
                continue
            if ok:
                left += 1
                self.log("alliance_accepted", alliance=st["tag"], applicant=uid, members=left, text=str(app.get("text") or "")[:80])
            else:
                self.log("alliance_rejected", alliance=st["tag"], applicant=uid, why=why)
        if mine.get("open") and left < A.MAX_MEMBERS:
            self._alli_advertise(st["tag"], st["id"], coord, left)

    def _alli_advertise(self, tag, alliance_id, coord, members: int) -> None:
        """Say on the galaxy board that the alliance takes members (off without the board; fails soft)."""
        st = self.memory.alliance
        if self.now < st["ad_at"] or not self._board_on():
            return
        st["ad_at"] = self.now + A.AD_EVERY
        near = A.fmt_coord(coord)
        try:
            self._board_post("ALLIANCE", f"{tag} takes members near {near} ({members}/{A.MAX_MEMBERS})",
                             {"tag": tag, "alliance_id": alliance_id, "at": near, "members": members})
        except Exception as e:
            if self._logi_note(("alliance_ad", type(e).__name__)):
                self.log("alliance_skipped", why=f"board: {type(e).__name__}")
