"""Revenge and bounties on the galaxy board: pure decision functions, no network, no clock of their own.

REVENGE JOIN. An agent that was robbed posts WARNING + REVENGE (social.robbery_posts). A hunter (raider, general class,
or anybody with a real aggression) that reads a REVENGE post may join: the named raider gets a priority bonus when the
hunter chooses whom to spy on and attack, and a short REVENGE reply (data.reply_to) tells the poster. Only when the hunter's
own strength allows it, never against a friend, one joined revenge at a time, and it ends after two hours. Peaceful
personalities ignore it. Joining changes WHOM the hunter prefers; the usual spy report, fight simulation and risk rules
still decide whether it really attacks.

BOUNTIES. A victim robbed for a lot (the loot is a big share of what it had) that is rich enough posts a BOUNTY on the raider:
a reward in resources, at most 10% of its stock, payable by it from its home planet. There is NO escrow: the reward is paid
AFTER the referee (referee.py) has confirmed a claim. A hunter that reads a BOUNTY adds the reward's value (brain.VALUE, the
3:2:1 reference prices) to the target's score; when it attacks the target and the combat report shows loot, it posts a
CLAIM. The referee checks the claim against the agents' logs and posts a VERDICT; on an OK the poster sends the reward with
a `transport` (logistics). A poster that does not pay is remembered as a cheat (reputation, like a trade cheat) by the claimant.

Times: `now` is the game's server time (as in the rest of the swarm); `wall` is the computer's clock, the one the board, the
agents' logs and the referee share (a bounty's `expires` is wall time).
"""
from __future__ import annotations

from . import social as S
from .brain import RESOURCES, VALUE

REFEREE = "referee"            # the board name of the referee: only its VERDICT posts count

# ---- revenge --------------------------------------------------------------------------------------------------
REVENGE_SECONDS = 7200.0       # a joined revenge lasts this long (server seconds)
REVENGE_BONUS = 2.0            # the named raider's worth is multiplied by this when choosing a target
REVENGE_MAX_RATIO = 1.5        # a raider whose known strength is above this times ours is not attacked ("far stronger")
FRIENDS_KEEP = 30

# ---- bounties --------------------------------------------------------------------------------------------------
BOUNTY_MIN_LOOT = 5_000        # a raid that took less is not worth a bounty
BOUNTY_LOOT_SHARE = 0.15       # ...and the loot must be this share of what the victim had (stock + loot)
BOUNTY_MIN_STOCK = 30_000.0    # a poster poorer than this (metal-equivalents) cannot afford one
BOUNTY_STOCK_CAP = 0.10        # the reward is at most this share of the stock (of each resource, and of its total value)
BOUNTY_LOOT_RATIO = 0.5        # ...and about this share of the loot
BOUNTY_MIN_REWARD = 2_000.0    # a smaller reward is not worth a hunt
BOUNTY_TTL = 6 * 3600.0        # wall seconds
BOUNTY_KEEP = 20               # bounties a hunter remembers
CLAIM_WAIT = 3 * 3600.0        # a claim without a verdict after this long (server seconds) is forgotten
PAY_WAIT = 3 * 3600.0          # a claimant waits this long (server seconds) after an OK verdict before it calls the poster a cheat
PAY_WINDOW = 2 * 3600.0        # a poster keeps trying to pay for this long (server seconds)
PAY_STOCK_CAP = 0.20           # the payment is at most this share of the poster's current stock of that resource
PAID_SHARE = 0.5               # a delivery of at least this share of the promised value counts as paid
DONE_KEEP = 100


