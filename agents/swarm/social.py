"""What an agent does with the galaxy board (agents/board.py): pure decision functions, no network, no clock of their own.

The board lets agents leave short typed messages (OFFER, WANT, BOUNTY, WARNING, REVENGE, TAUNT, ALLIANCE), so
rule-based agents can use it without any language model. Three behaviours live here:

1. ROBBED: after a raid the victim posts a WARNING (a raider was seen, with its player id and coordinates) and a REVENGE
   (who joins me?). The coordinates come from the fleet it saw coming (Memory.aggressors), the strength from opponent memory.
2. WARNINGS: a cautious agent (miner, scientist, turtle) whose neighbour was raided puts the raider on its list of
   aggressors, which makes it look more often and rearm (the guard mechanism of defense.py), for a shorter time than if
   the raid had hit itself.
3. TRADE (persona `trader`): swaps a surplus resource for a missing one with ANOTHER AGENT, at the 3:2:1 reference
   prices of trade.py (the merchant's rates, brain.VALUE), by sending cargo ships with `transport` in small instalments.
   It is tit-for-tat: the one who answers a post sends first, a small instalment; each side sends the next only after the
   previous one has really arrived (the game's `transport_received` message says what came). Whoever does not pay back
   in time is a cheat and is shunned for a long time. Everything is capped (see the constants) so nobody can be drained.

All times are the game's server time (seconds), as in the rest of the swarm. Everything the agent remembers about the board
is one small dict, Memory.social (see new_state), saved with the rest of its memory.
"""
from __future__ import annotations

import re

from .brain import RESOURCES, VALUE
from .defense import RECENT_RAID

# ---- the board in general ------------------------------------------------------------------------------------
POSTS_PER_HOUR = 6          # an agent posts at most this many times per hour (the board has its own, looser, limit)
READ_EVERY = 90.0           # seconds between two reads of the board
DOWN_RETRY = 300.0          # seconds before it looks again after the board did not answer
READ_LIMIT = 100
SEEN_KEEP = 200

# ---- robbed / warned ------------------------------------------------------------------------------------------
MIN_ROBBERY = 1000          # a raid that took (or destroyed) less than this is not worth shouting about
ROBBERY_COOLDOWN = 1800.0   # one announcement per this long
RAIDER_RECENT = 3600.0      # the attacker's fleet must have been seen within this time before the report for us to name it
NEAR_SYSTEMS = 25           # "neighbour": within this many systems (same galaxy)
WARNING_MAX_AGE = 7200.0    # board seconds: older warnings are history
GUARD_SHARE = 0.5           # a warning puts a raider on the aggressor list as if it had hit us this long ago: guard lasts RECENT_RAID * (1 - this)

# ---- trade -----------------------------------------------------------------------------------------------------
TARGET_MIX = {"metal": 0.5, "crystal": 0.3, "deuterium": 0.2}   # share of the stock's value an agent likes to hold
SURPLUS_EXCESS = 0.12       # a resource is a surplus when its share is this far above the mix...
NEED_GAP = 0.08             # ...and a need when this far below
MIN_STOCK_VALUE = 20_000.0  # no trading with a poorer stock (metal-equivalents)
MAX_TRADE_SHARE = 0.10      # a deal is at most this share of the stock of the resource given
MIN_TRADE_VALUE = 3_000.0   # smaller deals are not worth a flight
ASK_MARGIN = 0.03           # a poster asks 3% above the reference price
ACCEPT_FLOOR = 0.95         # an answerer accepts when it gets at least this much value per value given
INSTALMENTS = 4             # a deal is sent in this many parts...
INSTALMENTS_TRUSTED = 3     # ...fewer with a partner that has paid before
MIN_INSTALMENT = 500.0      # smaller instalments are not sent
EXPOSURE_SHARE = 0.10       # goods sent and not yet paid back, over all deals, are at most this share of the stock's value
MAX_OPEN_TRADES = 2
OFFER_REFRESH = 1800.0      # a standing offer is posted again after this long
POST_MAX_AGE = 3600.0       # board seconds: older offers are ignored
TRADE_IDLE = 3600.0         # a deal in which nothing happens for this long is closed
TRADE_MAX_AGE = 6 * 3600.0
REPLY_SLACK = 1800.0        # the partner's answer may take this long, plus twice our flight time, before it counts as cheating
DEBT_ABS = 500.0            # a debt below this is rounding
DEBT_REL = 0.10             # ...or below this share of what we sent
SHUN_SECONDS = 24 * 3600.0  # first offence; doubles with each repeat (up to 8x)
REP_KEEP = 200
MESSAGE_EVERY = 60.0        # how often the inbox is read while a deal is open


