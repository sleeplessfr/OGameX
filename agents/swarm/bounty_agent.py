"""The network side of bounty.py: Agent methods for revenge joins and bounties (mixed into Agent in agent.py).

Everything here is skipped unless the agent has a board (`--board`). Called from social_agent.py (`_social_read`,
`_plan_social`, `_social_battle`) and from two lines in agent.py (`_bounty_battle`, `_bounty_focus`).

Log events: revenge_join, revenge_done, bounty_post, bounty_claim, bounty_verdict, bounty_paid, bounty_unpaid.
`bounty_paid` and `bounty_unpaid` carry `side` (poster / claimant): the poster logs that it sent the reward or gave up,
the claimant that the reward arrived or never came (and shuns the poster).
"""
from __future__ import annotations

import random as _random
import time
from types import SimpleNamespace

from . import bounty as B
from . import logistics as L
from . import social as S
from .brain import VALUE
from .client import ApiError
from .combat import unit_value
from .military import FIGHTERS, INTEL_MAX_AGE, Intel, choose_strike

_PAYER = SimpleNamespace(name="trader")    # lets logistics.choose_cargo_build order a few small cargos whatever the persona: a debt is a debt


class BountyMixin:
    _soc_stock = None       # our home planet's stock as of this round (a BOUNTY reward is a share of it)

    def _wall(self) -> float:
        """The computer's clock: shared by the board, the agents' logs and the referee (tests may replace it)."""
        return time.time()

    def _bounty_guard(self, what: str, fn, *args):
        """A bug on the bounty side must never cost the agent its round."""
        try:
            return fn(*args)
        except Exception as e:
            if self._logi_note(("board_error", what, type(e).__name__)):
                self.log("board_error", type=type(e).__name__, msg=f"{what}: {str(e)[:150]}")
            return None

    def _friend_ids(self) -> set[int]:
        ids = set()
        for name in B.friend_names(self.memory.social):
            entry = self.store.get(name) if name != self.name else None
            if entry and entry.get("user_id") is not None:
                ids.add(int(entry["user_id"]))
        return ids

    # ---- the target list ---------------------------------------------------------------------------------
    def _bounty_focus(self) -> dict | None:
        """Who this agent prefers to spy on and attack right now (a joined revenge, running bounties), or None."""
        if not self._board_on():
            return None
        return self._bounty_guard("focus", lambda: B.build_focus(
            self.memory.social, self.persona, self.now, self._wall(), self.user_id, self._friend_ids(), self._allowed_target_ids()))

    # ---- every round (called from _plan_social) ------------------------------------------------------------
    def _plan_bounty(self, status: dict, home: dict) -> list[float]:
        st = self.memory.social
        self._soc_stock = dict(home["resources"])
        hints: list[float] = []
        if st["pay"]:
            self._bounty_pay(status, home)
            if st["pay"]:
                hints.append(120.0)
        if st["owed"]:
            self._bounty_inbox()
            if st["owed"]:
                hints.append(300.0)
        for bid, c in list(st["claims"].items()):         # a claim nobody judged
            if self.now - float(c.get("at") or 0) > B.CLAIM_WAIT:
                del st["claims"][bid]
        return hints

    # ---- reading the board (called from _social_read with the new posts) ------------------------------------------
    def _bounty_on_posts(self, home: dict, posts: list[dict], board_now: float) -> None:
        st = self.memory.social
        others = [p for p in posts if p.get("agent") != self.name]
        for post in others:
            v = B.parse_verdict(post)
            if v is not None:
                self._bounty_verdict(v)
        if B.is_hunter(self.persona):
            B.remember_bounties(st, others, self.name, self._wall(), self.now)    # the target list picks them up in _bounty_focus
            if not B.revenge_active(st, self.now):
                self._revenge_join(home, others, board_now)

    # ---- revenge --------------------------------------------------------------------------------------------------
    def _my_strength(self, units: list[dict]) -> float:
        ships = L.mobile_ships(units)
        return float(sum(unit_value(n) * k for n, k in ships.items() if n in FIGHTERS))

    def _revenge_join(self, home: dict, posts: list[dict], board_now: float) -> None:
        st, m = self.memory.social, self.memory
        if not any(p.get("kind") == "REVENGE" for p in posts):
            return
        units = self._logi_units(home["id"])
        call = B.pick_revenge(self.persona, posts, board_now, self.name, self._soc_coord, self.user_id, self._my_strength(units),
                              m.opponents, self._friend_ids(), self._allowed_target_ids())
        if call is None:
            return
        # If a fresh spy report shows what defends the raider, the fight simulation has the last word (same rules as an attack).
        info = m.intel.get(call["raider"])
        if info and self.now - info["at"] <= INTEL_MAX_AGE and info.get("defense") is not None:
            ships_here = L.mobile_ships(units)
            intel = Intel(info["loot"], info["defenders"], info.get("owner_id"), info.get("ships"), info.get("defense"), info.get("research"))
            rng = _random.Random(f"{self.name}:revenge:{call['id']}")
            _, go, why, _ = choose_strike(self.persona, rng, ships_here, self._logi_stats(), intel, self.my_tech, m.risk_scale)
            if not go:
                st["bounty_done"] = (st["bounty_done"] + [call["id"]])[-S.SEEN_KEEP:]
                return
        st["revenge"] = B.revenge_state(call, self.now)
        B.add_friend(st, call["agent"])
        st["bounty_done"] = (st["bounty_done"] + [call["id"]])[-S.SEEN_KEEP:]
        reply = B.revenge_reply(call, self._soc_coord)
        sent = self._board_post(reply["kind"], reply["text"], reply["data"])
        self.log("revenge_join", post=call["id"], victim=call["agent"], raider_at=S.fmt_coord(call["raider"]), raider_id=call["raider_id"],
                 until=int(st["revenge"]["until"]), replied=bool(sent))

    # ---- being robbed: a bounty (called from _social_battle once the WARNING / REVENGE posts went out) --------------------
    def _bounty_after_robbery(self, b: dict, robbery: dict) -> None:
        st = self.memory.social
        wall = self._wall()
        if B.open_bounty(st, wall):
            return
        offer = B.bounty_offer(int(b.get("loot") or 0), robbery, getattr(self, "_soc_stock", None), self._soc_coord, self.user_id, wall)
        if offer is None:
            return
        post = self._board_post(offer["kind"], offer["text"], offer["data"], ttl=offer["ttl"])
        if post is None:
            return
        d = offer["data"]
        st["bounty_out"][str(post["id"])] = {"id": post["id"], "target_at": d["target_at"], "pid": d["target_player_id"],
                                              "reward": d["reward"], "expires": d["expires"], "posted": self.now, "paid": False}
        for key in sorted(st["bounty_out"], key=lambda k: st["bounty_out"][k].get("posted", 0))[:-5]:
            del st["bounty_out"][key]
        self.log("bounty_post", bounty=post["id"], target=d["target_at"], target_id=d["target_player_id"], reward=d["reward"],
                 value=int(S.value_of(d["reward"])), expires=d["expires"])

    # ---- attacking: revenge ends, claims go out (called from agent._read_battles after the battle is logged) -----------------
    def _bounty_battle(self, b: dict) -> None:
        if not self._board_on() or b.get("role") != "attacker" or b.get("probes_only") or self._soc_coord is None:
            return
        self._bounty_guard("claim", self._bounty_battle_inner, b)

    def _bounty_battle_inner(self, b: dict) -> None:
        st = self.memory.social
        r = B.revenge_active(st, self.now)
        if r and (b.get("their_id") == r.get("pid") or b.get("at") == r.get("coord")):
            st["revenge"] = None
            self.log("revenge_done", raider_at=r.get("coord"), loot=b.get("loot"), their_loss=b.get("their_loss"), my_loss=b.get("my_loss"))
        if not B.is_hunter(self.persona):
            return
        for bounty, post in B.claims_for(b, st, self.name, self.user_id, self._soc_coord, self._wall(), self.now):
            sent = self._board_post(post["kind"], post["text"], post["data"])
            if sent is None:
                continue
            st["claims"][str(bounty["id"])] = {"bounty": bounty["id"], "claim": sent["id"], "poster": bounty["poster"],
                                                "poster_id": bounty["poster_id"], "poster_at": bounty["poster_at"],
                                                "reward": bounty["reward"], "at": self.now}
            self.log("bounty_claim", bounty=bounty["id"], claim=sent["id"], poster=bounty["poster"], at=post["data"]["at"],
                     loot=post["data"]["loot"], report=post["data"]["report"], reward=bounty["reward"])

    # ---- a verdict ---------------------------------------------------------------------------------------------------------
    def _bounty_verdict(self, v: dict) -> None:
        st = self.memory.social
        if v["id"] in st["bounty_done"]:
            return
        mine = next((c for c in st["claims"].values() if c.get("claim") == v["claim"]), None)
        own = st["bounty_out"].get(str(v["bounty"]))
        if mine is not None:
            st["bounty_done"] = (st["bounty_done"] + [v["id"]])[-S.SEEN_KEEP:]
            del st["claims"][str(mine["bounty"])]
            self.log("bounty_verdict", bounty=v["bounty"], claim=v["claim"], ok=v["ok"], why=v["why"], role="claimant", poster=mine["poster"])
            if v["ok"]:
                st["owed"][str(mine["bounty"])] = {"bounty": mine["bounty"], "poster": mine["poster"], "poster_at": mine["poster_at"],
                                                    "reward": mine["reward"], "deadline": self.now + B.PAY_WAIT, "since": self.now}
        elif own is not None and v["claimant"] != self.name:
            st["bounty_done"] = (st["bounty_done"] + [v["id"]])[-S.SEEN_KEEP:]
            self.log("bounty_verdict", bounty=v["bounty"], claim=v["claim"], ok=v["ok"], why=v["why"], role="poster", claimant=v["claimant"])
            if v["ok"] and not own.get("paid") and str(v["bounty"]) not in st["pay"] and v["claimant_id"] is not None:
                st["pay"][str(v["bounty"])] = {"bounty": v["bounty"], "claimant": v["claimant"], "pid": v["claimant_id"],
                                                "at": v["claimant_at"], "claim": v["claim"], "since": self.now, "next_try": 0.0}

    # ---- paying (the poster) -----------------------------------------------------------------------------------------------
    def _bounty_pay(self, status: dict, home: dict) -> None:
        st = self.memory.social
        for bid, p in sorted(st["pay"].items(), key=lambda kv: kv[1]["since"]):
            if self.now - p["since"] > B.PAY_WINDOW:
                del st["pay"][bid]
                self.log("bounty_unpaid", side="poster", bounty=p["bounty"], claimant=p["claimant"],
                         why=p.get("why") or "could not send the reward in time")
                continue
            if self.now < p["next_try"] or self.now < self.alert_until or self.memory.away:
                continue
            own = st["bounty_out"].get(bid)
            if own is None or own.get("paid"):
                del st["pay"][bid]
                continue
            slots = status.get("fleet_slots") or {}
            if slots.get("used", 0) >= slots.get("max", 1):
                continue
            if self._bounty_send(home, bid, p, own):
                del st["pay"][bid]

    def _bounty_fail(self, p: dict, why: str, wait: float = 600.0) -> bool:
        p["next_try"], p["why"] = self.now + wait, why
        return False

    def _bounty_send(self, home: dict, bid: str, p: dict, own: dict) -> bool:
        at = S.parse_coord(p["at"])
        if at is None or not self._partner_ok(p["claimant"], p["pid"], at):
            return self._bounty_fail(p, "the claimant is not one of our agents at those coordinates")
        stock = home["resources"]
        reward = own["reward"]
        units = self._logi_units(home["id"])
        ships_here = {u["machine_name"]: int(u["amount"]) for u in units if u.get("category") != "defense" and int(u.get("amount") or 0) > 0}
        want = B.payment_amount(reward, stock, 0)
        if not want:
            return self._bounty_fail(p, "the stock is too low to pay")
        ships, capacity = L.pick_cargo(ships_here, self._logi_stats(), sum(want.values()))
        if not ships:
            build = None if home.get("unit_queue") else L.choose_cargo_build(_PAYER, units, stock, ships_here)
            if build is not None:
                try:
                    self.client.post(f"/planets/{home['id']}/units", {"machine_name": build[0], "amount": build[1]})
                    self.stats["units"] += build[1]
                    self.log("units", what=build[0], amount=build[1], why="cargo ships to pay a bounty")
                except ApiError as e:
                    self.stats["refused"] += 1
                    self.log("units_refused", what=build[0], code=e.code, msg=e.message[:100])
            return self._bounty_fail(p, "no cargo ships", 180.0)
        target = {"coordinates": {"galaxy": at[0], "system": at[1], "position": at[2]}}
        possible, why, fuel, _seconds, _ = self._logi_check(home["id"], target, ships, "transport")
        if fuel is None or not possible:
            return self._bounty_fail(p, f"transport not possible: {why}"[:100])
        amounts = L.fit_to_capacity(B.payment_amount(reward, stock, fuel), capacity)
        amounts = {r: v for r, v in amounts.items() if v > 0}
        if not amounts or int(stock.get("deuterium", 0) or 0) < fuel:
            return self._bounty_fail(p, "not enough fuel or stock")
        try:
            self._logi_send(home["id"], "transport", target, ships, amounts)
        except ApiError as e:
            self.stats["refused"] += 1
            return self._bounty_fail(p, f"{e.code} {e.message[:80]}")
        for r, v in amounts.items():
            stock[r] = stock.get(r, 0) - v
        stock["deuterium"] = stock.get("deuterium", 0) - fuel
        own["paid"] = True
        self.log("bounty_paid", side="poster", bounty=bid, to=p["claimant"], at=p["at"], resources=amounts,
                 value=int(S.value_of(amounts)), promised=reward, fuel=fuel)
        return True

    # ---- being paid (the claimant) ---------------------------------------------------------------------------------------------
    def _bounty_inbox(self) -> None:
        st = self.memory.social
        if self.now - st["pay_mail_at"] >= S.MESSAGE_EVERY:
            st["pay_mail_at"] = self.now
            data = self.client.get("/messages?category=fleet_other&unread_only=1&per_page=20")
            for row in reversed(data["messages"]):        # oldest first
                if row.get("key") != "transport_received":
                    continue
                came_from, amounts = S.parse_received(row.get("body", ""))
                bid = B.owed_for_delivery(st["owed"], came_from, amounts)
                if bid is None:
                    continue
                self.client.post(f"/messages/{row['id']}/read")
                o = st["owed"].pop(bid)
                got = sum(v * VALUE[r] for r, v in amounts.items() if r in o["reward"])
                B.record_paid(st["rep"], o["poster"], got, self.now)
                self.log("bounty_paid", side="claimant", bounty=bid, poster=o["poster"], resources=amounts, value=int(got),
                         promised=o["reward"])
        for bid, o in list(st["owed"].items()):
            if self.now > o["deadline"]:
                del st["owed"][bid]
                B.record_unpaid(st["rep"], o["poster"], self.now)
                self.log("bounty_unpaid", side="claimant", bounty=bid, poster=o["poster"], promised=o["reward"],
                         why="no payment after the referee confirmed; shunned")