def _int(x, default=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


# ---- who ---------------------------------------------------------------------------------------------------------
def is_hunter(persona) -> bool:
    """Who joins a revenge and hunts bounties: fighters (raider, balanced, general class) but not the peaceful personalities."""
    return persona.aggression > 0.2 or (persona.character_class == "general" and persona.aggression > 0)


def friend_names(st: dict) -> set[str]:
    """The agents we do not attack: those we joined a revenge for, and trade partners who paid us (reputation) or have an open deal."""
    names = set(st.get("friends") or [])
    for name, r in (st.get("rep") or {}).items():
        if isinstance(r, dict) and _int(r.get("ok"), 0) > 0 and not _int(r.get("cheated"), 0):
            names.add(name)
    for t in (st.get("trades") or {}).values():
        if isinstance(t, dict) and t.get("partner"):
            names.add(t["partner"])
    return names


def add_friend(st: dict, name: str) -> None:
    friends = [f for f in st["friends"] if f != name] + [name]
    st["friends"] = friends[-FRIENDS_KEEP:]


# ---- revenge: reading the call ----------------------------------------------------------------------------------------
def parse_revenge(post: dict) -> dict | None:
    """A call for revenge (an original REVENGE post, not a reply) with the raider's coordinates, or None."""
    if post.get("kind") != "REVENGE" or not isinstance(post.get("data"), dict):
        return None
    d = post["data"]
    if d.get("reply_to") is not None:
        return None
    raider = S.parse_coord(d.get("raider_at"))
    if raider is None:
        return None
    return {"id": post.get("id"), "agent": post.get("agent"), "t": float(post.get("t") or 0), "raider": raider,
            "raider_id": _int(d.get("raider_id")), "victim_at": S.parse_coord(d.get("victim_at")),
            "loot": _int(d.get("loot"), 0), "strength": _int(d.get("strength"))}


def strength_allows(my_strength: float, known: float | None) -> bool:
    """Never attack someone far stronger. A raider whose strength nobody knows is allowed: the spy report and the fight
    simulation decide before any ship leaves. With no fleet there is nothing to avenge anybody with."""
    if my_strength <= 0:
        return False
    return not known or known <= my_strength * REVENGE_MAX_RATIO


def pick_revenge(persona, posts: list[dict], board_now: float, my_name: str, my_coord, my_id, my_strength: float,
                 opponents: dict, friend_ids: set[int], allowed_ids: set[int] | None) -> dict | None:
    """The revenge call to join, or None. The biggest robbery first. `opponents` is Memory.opponents (a known strength wins over
    the poster's figure). `allowed_ids` None = anyone may be attacked, else only these players (agents)."""
    if not is_hunter(persona):
        return None
    best = None
    for post in posts:
        c = parse_revenge(post)
        if c is None or c["agent"] == my_name or c["id"] is None:
            continue
        if board_now and board_now - c["t"] > S.WARNING_MAX_AGE:
            continue
        if c["raider"] == tuple(my_coord) or (my_id is not None and c["raider_id"] == my_id):
            continue
        if c["raider_id"] is not None and c["raider_id"] in friend_ids:
            continue
        if allowed_ids is not None and (c["raider_id"] is None or c["raider_id"] not in allowed_ids):
            continue          # a human, or somebody we cannot name: agents only
        rec = opponents.get(c["raider"]) or {}
        if not strength_allows(my_strength, rec.get("strength") or c["strength"]):
            continue
        if best is None or c["loot"] > best["loot"]:
            best = c
    return best


def revenge_state(call: dict, now: float) -> dict:
    return {"post": call["id"], "agent": call["agent"], "coord": S.fmt_coord(call["raider"]), "pid": call["raider_id"],
            "joined": now, "until": now + REVENGE_SECONDS}


def revenge_active(st: dict, now: float) -> dict | None:
    r = st.get("revenge")
    if isinstance(r, dict) and now < float(r.get("until") or 0):
        return r
    return None


def revenge_reply(call: dict, my_coord) -> dict:
    who = f"player {call['raider_id']}" if call["raider_id"] is not None else "the raider"
    data = {"reply_to": call["id"], "raider_at": S.fmt_coord(call["raider"]), "joiner_at": S.fmt_coord(my_coord)}
    if call["raider_id"] is not None:
        data["raider_id"] = call["raider_id"]
    return {"kind": "REVENGE", "text": f"Joining {call['agent']} against {who} at {data['raider_at']}.", "data": data}


# ---- bounties: posting one -------------------------------------------------------------------------------------------------
def reward_for(stock: dict, loot: int) -> dict[str, int] | None:
    """{resource: amount}: about half the loot, from the resource we have most of, never above 10% of that stock or of the
    stock's value. None when we are too poor or it would be too little."""
    total = S.stock_value(stock)
    if total < BOUNTY_MIN_STOCK:
        return None
    res = max(RESOURCES, key=lambda r: VALUE[r] * max(0, _int(stock.get(r), 0) or 0))
    wanted = min(BOUNTY_LOOT_RATIO * loot, BOUNTY_STOCK_CAP * total)
    amount = min(int(wanted / VALUE[res]), int(BOUNTY_STOCK_CAP * max(0, _int(stock.get(res), 0) or 0)))
    if amount * VALUE[res] < BOUNTY_MIN_REWARD:
        return None
    return {res: amount}


def bounty_offer(loot: int, robbery: dict, stock: dict | None, my_coord, my_id, wall: float) -> dict | None:
    """The BOUNTY to post after a robbery, or None. `robbery` is the data of our WARNING post (raider_at, raider_id)."""
    if stock is None or my_id is None or loot < BOUNTY_MIN_LOOT:
        return None
    if not robbery.get("raider_at") or robbery.get("raider_id") is None:
        return None            # we need to name the target
    if loot < BOUNTY_LOOT_SHARE * (S.stock_value(stock) + loot):
        return None
    reward = reward_for(stock, loot)
    if reward is None:
        return None
    (res, amount), = reward.items()
    data = {"target_player_id": robbery["raider_id"], "target_at": robbery["raider_at"], "reward": reward,
            "expires": int(wall + BOUNTY_TTL), "poster_id": my_id, "poster_at": S.fmt_coord(my_coord)}
    text = (f"WANTED: player {robbery['raider_id']} at {robbery['raider_at']}. Reward {amount:,} {res} "
            f"for a raid that wins loot; paid after the referee confirms.")
    return {"kind": "BOUNTY", "text": text, "data": data, "ttl": BOUNTY_TTL}


def open_bounty(st: dict, wall: float) -> dict | None:
    """Our own bounty that is still running (one at a time)."""
    for b in st["bounty_out"].values():
        if isinstance(b, dict) and float(b.get("expires") or 0) > wall and not b.get("paid"):
            return b
    return None


# ---- bounties: reading them -----------------------------------------------------------------------------------------------------
def _reward(raw) -> dict[str, int] | None:
    if not isinstance(raw, dict) or not 0 < len(raw) <= 3:
        return None
    out = {}
    for r, v in raw.items():
        n = _int(v)
        if r not in VALUE or n is None or not 0 < n < 10 ** 9:
            return None
        out[r] = n
    return out


def parse_bounty(post: dict) -> dict | None:
    """The terms of a BOUNTY post, or None if it is not well formed."""
    if post.get("kind") != "BOUNTY" or not isinstance(post.get("data"), dict) or post.get("id") is None:
        return None
    d = post["data"]
    reward = _reward(d.get("reward"))
    target, poster_at = S.parse_coord(d.get("target_at")), S.parse_coord(d.get("poster_at"))
    pid, poster_id = _int(d.get("target_player_id")), _int(d.get("poster_id"))
    try:
        expires = float(d.get("expires"))
        t = float(post.get("t") or 0)
    except (TypeError, ValueError):
        return None
    if reward is None or target is None or poster_at is None or pid is None or poster_id is None:
        return None
    return {"id": post["id"], "poster": str(post.get("agent")), "t": t, "pid": pid, "at": S.fmt_coord(target), "reward": reward,
            "value": S.value_of(reward), "expires": expires, "poster_id": poster_id, "poster_at": S.fmt_coord(poster_at)}


def remember_bounties(st: dict, posts: list[dict], my_name: str, wall: float, now: float) -> list[dict]:
    """Store the well-formed, running BOUNTY posts of OTHER agents (not shunned posters); returns the new ones."""
    new = []
    for post in posts:
        b = parse_bounty(post)
        if b is None or b["poster"] == my_name or b["expires"] <= wall or S.is_shunned(st["rep"], b["poster"], now):
            continue
        if str(b["id"]) not in st["bounties"]:
            new.append(b)
        st["bounties"][str(b["id"])] = b
    for key in [k for k, b in st["bounties"].items() if float(b.get("expires") or 0) <= wall]:
        del st["bounties"][key]
    if len(st["bounties"]) > BOUNTY_KEEP:
        for key in sorted(st["bounties"], key=lambda k: st["bounties"][k].get("t", 0))[:len(st["bounties"]) - BOUNTY_KEEP]:
            del st["bounties"][key]
    return new


def running_bounties(st: dict, wall: float, now: float, claimed=()) -> list[dict]:
    """Known bounties that have not expired, from posters we do not shun, and that we have not claimed."""
    return [b for b in st["bounties"].values() if isinstance(b, dict) and float(b.get("expires") or 0) > wall
            and not S.is_shunned(st["rep"], str(b.get("poster")), now) and str(b.get("id")) not in claimed]


# ---- the target list ----------------------------------------------------------------------------------------------------------------
def new_focus() -> dict:
    return {"coords": {}, "ids": {}}


def _add_focus(focus: dict, coord, uid, mult: float, add: float) -> None:
    for table, key in ((focus["coords"], coord), (focus["ids"], uid)):
        if key is None:
            continue
        m, a = table.get(key, (1.0, 0.0))
        table[key] = (max(m, mult), a + add)


def focus_for(focus: dict | None, coord, owner_id) -> tuple[float, float]:
    """(multiplier, extra score) for a target: 1.0 and 0 for an ordinary one."""
    if not focus:
        return 1.0, 0.0
    c = focus["coords"].get(tuple(coord)) if coord is not None else None
    i = focus["ids"].get(owner_id) if owner_id is not None else None
    c, i = c or (1.0, 0.0), i or (1.0, 0.0)
    return max(c[0], i[0]), max(c[1], i[1])


def build_focus(st: dict, persona, now: float, wall: float, my_id, friend_ids: set[int], allowed_ids: set[int] | None) -> dict | None:
    """Who a hunter prefers right now: the raider of the revenge it joined (a multiplier) and the targets of running bounties
    (the reward's value added to the score). Never ourselves, a friend, or somebody we may not attack. None when there is nothing."""
    if not is_hunter(persona):
        return None
    focus = new_focus()

    def allowed(uid) -> bool:
        if uid is not None and (uid == my_id or uid in friend_ids):
            return False
        return allowed_ids is None or (uid is not None and uid in allowed_ids)

    r = revenge_active(st, now)
    if r and allowed(r.get("pid")):
        _add_focus(focus, S.parse_coord(r.get("coord")), r.get("pid"), REVENGE_BONUS, 0.0)
    for b in running_bounties(st, wall, now, claimed=set(st["claims"])):
        if allowed(b["pid"]) and b["poster_id"] != my_id:
            _add_focus(focus, S.parse_coord(b["at"]), b["pid"], 1.0, float(b["value"]))
    return focus if focus["coords"] or focus["ids"] else None


# ---- claiming ---------------------------------------------------------------------------------------------------------------------------
def claims_for(b: dict, st: dict, my_name: str, my_id, my_coord, wall: float, now: float) -> list[tuple[dict, dict]]:
    """[(bounty, CLAIM post)] for the running bounties on the planet we just raided, when the combat report shows loot.
    `b` is military.summarize_battle(...) (with `report_id` added by the agent)."""
    if b.get("role") != "attacker" or b.get("probes_only") or _int(b.get("loot"), 0) <= 0 or my_id is None:
        return []
    found = []
    for bounty in running_bounties(st, wall, now, claimed=set(st["claims"])):
        if bounty["poster"] == my_name or bounty["poster_id"] == my_id:
            continue
        if not (b.get("their_id") == bounty["pid"] or b.get("at") == bounty["at"]):
            continue
        data = {"bounty_id": bounty["id"], "report": b.get("report_id"), "at": b.get("at"), "time": int(wall),
                "claimant_id": my_id, "claimant_at": S.fmt_coord(my_coord), "loot": _int(b.get("loot"), 0)}
        text = f"Claiming bounty {bounty['id']} on player {bounty['pid']}: raided {b.get('at')} and took {data['loot']:,}."
        found.append((bounty, {"kind": "CLAIM", "text": text, "data": data}))
    return found


# ---- the verdict and the payment ---------------------------------------------------------------------------------------------------------
def parse_verdict(post: dict) -> dict | None:
    """A VERDICT from the referee (nobody else's counts), or None."""
    if post.get("kind") != "VERDICT" or post.get("agent") != REFEREE or not isinstance(post.get("data"), dict):
        return None
    d = post["data"]
    bid, cid = d.get("bounty_id"), d.get("claim_id")
    if bid is None or cid is None or not isinstance(d.get("ok"), bool):
        return None
    return {"id": post.get("id"), "bounty": bid, "claim": cid, "ok": d["ok"], "why": str(d.get("why", ""))[:120],
            "claimant": d.get("claimant"), "claimant_id": _int(d.get("claimant_id")), "claimant_at": d.get("claimant_at")}


def payment_amount(reward: dict, stock: dict, fuel: int) -> dict[str, int]:
    """What can be paid now: the promised reward, never more than PAY_STOCK_CAP of the stock of each resource (the fuel comes off
    the deuterium first)."""
    out = {}
    for r, promised in reward.items():
        have = max(0, _int(stock.get(r), 0) or 0) - (fuel if r == "deuterium" else 0)
        n = min(int(promised), int(PAY_STOCK_CAP * max(0, have)))
        if n > 0:
            out[r] = n
    return out


def owed_for_delivery(owed: dict, came_from, amounts: dict) -> str | None:
    """Which bounty a delivery pays: the one whose poster's coordinates it came from and whose reward it (about) covers."""
    for bid, o in owed.items():
        if came_from is not None and S.parse_coord(o.get("poster_at")) != tuple(came_from):
            continue
        reward = o.get("reward") or {}
        got = sum(max(0, _int(v, 0)) * VALUE[r] for r, v in amounts.items() if r in reward)
        if got >= PAID_SHARE * S.value_of(reward) > 0:
            return bid
    return None


def record_unpaid(rep: dict, poster: str, now: float) -> None:
    """The poster did not pay: shunned like a trade cheat (24 h, doubling for repeat offenders), so its bounties are ignored."""
    r = S._rep(rep, poster)
    r["cheated"] += 1
    r["shun_until"] = now + S.SHUN_SECONDS * 2 ** min(3, r["cheated"] - 1)
    r["last"] = now


def record_paid(rep: dict, poster: str, value: float, now: float) -> None:
    r = S._rep(rep, poster)
    r["ok"] += 1
    r["paid"] += value
    r["last"] = now
