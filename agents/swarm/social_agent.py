"""The network side of social.py: Agent methods for the galaxy board (mixed into Agent in agent.py).

Everything here is skipped unless the agent was given a board (`agent.board`, set by run_swarm.py when `--board URL` is
used). A board that does not answer changes nothing: the client fails soft and the agent plays as before.

Log events: board_post, board_read, trade_offer (action: posted, answered, accepted, done, skipped, refused),
trade_sent, trade_received, trade_cheated, board_error.
"""
from __future__ import annotations

from . import logistics as L
from . import social as S
from .brain import VALUE
from .client import ApiError


class SocialMixin:
    board = None            # a BoardClient (swarm/boardclient.py); None or a NullBoard = the feature is off
    _soc_coord = None       # our home planet's coordinates, known after the first round

    def _board_on(self) -> bool:
        return bool(getattr(self.board, "enabled", False))

    # ---- one round -----------------------------------------------------------------------------------
    def _plan_social(self, status: dict, planets: list[dict]) -> list[float]:
        """Read the board now and then, react to warnings, and (traders) keep the deals going. Never raises."""
        if not self._board_on():
            return []
        try:
            home = next((p for p in planets if p["id"] == status["player"].get("current_planet_id")), planets[0])
            c = home["coordinates"]
            self._soc_coord = (int(c["galaxy"]), int(c["system"]), int(c["position"]))
            st = self.memory.social
            if self.now >= st["read_at"]:
                self._social_read(home)
            if self.persona.trade_interest > 0:
                return self._trade_step(status, home)
        except Exception as e:   # a bug or a surprise on the social side must never cost the agent its round
            if self._logi_note(("board_error", type(e).__name__)):
                self.log("board_error", type=type(e).__name__, msg=str(e)[:160])
        return []

    def _board_post(self, kind: str, text: str, data: dict, ttl: float | None = None) -> dict | None:
        st = self.memory.social
        if not S.can_post(st, self.now):
            return None
        post = self.board.post(kind, text, data, ttl)
        if post is None:
            return None
        S.note_post(st, self.now)
        self.log("board_post", kind=kind, text=text[:120], id=post.get("id"))
        return post

    # ---- reading: warnings and revenge ---------------------------------------------------------------
    def _social_read(self, home: dict) -> None:
        st, m = self.memory.social, self.memory
        r = self.board.read(since=st["cursor"], kind=["WARNING", "REVENGE"], limit=S.READ_LIMIT)
        if not r["ok"]:
            st["read_at"] = self.now + S.DOWN_RETRY      # the board is not there: carry on as before
            return
        st["read_at"] = self.now + S.READ_EVERY
        posts = r["posts"]
        if posts:
            st["cursor"] = max(st["cursor"], max(p["id"] for p in posts))
            guard = []
            for raider in S.warned_raiders(self.persona, self._soc_coord, posts, r["now"], self.name):
                stamp = S.guard_stamp(self.now)
                if m.aggressors.get(raider, -1e18) < stamp:
                    m.aggressors[raider] = stamp
                    guard.append(S.fmt_coord(raider))
            others = [p for p in posts if p.get("agent") != self.name]
            if others:
                self.log("board_read", posts=len(others), warnings=sum(p["kind"] == "WARNING" for p in others),
                         revenge=sum(p["kind"] == "REVENGE" for p in others), guard=guard or None)
        if self.persona.trade_interest > 0:
            offers = self.board.read(since=0, kind=["OFFER", "WANT"], limit=S.READ_LIMIT)
            if offers["ok"]:
                self._trade_on_posts(home, offers["posts"], offers["now"])

    # ---- being robbed: WARNING and REVENGE ----------------------------------------------------------------
    def _social_battle(self, b: dict) -> None:
        """Called for every battle report we read. After a robbery: remember it and tell the board."""
        if not self._board_on() or b.get("role") != "defender" or self._soc_coord is None:
            return
        st = self.memory.social
        posts = S.robbery_posts(b, self.memory.aggressors, self.memory.opponents, self.now, self._soc_coord, self.user_id, st)
        if not posts:
            return
        raider = posts[0][2].get("raider_at")
        rec = self.memory.opponents.get(S.parse_coord(raider)) if raider else None
        if rec is not None:        # opponent memory (opponents.py): this planet robbed us
            rec["robbed"] = rec.get("robbed", 0) + 1
            rec["robbed_at"] = self.now
        sent = [self._board_post(kind, text, data) for kind, text, data in posts]
        if any(sent):
            st["robbery_at"] = self.now

    # ---- trading ---------------------------------------------------------------------------------------------------
    def _partner_ok(self, name: str, pid: int, at) -> bool:
        """The poster is one of OUR agents (in the accounts file) and its coordinates are really its main planet. Never a human."""
        entry = self.store.get(name) if name != self.name else None
        if not entry or entry.get("user_id") is None or int(entry["user_id"]) != int(pid):
            return False
        try:
            rows = self._get_players(self.now)[0]
        except ApiError:
            return False
        row = next((r for r in rows if r.get("user_id") == pid), None)
        mp = (row or {}).get("main_planet") or {}
        return (mp.get("galaxy"), mp.get("system"), mp.get("position")) == tuple(at)

    def _open_trades(self) -> list[dict]:
        return [t for t in self.memory.social["trades"].values() if t.get("status") == "open"]

    def _trade_on_posts(self, home: dict, posts: list[dict], board_now: float) -> None:
        """Replies to our offer become deals (we send against goods that arrive); a fair offer of someone else is answered."""
        st = self.memory.social
        stock = home["resources"]
        busy = {t["partner"] for t in self._open_trades()}
        offer = st["offer"]
        if offer and len(busy) < S.MAX_OPEN_TRADES:
            for terms in S.replies_to(posts, offer.get("id"), self.name, st["rep"], self.now, busy):
                if len(busy) >= S.MAX_OPEN_TRADES or terms["id"] in st["answered"]:
                    continue
                st["answered"].append(terms["id"])
                give, get = next(iter(offer["data"]["give"])), next(iter(offer["data"]["want"]))
                if terms["give"] != get or terms["want"] != give or not self._partner_ok(terms["agent"], terms["player_id"], terms["at"]):
                    continue
                give_v = offer["data"]["give"][give] * VALUE[give]
                want_v = offer["data"]["want"][get] * VALUE[get]
                total = min(give_v, S.deal_cap(stock, give) * VALUE[give])
                if total < S.MIN_TRADE_VALUE:
                    continue
                st["trades"][terms["agent"]] = S.new_trade(terms["agent"], terms["player_id"], terms["at"], give, get, total,
                                                           give_v / want_v, "poster", self.now, offer.get("id"), st["rep"].get(terms["agent"]))
                busy.add(terms["agent"])
                self.log("trade_offer", action="accepted", **{"with": terms["agent"]}, give={give: terms["want_amt"]}, want={get: terms["give_amt"]})
        if len(busy) < S.MAX_OPEN_TRADES and self.now >= self.alert_until and self.rng.random() < self.persona.trade_interest:
            match = S.pick_match(stock, posts, self.name, board_now, st["rep"], self.now, busy, st["answered"])
            if match is not None:
                self._trade_answer(home, match)

    def _trade_answer(self, home: dict, match: dict) -> None:
        st = self.memory.social
        terms = match["terms"]
        st["answered"] = (st["answered"] + [terms["id"]])[-S.SEEN_KEEP:]
        if not self._partner_ok(terms["agent"], terms["player_id"], terms["at"]):
            return
        reply = S.reply_post(terms, match, self._soc_coord, self.user_id, home["resources"])
        if self._board_post(reply["kind"], reply["text"], reply["data"]) is None:
            return          # the poster must know we are coming: no public answer, no deal
        st["trades"][terms["agent"]] = S.new_trade(terms["agent"], terms["player_id"], terms["at"], terms["want"], terms["give"],
                                                   match["total_value"], match["r"], "starter", self.now, terms["id"], st["rep"].get(terms["agent"]))
        self.log("trade_offer", action="answered", **{"with": terms["agent"]}, give=reply["data"]["give"], want=reply["data"]["want"])

    def _trade_step(self, status: dict, home: dict) -> list[float]:
        """Every round: read the inbox, judge the deals, send the next instalments, keep an offer on the board."""
        st = self.memory.social
        trades = st["trades"]
        if trades:
            if self.now - st["mail_at"] >= S.MESSAGE_EVERY:
                st["mail_at"] = self.now
                self._trade_inbox()
            self._trade_judge()
        for t in sorted(self._open_trades(), key=lambda t: t["opened"]):
            if self.now >= self.alert_until and not self.memory.away:
                self._trade_send(status, home, t)
        self._trade_offer(home)
        return [90.0] if self._open_trades() else []

    def _trade_offer(self, home: dict) -> None:
        st = self.memory.social
        if len(self._open_trades()) >= S.MAX_OPEN_TRADES or self.now < self.alert_until:
            return
        if st["offer"] and self.now - st["offer_at"] < S.OFFER_REFRESH:
            return
        new = S.make_offer(home["resources"], self._soc_coord, self.user_id)
        if new is None:
            st["offer"] = None
            return
        post = self._board_post(new["kind"], new["text"], new["data"], ttl=3600)
        if post is None:
            return
        st["offer"], st["offer_at"] = {"id": post["id"], "data": new["data"]}, self.now
        self.log("trade_offer", action="posted", give=new["data"]["give"], want=new["data"]["want"], post=post["id"])

    def _trade_inbox(self) -> None:
        trades = self.memory.social["trades"]
        data = self.client.get("/messages?category=fleet_other&unread_only=1&per_page=20")
        for row in reversed(data["messages"]):        # oldest first
            if row.get("key") != "transport_received":
                continue
            came_from, amounts = S.parse_received(row.get("body", ""))
            t = S.trade_for_delivery(trades, came_from, amounts)
            if t is None:
                continue          # not (yet) part of a deal: leave it unread, the deal may only be set up in a moment
            self.client.post(f"/messages/{row['id']}/read")
            got = S.after_receiving(t, amounts, self.now)
            self.log("trade_received", partner=t["partner"], resources=amounts, value=int(got))

    def _trade_judge(self) -> None:
        st = self.memory.social
        for name, t in list(st["trades"].items()):
            verdict = S.judge(t, self.now)
            if verdict == "open":
                continue
            del st["trades"][name]
            if verdict == "cheated":
                S.record_cheat(st["rep"], t, self.now)
                self.log("trade_cheated", partner=name, owed=int(S.debt(t)), sent=int(t["sent_value"]), received=int(t["recv_value"]),
                         why="no payment in time; shunned")
            elif verdict == "done":
                S.record_done(st["rep"], t, self.now)
                self.log("trade_offer", action="done", **{"with": name}, sent=int(t["sent_value"]), received=int(t["recv_value"]),
                         instalments=t["instalments"])

    def _trade_send(self, status: dict, home: dict, t: dict) -> bool:
        if self.now < t.get("next_try", 0.0):
            return False
        slots = status.get("fleet_slots") or {}
        if slots.get("used", 0) >= slots.get("max", 1):
            return False
        stock = home["resources"]
        amount = S.next_instalment(t, stock, self._open_trades())
        if amount <= 0:
            return False
        at = S.parse_coord(t["at"])
        if not self._partner_ok(t["partner"], t["pid"], at):
            t["next_try"] = self.now + 600
            return False
        units = self._logi_units(home["id"])
        ships_here = {u["machine_name"]: int(u["amount"]) for u in units if u.get("category") != "defense" and int(u.get("amount") or 0) > 0}
        ships, capacity = L.pick_cargo(ships_here, self._logi_stats(), amount)
        if not ships:
            build = None if home.get("unit_queue") else L.choose_cargo_build(self.persona, units, stock, ships_here)
            t["next_try"] = self.now + 180
            if build is not None:
                try:
                    self.client.post(f"/planets/{home['id']}/units", {"machine_name": build[0], "amount": build[1]})
                    self.stats["units"] += build[1]
                    self.log("units", what=build[0], amount=build[1], why="cargo ships for trading")
                except ApiError as e:
                    self.stats["refused"] += 1
                    self.log("units_refused", what=build[0], code=e.code, msg=e.message[:100])
            return False
        amount = min(amount, capacity)
        target = {"coordinates": {"galaxy": at[0], "system": at[1], "position": at[2]}}
        possible, why, fuel, seconds, _ = self._logi_check(home["id"], target, ships, "transport")
        if fuel is None or not possible:
            t["next_try"] = self.now + 600
            self.log("trade_offer", action="skipped", **{"with": t["partner"]}, why=f"transport not possible: {why}")
            return False
        spare_deut = int(stock.get("deuterium", 0) or 0) - fuel
        if t["give"] == "deuterium":
            amount = min(amount, spare_deut)
        if spare_deut < 0 or amount <= 0 or not L.fuel_ok({t["give"]: amount}, fuel):
            t["next_try"] = self.now + 600
            self.log("trade_offer", action="skipped", **{"with": t["partner"]}, why=f"fuel {fuel} deuterium is too much for this cargo")
            return False
        try:
            mission = self._logi_send(home["id"], "transport", target, ships, {t["give"]: amount})
        except ApiError as e:
            self.stats["refused"] += 1
            t["next_try"] = self.now + 600
            self.log("trade_offer", action="refused", **{"with": t["partner"]}, why=f"{e.code} {e.message[:100]}")
            return False
        stock[t["give"]] = stock.get(t["give"], 0) - amount
        stock["deuterium"] = stock.get("deuterium", 0) - fuel
        flight = float(mission.get("seconds_remaining") or seconds or 0)
        S.after_sending(t, amount, self.now, flight)
        count = max(t["instalments"], round(t["total_value"] / max(1.0, t["per"])))
        self.log("trade_sent", to=t["partner"], at=t["at"], resources={t["give"]: amount}, value=int(amount * VALUE[t["give"]]),
                 instalment=f"{t['instalments']}/{count}", fuel=fuel, seconds=int(flight))
        return True