def new_state() -> dict:
    return {"cursor": 0, "read_at": 0.0, "posted": [], "robbery_at": 0.0, "offer": None, "offer_at": 0.0,
            "trades": {}, "rep": {}, "answered": [], "mail_at": 0.0, "seen_mail": []}


def clean_state(raw) -> dict:
    """A saved state, tolerant of anything missing or malformed (old memory files simply have none)."""
    out = new_state()
    if not isinstance(raw, dict):
        return out
    for key in ("cursor",):
        if isinstance(raw.get(key), int):
            out[key] = raw[key]
    for key in ("read_at", "robbery_at", "offer_at", "mail_at"):
        if isinstance(raw.get(key), (int, float)):
            out[key] = float(raw[key])
    for key in ("posted", "answered", "seen_mail"):
        if isinstance(raw.get(key), list):
            out[key] = [x for x in raw[key] if isinstance(x, (int, float))][-SEEN_KEEP:]
    if isinstance(raw.get("offer"), dict):
        out["offer"] = raw["offer"]
    for key in ("trades", "rep"):
        if isinstance(raw.get(key), dict):
            out[key] = {str(k): v for k, v in raw[key].items() if isinstance(v, dict)}
    return out


# ---- small helpers -----------------------------------------------------------------------------------------------
def fmt_coord(c) -> str:
    return f"{c[0]}:{c[1]}:{c[2]}"


def parse_coord(text) -> tuple[int, int, int] | None:
    try:
        g, s, p = (int(x) for x in str(text).split(":"))
        return g, s, p
    except (ValueError, TypeError):
        return None


def near(a, b, systems: int = NEAR_SYSTEMS) -> bool:
    return a[0] == b[0] and abs(a[1] - b[1]) <= systems


def stock_value(stock: dict) -> float:
    return sum(VALUE[r] * max(0, int(stock.get(r, 0) or 0)) for r in RESOURCES)


def value_of(amounts: dict) -> float:
    return sum(VALUE[r] * max(0, int(v or 0)) for r, v in (amounts or {}).items() if r in VALUE)


def _int(x, default=0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


# ---- posting budget ------------------------------------------------------------------------------------------------
def can_post(state: dict, now: float) -> bool:
    state["posted"] = [t for t in state["posted"] if now - t < 3600]
    return len(state["posted"]) < POSTS_PER_HOUR


def note_post(state: dict, now: float) -> None:
    state["posted"].append(now)
    state["posted"] = state["posted"][-POSTS_PER_HOUR * 2:]


# ---- robbed: WARNING and REVENGE -------------------------------------------------------------------------------------
def robbery_posts(b: dict, aggressors: dict, opponents: dict, now: float, my_coord, my_id, state: dict) -> list[tuple[str, str, dict]]:
    """Posts to make after a battle report in which we were the defender: [(kind, text, data)], or [] (not robbed, too small,
    announced a moment ago). `b` is military.summarize_battle(...); `aggressors` is Memory.aggressors (origin coordinates of
    fleets seen coming, with the time); `opponents` is Memory.opponents (what we know about that planet)."""
    if b.get("role") != "defender":
        return []
    loot, lost = _int(b.get("loot")), _int(b.get("my_loss"))
    if loot < MIN_ROBBERY and lost < MIN_ROBBERY:
        return []
    last = state.get("robbery_at", 0.0)
    if last and now - last < ROBBERY_COOLDOWN:
        return []
    recent = [(when, c) for c, when in aggressors.items() if now - when <= RAIDER_RECENT]
    raider = max(recent)[1] if recent else None
    rec = opponents.get(raider) if raider else None
    data: dict = {"victim_at": fmt_coord(my_coord), "loot": loot, "lost": lost}
    if my_id is not None:
        data["victim_id"] = my_id
    if b.get("their_id") is not None:
        data["raider_id"] = b["their_id"]
    if raider:
        data["raider_at"] = fmt_coord(raider)
    if rec and rec.get("strength"):
        data["strength"] = int(rec["strength"])
    who = f"player {data['raider_id']}" if "raider_id" in data else "an unknown raider"
    where = f" from {data['raider_at']}" if "raider_at" in data else ""
    return [("WARNING", f"Robbed of {loot:,} at {data['victim_at']} by {who}{where}. Watch your stores.", data),
            ("REVENGE", f"Robbed by {who}{where}. Who joins me against this raider?", data)]


def wants_warnings(persona) -> bool:
    """Who listens to warnings: the careful, peaceful ones (miner, scientist, turtle)."""
    return persona.caution >= 0.5 and persona.aggression <= 0.2


def warned_raiders(persona, my_coord, posts: list[dict], board_now: float, my_name: str) -> list[tuple[int, int, int]]:
    """Coordinates of raiders named in fresh WARNING / REVENGE posts of OTHER agents about a neighbour (or a raider that
    lives close to us). Empty for personalities that do not care."""
    if not wants_warnings(persona):
        return []
    found: list[tuple[int, int, int]] = []
    for p in posts:
        if p.get("kind") not in ("WARNING", "REVENGE") or p.get("agent") == my_name:
            continue
        if board_now and board_now - float(p.get("t") or 0) > WARNING_MAX_AGE:
            continue
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        raider, victim = parse_coord(d.get("raider_at")), parse_coord(d.get("victim_at"))
        if raider is None or raider == tuple(my_coord):
            continue
        if (victim and near(victim, my_coord)) or near(raider, my_coord):
            if raider not in found:
                found.append(raider)
    return found


def guard_stamp(now: float) -> float:
    """The time to store in Memory.aggressors for a raider we were only warned about: the agent is on guard for half as long."""
    return now - GUARD_SHARE * RECENT_RAID


# ---- trade: what to offer ----------------------------------------------------------------------------------------------
def excess(stock: dict) -> dict[str, float] | None:
    """Share of the stock's value in each resource minus the share we like to hold. None for a stock too poor to trade."""
    total = stock_value(stock)
    if total < MIN_STOCK_VALUE:
        return None
    return {r: VALUE[r] * max(0, int(stock.get(r, 0) or 0)) / total - TARGET_MIX[r] for r in RESOURCES}


def pick_pair(stock: dict) -> tuple[str, str, bool] | None:
    """(surplus resource, missing resource, surplus_is_stronger) or None when the stock is balanced."""
    ex = excess(stock)
    if ex is None:
        return None
    sur, need = max(ex, key=ex.get), min(ex, key=ex.get)
    if sur == need or ex[sur] < SURPLUS_EXCESS or ex[need] > -NEED_GAP:
        return None
    return sur, need, ex[sur] >= -ex[need]


def deal_cap(stock: dict, give: str) -> int:
    """The most of `give` one deal may involve (a share of the stock)."""
    return int(max(0, int(stock.get(give, 0) or 0)) * MAX_TRADE_SHARE)


def make_offer(stock: dict, at, player_id) -> dict | None:
    """The standing offer to post: {'kind', 'text', 'data'}. data = give {res: amount}, want {res: amount}, at, player_id."""
    pair = pick_pair(stock)
    if pair is None:
        return None
    give, want, surplus_led = pair
    amount = deal_cap(stock, give)
    value = amount * VALUE[give]
    if value < MIN_TRADE_VALUE:
        return None
    ask = int(value * (1 + ASK_MARGIN) / VALUE[want])
    kind = "OFFER" if surplus_led else "WANT"
    text = (f"OFFER {amount:,} {give} for {ask:,} {want}" if surplus_led
            else f"WANT {ask:,} {want}, paying {amount:,} {give}")
    data = {"give": {give: amount}, "want": {want: ask}, "at": fmt_coord(at)}
    if player_id is not None:
        data["player_id"] = player_id
    return {"kind": kind, "text": text, "data": data}


def parse_trade_post(post: dict) -> dict | None:
    """The terms of an OFFER / WANT post from another agent, or None if it is not well formed."""
    if post.get("kind") not in ("OFFER", "WANT") or not isinstance(post.get("data"), dict):
        return None
    d = post["data"]
    try:
        (g_res, g_amt), = d["give"].items()
        (w_res, w_amt), = d["want"].items()
        g_amt, w_amt = int(g_amt), int(w_amt)
        pid = int(d["player_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    at = parse_coord(d.get("at"))
    if g_res not in VALUE or w_res not in VALUE or g_res == w_res or at is None:
        return None
    if not (0 < g_amt < 10 ** 9 and 0 < w_amt < 10 ** 9):
        return None
    return {"id": post.get("id"), "agent": post.get("agent"), "t": float(post.get("t") or 0), "give": g_res, "give_amt": g_amt,
            "want": w_res, "want_amt": w_amt, "at": at, "player_id": pid, "reply_to": d.get("reply_to")}


def pick_match(stock: dict, posts: list[dict], my_name: str, board_now: float, rep: dict, now: float,
               busy: set[str], answered: list) -> dict | None:
    """The best standing offer of another agent that gives what we miss for what we have too much of, at a fair price.
    Returns {'terms': parse_trade_post(...), 'total_value': value we would give in total, 'r': value we send per value received}."""
    pair = pick_pair(stock)
    if pair is None:
        return None
    surplus, need, _ = pair
    best = None
    for post in posts:
        t = parse_trade_post(post)
        if t is None or t["reply_to"] is not None or t["agent"] == my_name or t["agent"] in busy or post.get("id") in answered:
            continue
        if board_now and board_now - t["t"] > POST_MAX_AGE:
            continue
        if is_shunned(rep, t["agent"], now):
            continue
        if t["give"] != need or t["want"] != surplus:
            continue
        they_give, they_want = t["give_amt"] * VALUE[t["give"]], t["want_amt"] * VALUE[t["want"]]
        if they_give < ACCEPT_FLOOR * they_want:
            continue
        total = min(they_want, deal_cap(stock, surplus) * VALUE[surplus])
        if total < MIN_TRADE_VALUE:
            continue
        score = they_give / they_want + 0.05 * min(3, int((rep.get(t["agent"]) or {}).get("ok", 0)))
        if best is None or score > best[0]:
            best = (score, {"terms": t, "total_value": total, "r": they_want / they_give})
    return best[1] if best else None


def reply_post(terms: dict, match: dict, my_coord, my_id, stock: dict) -> dict:
    """The public answer that tells the poster we are starting (so it expects our cargo)."""
    give, want = terms["want"], terms["give"]
    amount = int(match["total_value"] / VALUE[give])
    ask = int(amount * VALUE[give] / match["r"] / VALUE[want])
    data = {"give": {give: amount}, "want": {want: ask}, "at": fmt_coord(my_coord), "player_id": my_id, "reply_to": terms["id"]}
    return {"kind": "OFFER", "text": f"Taking {terms['agent']}'s offer: I send {amount:,} {give} in parts, you send {ask:,} {want}.", "data": data}


# ---- trade: a deal in progress ---------------------------------------------------------------------------------------------
def new_trade(partner: str, pid: int, at, give: str, get: str, total_value: float, r: float, role: str, now: float,
              post_id=None, rep: dict | None = None) -> dict:
    """A deal. We send `give` and receive `get`; `r` = value we send per value we receive. The starter (the one who answered a
    post) is allowed one instalment on credit; the poster sends only against goods that have arrived."""
    t = {"partner": partner, "pid": int(pid), "at": fmt_coord(at), "give": give, "get": get, "total_value": float(total_value),
         "r": float(r), "role": role, "sent_value": 0.0, "recv_value": 0.0, "instalments": 0, "opened": now, "last_activity": now,
         "deadline": 0.0, "post_id": post_id, "status": "open"}
    t["per"] = float(total_value) / (INSTALMENTS_TRUSTED if (rep or {}).get("ok") else INSTALMENTS)
    t["credit_value"] = t["per"] if role == "starter" else 0.0
    return t


def sent_all(t: dict) -> bool:
    """Everything agreed has been sent (what is left is less than the smallest instalment we would send)."""
    return t["total_value"] - t["sent_value"] < min(MIN_INSTALMENT, t["per"])


def allowed_value(t: dict) -> float:
    """How much more (in value) we may send now: what the goods that arrived justify, plus the starter's credit, never above the deal."""
    by_payment = t["recv_value"] * t["r"] + t["credit_value"] - t["sent_value"]
    return max(0.0, min(t["total_value"] - t["sent_value"], by_payment))


def next_instalment(t: dict, stock: dict, others: list[dict]) -> int:
    """How many units of t['give'] to send now, 0 for nothing. Capped by the instalment size, what the goods received justify,
    MAX_TRADE_SHARE of the current stock of that resource, and the total exposure (goods out and not yet paid for) over all deals."""
    if t["status"] != "open" or sent_all(t):
        return 0
    value = min(t["per"], allowed_value(t))
    exposure = sum(max(0.0, o["sent_value"] - o["recv_value"] * o["r"]) for o in others if o is not t and o.get("status") == "open")
    exposure += max(0.0, t["sent_value"] - t["recv_value"] * t["r"])
    value = min(value, EXPOSURE_SHARE * stock_value(stock) - exposure)
    amount = min(int(value / VALUE[t["give"]]), deal_cap(stock, t["give"]))
    if amount * VALUE[t["give"]] < min(MIN_INSTALMENT, t["per"]):
        return 0
    return amount


def after_sending(t: dict, amount: int, now: float, flight_seconds: float) -> None:
    t["sent_value"] += amount * VALUE[t["give"]]
    t["instalments"] += 1
    t["last_activity"] = now
    t["deadline"] = now + 2 * float(flight_seconds or 0) + REPLY_SLACK


def after_receiving(t: dict, amounts: dict, now: float) -> float:
    got = sum(max(0, _int(v)) * VALUE[r] for r, v in amounts.items() if r == t["get"])
    t["recv_value"] += got
    t["last_activity"] = now
    return got


def debt(t: dict) -> float:
    """Value the partner owes us for what we sent (at the agreed rate)."""
    return max(0.0, t["sent_value"] / t["r"] - t["recv_value"]) if t["sent_value"] > 0 else 0.0


def judge(t: dict, now: float) -> str:
    """'open', 'done', 'cheated' or 'expired'."""
    owed = debt(t)
    unpaid = owed > max(DEBT_ABS, DEBT_REL * t["sent_value"] / t["r"])
    if unpaid and now > t["deadline"]:
        return "cheated"
    if unpaid:
        return "open"
    finished = sent_all(t)
    idle = now - t["last_activity"] > TRADE_IDLE
    if (finished or idle) and (t["recv_value"] > 0 or t["sent_value"] > 0):
        return "done"
    if idle or now - t["opened"] > TRADE_MAX_AGE:
        return "expired"
    return "open"


# ---- reputation ---------------------------------------------------------------------------------------------------------------
def is_shunned(rep: dict, partner: str, now: float) -> bool:
    return now < float((rep.get(partner) or {}).get("shun_until", 0) or 0)


def _rep(rep: dict, partner: str) -> dict:
    r = rep.setdefault(partner, {"ok": 0, "cheated": 0, "paid": 0.0, "given": 0.0, "shun_until": 0.0, "last": 0.0})
    if len(rep) > REP_KEEP:
        for k in sorted(rep, key=lambda k: rep[k].get("last", 0))[:len(rep) - REP_KEEP]:
            if k != partner:
                del rep[k]
    return r


def record_done(rep: dict, t: dict, now: float) -> None:
    r = _rep(rep, t["partner"])
    r["ok"] += 1
    r["paid"] += t["recv_value"]
    r["given"] += t["sent_value"]
    r["last"] = now


def record_cheat(rep: dict, t: dict, now: float) -> None:
    r = _rep(rep, t["partner"])
    r["cheated"] += 1
    r["given"] += t["sent_value"]
    r["paid"] += t["recv_value"]
    r["shun_until"] = now + SHUN_SECONDS * 2 ** min(3, r["cheated"] - 1)
    r["last"] = now


# ---- reading the inbox -------------------------------------------------------------------------------------------------------------
def parse_received(body: str) -> tuple[tuple[int, int, int] | None, dict[str, int]]:
    """(coordinates the cargo came from, amounts) from a `transport_received` message body."""
    coords = re.findall(r"(\d+):(\d+):(\d+)", body or "")
    came_from = tuple(int(x) for x in coords[0]) if coords else None
    amounts = {}
    for res in RESOURCES:
        m = re.search(res + r"\s*:\s*([\d.,]+)", body or "", re.IGNORECASE)
        if m:
            n = _int(re.sub(r"\D", "", m.group(1)))
            if n:
                amounts[res] = n
    return came_from, amounts


def trade_for_delivery(trades: dict, came_from, resources: dict) -> dict | None:
    """Which open deal a delivery belongs to: the one with that sender's coordinates; when the message names no sender, the only
    open deal expecting this resource. Cargo from anyone else is nobody's business here (it is simply kept)."""
    open_ = [t for t in trades.values() if t.get("status") == "open" and t["get"] in resources]
    if came_from is not None:
        return next((t for t in open_ if parse_coord(t["at"]) == tuple(came_from)), None)
    return open_[0] if len(open_) == 1 else None


def replies_to(posts: list[dict], offer_id, my_name: str, rep: dict, now: float, busy: set[str]) -> list[dict]:
    """Posts that answer our standing offer (data.reply_to == its id), from other agents we do not shun and have no open deal with."""
    found = []
    for post in posts:
        t = parse_trade_post(post)
        if t is None or offer_id is None or t["reply_to"] != offer_id or t["agent"] == my_name or t["agent"] in busy:
            continue
        if not is_shunned(rep, t["agent"], now):
            found.append(t)
    return found
