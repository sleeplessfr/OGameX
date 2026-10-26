"""Diplomacy: first contact and a ladder of relationships between agents. Pure functions, no network (the calls are in diplomacy_agent.py).

In plain words (Stellaris style): an agent does not know another player until they MEET. Meeting means a spy report on them, a
battle with them, a board post from them, or being within 2 systems of one of our planets. After that the pair climbs a ladder,
one step at a time, and can fall down it again:

    unknown -> contact -> neutral -> friendly -> pact -> alliance
                 hello     trust       proposal    proposal
                 accepted  reaches 20  + trust 40  + trust 65 + a shared threat

* `contact -> neutral`, `friendly -> pact` and `pact -> alliance` are PROPOSALS: one agent posts a HELLO on the galaxy board, the other
  answers ACCEPT, REFUSE or WAIT (a REPLY post). A language model may write the words and give the answer, but the HARD RULES below are
  checked in code and it cannot override them. `neutral -> friendly` is automatic once trust is high enough.
* A pact means "neither raids the other": the raid target picker skips the partner. An alliance makes the agent prefer the in-game
  alliance of that partner (the existing alliance code does the rest).
* Each pair has `trust` (0..100) and `opinion` (-100..100). They only move by events: completed trades, shared warnings, accepted
  proposals and a little peaceful time (capped per day) go up; a raid, a cheat and above all breaking a pact go down. A betrayal is
  remembered: nothing is proposed or accepted for 48 hours.

HARD RULES (hard_check): never a step with someone who attacked us in the last 12 h, who robbed us, or against whom we hold an open
revenge call or bounty; never skip a step; after a refusal 6 hours of silence for that pair; pact needs trust 40, alliance 65.

All times are server seconds (like the rest of Memory).
"""
from __future__ import annotations

import random
import re
import zlib

STEPS = ("unknown", "contact", "neutral", "friendly", "pact", "alliance")
PEACE_STEPS = ("pact", "alliance")      # these partners are never raided

CONTACT_SYSTEMS = 2          # same galaxy and at most this many systems from one of our planets = contact
HELLO_EVERY = 3 * 3600.0     # one proposal per agent per this long (and at most one open)
OPEN_TIMEOUT = 2 * 3600.0    # an unanswered proposal counts as a refusal after this
REFUSAL_COOL = 6 * 3600.0    # after a refusal nobody proposes to that pair for this long
HIT_GRUDGE = 12 * 3600.0     # who attacked us this recently is never answered with a step
ROBBED_GRUDGE = 48 * 3600.0  # ... and who robbed us, for longer
BETRAY_COOL = 48 * 3600.0    # a betrayal (they attacked us under a pact) blocks every step for this long
LATER_RETRY = 1800.0         # WAIT: ask again after this ...
LATER_TRIES = 2              # ... at most this many times, then it is a refusal
HELLO_FRESH = 3 * 3600.0     # board seconds: an older HELLO is ignored

FRIENDLY_AT, PACT_AT, ALLIANCE_AT = 20.0, 40.0, 65.0         # trust needed to reach / be offered each step
FALL_BELOW = {"friendly": 10.0, "pact": 25.0, "alliance": 40.0}   # trust under which the step falls back one
ALLIANCE_MIN_SOCIABILITY = 0.3   # the in-game alliance mechanics need a sociable persona

GAIN = {"trade": 8.0, "warning": 3.0, "accepted": 6.0, "peace_hour": 1.0}
DAY_CAP = 6.0                # trust from peaceful time and warnings together, per day
LOSS = {"raided_us": 40.0, "we_raided": 25.0, "cheat": 30.0, "spied": 3.0, "spy_lie": 30.0}
SPY_COOL = 3 * 3600.0       # a probe costs trust at most this often per pair
LOG_BIG = 5.0                # a trust change this big is always logged
LOG_EVERY = 6 * 3600.0       # smaller ones at most this often per pair

# ---- pact limits (so that peace cannot take over the galaxy) ----
PACT_LIFE = 3 * 86400.0       # a pact lasts this long (server seconds), then it is renewed or it falls back to `friendly`
PACT_RENEW_WINDOW = 12 * 3600.0   # peaceful time in the last stretch of a pact renews it (trust >= PACT_AT and no grudge)
PACT_EXPIRE_COOL = 6 * 3600.0     # after an expiry nobody proposes a new pact to that pair for this long
SLOTS_MIN, SLOTS_MAX = 1, 6       # pact_slots() always lies in this range
RAIDER_AT = 0.4               # aggression from which an agent only makes pacts with partners it could NOT profitably raid
RUTHLESS_AT = 0.6             # aggression from which an agent has a single slot (two when sociable) and may betray
STRONGER_RATIO = 2.0          # a partner with score ratio >= this (theirs vs ours, see standing()) is too strong to raid
DEFENCE_VS_LOOT = 2.0         # a known defence worth >= this times the loot (and >= min_loot) makes a raid unprofitable
PACT_INTEL_MAX_AGE = 24 * 3600.0  # a spy report older than this is not used for the "could I raid them" question
BETRAY_CHANCE = 0.15          # chance per day that a ruthless agent with a pact considers breaking one
BETRAY_LOOT_FACTOR = 8        # ... only for a loot of at least this times its usual min_loot ...
BETRAY_LOOT_FLOOR = 5000      # ... and at least this much in absolute terms
BETRAY_WIN_CHANCE = 0.9       # ... and a simulated fight that is won with at least this chance
BETRAY_EVERY = 24 * 3600.0    # at most one betrayal per agent per this long

KEEP_RELATIONS = 200


# ---- state ------------------------------------------------------------------------------------------------------------------
def new_state() -> dict:
    return {"rel": {}, "open": None, "next_hello_at": 0.0, "read_at": 0.0, "scan_at": 0.0, "cursor": 0,
            "handled": [], "later": {}, "born": 0.0, "betray_at": 0.0}


def new_rel(now: float, how: str = "") -> dict:
    return {"step": "contact", "trust": 0.0, "opinion": 0.0, "since": float(now), "how": how, "name": "", "coord": "",
            "imp": False, "role": "", "ratio": None, "rank": None, "hit_us": 0, "spied_us": 0, "last_hit_at": 0.0, "robbed_at": 0.0,
            "betrayed_at": 0.0, "betrayals": 0, "cool_until": 0.0, "trades": 0, "cheats": 0, "warned_us_at": 0.0,
            "peace_at": float(now), "day": 0, "day_gain": 0.0, "log_at": 0.0, "warn_at": 0.0,
            "pact_since": 0.0, "pact_until": 0.0, "renewals": 0, "we_betrayed": 0, "spied_at": 0.0, "spy_lies": 0}


def _num(value, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:      # NaN
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def clean_rel(raw, now: float = 0.0) -> dict | None:
    """One relationship from a saved memory file; anything malformed goes back to its default. None if it is not a dict."""
    if not isinstance(raw, dict):
        return None
    rel = new_rel(now)
    for key, default in new_rel(now).items():
        value = raw.get(key, default)
        if key == "step":
            rel[key] = value if value in STEPS and value != "unknown" else "contact"
        elif key == "trust":
            rel[key] = _num(value, 0.0, 0.0, 100.0)
        elif key == "opinion":
            rel[key] = _num(value, 0.0, -100.0, 100.0)
        elif key in ("how", "name", "coord", "role"):
            rel[key] = value[:60] if isinstance(value, str) else ""
        elif key == "imp":
            rel[key] = bool(value)
        elif key == "ratio":
            rel[key] = _num(value, 0.0) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        elif key == "rank":
            rel[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) else None
        elif key in ("hit_us", "spied_us", "betrayals", "trades", "cheats", "day", "renewals", "we_betrayed", "spy_lies"):
            rel[key] = int(_num(value, 0, 0))
        else:
            rel[key] = _num(value, float(default), 0.0)
    return rel


def clean_state(raw) -> dict:
    """A saved state: tolerant of anything missing or malformed (old memory files simply have none)."""
    st = new_state()
    if not isinstance(raw, dict):
        return st
    if isinstance(raw.get("rel"), dict):
        for key, value in list(raw["rel"].items())[:KEEP_RELATIONS]:
            rel = clean_rel(value)
            if rel is not None and str(key).lstrip("-").isdigit():
                st["rel"][str(int(key))] = rel
    op = raw.get("open")
    if isinstance(op, dict) and op.get("to") is not None and op.get("step") in STEPS:
        st["open"] = {"to": int(_num(op.get("to"), 0)), "to_name": str(op.get("to_name") or "")[:60], "step": op["step"],
                      "post": op.get("post"), "at": _num(op.get("at"), 0.0)}
    for key in ("next_hello_at", "read_at", "scan_at", "born", "betray_at"):
        st[key] = _num(raw.get(key), 0.0, 0.0)
    st["cursor"] = int(_num(raw.get("cursor"), 0, 0))
    if isinstance(raw.get("handled"), list):
        st["handled"] = [x for x in raw["handled"] if isinstance(x, int) and not isinstance(x, bool)][-100:]
    if isinstance(raw.get("later"), dict):
        for key, value in list(raw["later"].items())[:20]:
            if isinstance(value, dict) and str(key).lstrip("-").isdigit():
                st["later"][str(int(key))] = {k: v for k, v in value.items() if isinstance(v, (int, float, str, bool, type(None)))}
    return st


def step_index(step: str) -> int:
    return STEPS.index(step) if step in STEPS else 0


def next_step(step: str) -> str | None:
    i = step_index(step)
    return STEPS[i + 1] if i + 1 < len(STEPS) else None


def prev_step(step: str) -> str:
    return STEPS[max(1, step_index(step) - 1)]


def step_of(st: dict, uid) -> str:
    rel = st["rel"].get(str(uid))
    return rel["step"] if rel else "unknown"


def pact_valid(rel: dict, now: float | None = None) -> bool:
    """Is this relationship protecting the partner? An alliance always; a pact until `pact_until` (0 = an old saved pact without an end
    date: valid until settle() gives it one)."""
    step = rel.get("step")
    if step == "alliance":
        return True
    if step != "pact":
        return False
    until = rel.get("pact_until") or 0.0
    return now is None or until <= 0 or now < until


def peace_ids(st: dict, now: float | None = None) -> set[int]:
    """Players we have a valid pact or an alliance with: never a raid target (an expired pact no longer protects)."""
    return {int(k) for k, r in st["rel"].items() if pact_valid(r, now)}


def pact_used(st: dict, now: float | None = None) -> int:
    """How many pact slots are taken: valid pacts plus alliances."""
    return sum(1 for r in st["rel"].values() if pact_valid(r, now))


def pact_slots(persona) -> int:
    """The most pact and alliance partners at the same time this personality accepts (1..6).

    Aggression decides first: ruthless (>= 0.6) 1, or 2 when sociable (>= 0.5); 0.4..0.6 two; 0.2..0.4 two, or three when sociable;
    peaceful ones start at 4 and gain one for a cautious nature (>= 0.6) and one for a sociable or trading one (>= 0.7), so
    miners, turtles and traders get 5-6, scientists 5."""
    g = lambda name: _num(getattr(persona, name, 0.0), 0.0, 0.0, 1.0)
    agg, soc = g("aggression"), max(g("sociability"), g("trade_interest"))
    if agg >= RUTHLESS_AT:
        n = 2 if soc >= 0.5 else 1
    elif agg >= RAIDER_AT:
        n = 2
    elif agg >= 0.2:
        n = 3 if soc >= 0.5 else 2
    else:
        n = 4 + (1 if g("caution") >= 0.6 else 0) + (1 if soc >= 0.7 else 0)
    return max(SLOTS_MIN, min(SLOTS_MAX, n))


def is_raider(persona) -> bool:
    return _num(getattr(persona, "aggression", 0.0), 0.0, 0.0, 1.0) >= RAIDER_AT


def could_raid(persona, info: dict | None) -> bool:
    """Would a raid on this partner look worthwhile? `info` has what we know: `ratio` (their score vs ours), `strength` and `loot` of
    the last spy report (None = unknown), `age` of that report. No information at all counts as 'yes' (a raider cannot tell)."""
    info = info or {}
    ratio, strength, loot = info.get("ratio"), info.get("strength"), info.get("loot")
    age = info.get("age")
    fresh = strength is not None and loot is not None and (age is None or age <= PACT_INTEL_MAX_AGE)
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and ratio >= STRONGER_RATIO:
        return False                         # far stronger than us: not a target
    if fresh:
        min_loot = _num(getattr(persona, "min_loot", 0), 0.0, 0.0)
        if loot < min_loot or strength >= DEFENCE_VS_LOOT * max(loot, min_loot, 1):
            return False                     # nothing worth taking, or defended too well for the loot
        return True
    return True                              # weak, peer or unknown without a fresh report: looks like a target


def alliance_ids(st: dict) -> set[int]:
    return {int(k) for k, r in st["rel"].items() if r.get("step") == "alliance"}


# ---- contact and first impression --------------------------------------------------------------------------------------------
def add_contact(st: dict, uid, now: float, how: str, name: str = "") -> dict | None:
    """Record that we met `uid`. Returns the new relationship, or None when we already knew them."""
    key = str(int(uid))
    rel = st["rel"].get(key)
    if rel is not None:
        if name and not rel["name"]:
            rel["name"] = name
        return None
    if len(st["rel"]) >= KEEP_RELATIONS:
        return None
    rel = st["rel"][key] = new_rel(now, how)
    rel["name"] = name
    return rel


def near(my_coords, coord) -> bool:
    """Is `coord` in the same galaxy, within CONTACT_SYSTEMS systems of one of our planets?"""
    try:
        g, s = int(coord[0]), int(coord[1])
    except (TypeError, ValueError, IndexError):
        return False
    return any(c[0] == g and abs(c[1] - s) <= CONTACT_SYSTEMS for c in my_coords)


def neighbours(my_coords, rows: list[dict], my_uid, allowed: set[int] | None) -> list[dict]:
    """Highscore rows of players living near us (only `allowed` ids when given). Tolerant of broken rows."""
    out = []
    for row in rows or []:
        try:
            uid = int(row["user_id"])
            mp = row["main_planet"]
            c = (int(mp["galaxy"]), int(mp["system"]), int(mp["position"]))
        except (KeyError, TypeError, ValueError):
            continue
        if row.get("is_me") or uid == my_uid or (allowed is not None and uid not in allowed):
            continue
        if near(my_coords, c):
            out.append(row)
    return out


def _score(row) -> float | None:
    try:
        return float(row["score"])
    except (KeyError, TypeError, ValueError):
        return None


def categories(row: dict | None) -> dict[str, float]:
    """Category scores of a highscore row, if the API gave them (keys `economy`, `research`, `military` or `*_score`, or nested
    under `scores`); `total_ships` too. Missing = empty."""
    out: dict[str, float] = {}
    if not isinstance(row, dict):
        return out
    nested = row.get("scores") if isinstance(row.get("scores"), dict) else {}
    for kind in ("economy", "research", "military"):
        for src in (row.get(f"{kind}_score"), row.get(kind), nested.get(kind)):
            if isinstance(src, dict):
                src = src.get("score")
            if isinstance(src, (int, float)) and not isinstance(src, bool):
                out[kind] = float(src)
                break
    if isinstance(row.get("total_ships"), (int, float)) and not isinstance(row.get("total_ships"), bool):
        out["ships"] = float(row["total_ships"])
    return out


def standing(my_score, row: dict | None, rows: list[dict] | None = None) -> dict:
    """Where the other player stands against us: score ratio, ranks, 'stronger' / 'weaker' / 'peer' / 'unknown'."""
    theirs = _score(row) if row else None
    if theirs is None or my_score is None:
        return {"ratio": None, "role": "unknown", "rank": (row or {}).get("rank") if isinstance((row or {}).get("rank"), int) else None,
                "my_rank": None, "cats": categories(row)}
    ratio = (theirs + 50.0) / (float(my_score) + 50.0)    # the +50 keeps the first hours (scores near 0) from looking lopsided
    role = "stronger" if ratio >= 2.0 else "weaker" if ratio <= 0.5 else "peer"
    scores = [s for s in (_score(r) for r in rows or []) if s is not None]
    my_rank = 1 + sum(1 for s in scores if s > float(my_score)) if scores else None
    rank = row.get("rank") if isinstance(row.get("rank"), int) else (1 + sum(1 for s in scores if s > theirs) if scores else None)
    return {"ratio": round(ratio, 2), "role": role, "rank": rank, "my_rank": my_rank, "cats": categories(row)}


def first_impression(persona, my_score, row: dict | None, rows: list[dict] | None = None) -> dict | None:
    """Opinion of a player we just met, from the highscore and our personality. None when the ranking has nothing on them.

    A much stronger neighbour is respected (a cautious agent most of all); a much weaker one is a target or a nuisance (an aggressive
    agent most of all). Sociable and trading personalities start friendlier."""
    if not isinstance(row, dict):
        return None
    st = standing(my_score, row, rows)
    g = lambda name: _num(getattr(persona, name, 0.0), 0.0, 0.0, 1.0)
    opinion = 5.0 + 15.0 * g("sociability") + 8.0 * g("trade_interest")
    if st["role"] == "stronger":
        opinion += 10.0 + 25.0 * g("caution") - 10.0 * g("aggression")
    elif st["role"] == "weaker":
        opinion -= 10.0 + 40.0 * g("aggression")
    cats = st["cats"]
    if cats.get("military", 0) > 0 and cats.get("military", 0) >= 0.6 * (_score(row) or 1):   # mostly an army: be wary
        opinion -= 5.0 * g("caution")
    return {"opinion": max(-50.0, min(50.0, opinion)), **{k: st[k] for k in ("role", "ratio", "rank", "my_rank")}}


def apply_impression(rel: dict, imp: dict | None) -> bool:
    if imp is None or rel["imp"]:
        return False
    rel.update(opinion=imp["opinion"], role=imp["role"], ratio=imp["ratio"], rank=imp["rank"], imp=True)
    return True


# ---- trust ----------------------------------------------------------------------------------------------------------------------
def bump(rel: dict, delta: float, now: float, *, soft: bool = False) -> float:
    """Move trust by `delta` (0..100). `soft` gains (peace, warnings) share a daily cap. Returns the change really made."""
    if delta > 0 and soft:
        day = int(now // 86400)
        if rel["day"] != day:
            rel["day"], rel["day_gain"] = day, 0.0
        delta = min(delta, max(0.0, DAY_CAP - rel["day_gain"]))
        rel["day_gain"] += delta
    before = rel["trust"]
    rel["trust"] = max(0.0, min(100.0, before + delta))
    return rel["trust"] - before


def loggable(rel: dict, change: float, now: float) -> bool:
    """Keep the log small: big changes always, small ones once in a while per pair."""
    if abs(change) < 1e-9:
        return False
    if abs(change) >= LOG_BIG or now - rel["log_at"] >= LOG_EVERY:
        rel["log_at"] = now
        return True
    return False


def peace_tick(rel: dict, now: float) -> float:
    """A little trust for peaceful time (one point an hour, capped per day), unless they attacked us recently."""
    if now - rel["last_hit_at"] < HIT_GRUDGE or now - rel["betrayed_at"] < BETRAY_COOL:
        rel["peace_at"] = now
        return 0.0
    hours = int((now - rel["peace_at"]) // 3600)
    if hours < 1:
        return 0.0
    rel["peace_at"] += hours * 3600
    return bump(rel, GAIN["peace_hour"] * hours, now, soft=True)


def start_pact(rel: dict, now: float) -> None:
    rel["pact_since"], rel["pact_until"] = float(now), float(now) + PACT_LIFE


def pact_time(rel: dict, now: float, renew_ok: bool = True, events: list | None = None) -> bool:
    """A pact's clock. Near the end, peaceful time renews it (trust >= PACT_AT, no grudge, and `renew_ok`: the agent's own persona
    check); at the end it falls back to `friendly`. Appends 'renewed' / 'expired' to `events`. True if the step changed."""
    if rel["step"] != "pact":
        return False
    if rel["pact_until"] <= 0:                 # a pact saved before pacts expired: it starts its clock now
        start_pact(rel, now)
        return False
    if now >= rel["pact_until"] - PACT_RENEW_WINDOW and rel["trust"] >= PACT_AT and renew_ok and not blocked_by_history(rel, now):
        rel["pact_until"] = float(now) + PACT_LIFE
        rel["renewals"] += 1
        if events is not None:
            events.append("renewed")
        return False
    if now >= rel["pact_until"]:
        rel["step"] = "friendly"
        rel["pact_until"] = 0.0
        rel["cool_until"] = max(rel["cool_until"], now + PACT_EXPIRE_COOL)
        if events is not None:
            events.append("expired")
        return True
    return False


def settle(rel: dict, now: float, renew_ok: bool = True, events: list | None = None) -> list[tuple[str, str]]:
    """Apply trust to the ladder: fall back one step when trust is too low, `neutral -> friendly` when it is high enough, and the
    pact clock (renew near the end, else `pact -> friendly`). Returns the (from, to) moves made. One move per call, so a step is
    never skipped."""
    step = rel["step"]
    low = FALL_BELOW.get(step)
    if low is not None and rel["trust"] < low:
        rel["step"] = prev_step(step)
        if rel["step"] == "pact":
            start_pact(rel, now)
        return [(step, rel["step"])]
    if step == "pact" and pact_time(rel, now, renew_ok, events):
        return [("pact", "friendly")]
    if step == "neutral" and rel["trust"] >= FRIENDLY_AT and rel["opinion"] >= 0 and not blocked_by_history(rel, now):
        rel["step"] = "friendly"
        return [(step, "friendly")]
    return []


def blocked_by_history(rel: dict, now: float) -> str | None:
    if now - rel["betrayed_at"] < BETRAY_COOL:
        return "betrayal not forgotten"
    if now - rel["last_hit_at"] < HIT_GRUDGE:
        return "attacked us in the last 12 hours"
    if now - rel["robbed_at"] < ROBBED_GRUDGE:
        return "robbed us"
    return None


def on_hit(rel: dict, now: float, robbed: bool = False) -> dict:
    """They attacked us. Trust and opinion drop hard; under a pact or alliance it is a BETRAYAL. Returns what happened."""
    was = rel["step"]
    rel["hit_us"] += 1
    rel["last_hit_at"] = now
    if robbed:
        rel["robbed_at"] = now
    betrayal = was in PEACE_STEPS
    before = rel["trust"]
    change = bump(rel, -LOSS["raided_us"], now)
    rel["opinion"] = max(-100.0, rel["opinion"] - 30.0)
    if betrayal:
        rel["betrayed_at"] = now
        rel["betrayals"] += 1
        rel["step"] = "contact"
        rel["trust"] = 0.0
        rel["opinion"] = min(rel["opinion"], -60.0)
        change = -before
    elif step_index(was) > step_index("contact"):
        rel["step"] = prev_step(was)
    return {"change": change, "betrayal": betrayal, "from": was, "to": rel["step"]}


def on_we_hit(rel: dict, now: float) -> dict:
    """We attacked them (against a pact this never happens: the target picker skips partners)."""
    was = rel["step"]
    change = bump(rel, -LOSS["we_raided"], now)
    rel["opinion"] = max(-100.0, rel["opinion"] - 10.0)
    if step_index(was) > step_index("contact"):
        rel["step"] = prev_step(was)
    return {"change": change, "from": was, "to": rel["step"]}


def betray_draw(name: str, day: int) -> bool:
    """The agent's own dice for one day (deterministic from its name and the day; never the persona's random stream)."""
    return random.Random(f"{name}:betray:{int(day)}").random() < BETRAY_CHANCE


def betray_loot_ok(persona, loot) -> bool:
    """Is the loot big enough to break a pact for: BETRAY_LOOT_FACTOR times the usual minimum and at least BETRAY_LOOT_FLOOR?"""
    try:
        loot = float(loot)
    except (TypeError, ValueError):
        return False
    return loot >= max(BETRAY_LOOT_FACTOR * _num(getattr(persona, "min_loot", 0), 0.0, 0.0), BETRAY_LOOT_FLOOR)


def on_we_betray(rel: dict, now: float) -> dict:
    """We attacked a pact partner on purpose. The normal cost of a raid applies, and then the relationship is wiped like a betrayal
    on their side: `contact`, trust 0, `betrayed_at` set (BETRAY_COOL blocks every step for 48 hours)."""
    res = on_we_hit(rel, now)
    rel["step"] = "contact"
    rel["trust"] = 0.0
    rel["opinion"] = min(rel["opinion"], -60.0)
    rel["betrayed_at"] = now
    rel["we_betrayed"] += 1
    rel["pact_until"] = 0.0
    return {**res, "to": "contact"}


def on_spied(rel: dict, now: float) -> dict:
    """They sent a spy probe at us. From a stranger it is normal OGame: a small trust loss, at most every SPY_COOL. From someone who
    holds a friendly / pact / alliance step with us (they asked for peace, then spied) it is a LIE: big trust and opinion loss, the
    step falls one rung, and `spy_lies` counts it for the prompts."""
    was = rel["step"]
    rel["spied_us"] += 1
    if step_index(was) >= step_index("friendly"):
        rel["spy_lies"] += 1
        rel["spied_at"] = now
        change = bump(rel, -LOSS["spy_lie"], now)
        rel["opinion"] = max(-100.0, rel["opinion"] - 30.0)
        rel["step"] = prev_step(was)
        if was == "pact":
            rel["pact_until"] = 0.0
        return {"change": change, "lie": True, "from": was, "to": rel["step"]}
    if now - rel["spied_at"] < SPY_COOL:
        return {"change": 0.0, "lie": False, "from": was, "to": was}
    rel["spied_at"] = now
    change = bump(rel, -LOSS["spied"], now)
    rel["opinion"] = max(-100.0, rel["opinion"] - 2.0)
    return {"change": change, "lie": False, "from": was, "to": was}


def on_cheat(rel: dict, now: float) -> float:
    rel["cheats"] += 1
    rel["opinion"] = max(-100.0, rel["opinion"] - 25.0)
    return bump(rel, -LOSS["cheat"], now)


def on_trade_done(rel: dict, now: float) -> float:
    rel["trades"] += 1
    rel["opinion"] = min(100.0, rel["opinion"] + 5.0)
    return bump(rel, GAIN["trade"], now)


# ---- hard rules ------------------------------------------------------------------------------------------------------------------
def hard_check(rel: dict | None, proposed: str, now: float, persona=None, *, aggressor_recent: bool = False, revenge_open: bool = False,
               check_cool: bool = True, slots_used: int | None = None, pact_info: dict | None = None) -> str | None:
    """Why this step may NOT be proposed or accepted, or None if it may. A language model cannot override this.

    `aggressor_recent`: one of their planets attacked us within 12 h (Memory.aggressors); `revenge_open`: we hold a revenge call or a
    bounty against them. `slots_used`: pact and alliance partners we have now (None = not checked); `pact_info`: what we know of the
    partner for a raider (see could_raid)."""
    if rel is None:
        return "no contact yet"
    if now - rel["betrayed_at"] < BETRAY_COOL:
        return "they betrayed us, not forgotten yet"
    if now - rel["last_hit_at"] < HIT_GRUDGE or aggressor_recent:
        return "attacked us in the last 12 hours"
    if now - rel["robbed_at"] < ROBBED_GRUDGE:
        return "robbed us"
    if revenge_open:
        return "we hold an open revenge call or bounty against them"
    if check_cool and now < rel["cool_until"]:
        return "cool-off after a refusal"
    if proposed != next_step(rel["step"]) or proposed in ("unknown", "contact", "friendly"):
        return f"would skip a step ({rel['step']} -> {proposed})"
    if proposed == "pact" and rel["trust"] < PACT_AT:
        return f"trust {rel['trust']:.0f} is below {PACT_AT:.0f} for a pact"
    if proposed == "pact" and persona is not None:
        if slots_used is not None and slots_used >= pact_slots(persona):
            return "pact slots full"
        if is_raider(persona) and could_raid(persona, pact_info):
            return "looks like a target"
    if proposed == "alliance":
        if rel["trust"] < ALLIANCE_AT:
            return f"trust {rel['trust']:.0f} is below {ALLIANCE_AT:.0f} for an alliance"
        if persona is not None and _num(getattr(persona, "sociability", 0.0), 0.0) < ALLIANCE_MIN_SOCIABILITY:
            return "not sociable enough for an alliance"
    return None


def offer_step(rel: dict, shared_threat: bool) -> str | None:
    """The step this relationship could be offered next (without the hard rules), or None."""
    step = rel["step"]
    if step == "contact":
        return "neutral"
    if step == "friendly" and rel["trust"] >= PACT_AT:
        return "pact"
    if step == "pact" and rel["trust"] >= ALLIANCE_AT and shared_threat:
        return "alliance"
    return None


def decide_rule(rel: dict, proposed: str) -> tuple[str, str]:
    """The deterministic answer when no language model is there (or it failed): accept if trust is enough and opinion not bad."""
    need = {"neutral": -20.0, "pact": 0.0, "alliance": 10.0}.get(proposed)
    if need is None:
        return "REFUSE", "not a step I can take"
    if rel["opinion"] < need:
        return "REFUSE", f"I do not think much of them (opinion {rel['opinion']:.0f})"
    return "ACCEPT", f"trust {rel['trust']:.0f} is enough"


# ---- facts and prompts for the language model --------------------------------------------------------------------------------------
def build_facts(rel: dict, proposed: str, now: float, *, standing_: dict | None = None, my_score=None, their_score=None,
                threats: int = 0, proposer: str = "", answerer: str = "") -> dict:
    s = standing_ or {}
    since_hit = None if not rel["last_hit_at"] else round((now - rel["last_hit_at"]) / 3600, 1)
    return {"proposer": proposer, "answerer": answerer, "proposed": proposed, "step": rel["step"], "trust": round(rel["trust"]), "opinion": round(rel["opinion"]),
            "spied_us": rel["spied_us"], "spy_lies": rel.get("spy_lies", 0), "hit_us": rel["hit_us"], "hours_since_hit": since_hit, "trades": rel["trades"], "cheats": rel["cheats"],
            "betrayals": rel["betrayals"], "ratio": s.get("ratio"), "role": s.get("role", "unknown"), "rank": s.get("rank"),
            "my_rank": s.get("my_rank"), "my_score": my_score, "their_score": their_score, "threats": int(threats)}


def facts_digest(f: dict) -> str:
    """One short line for the log."""
    return (f"trust={f['trust']} op={f['opinion']} step={f['step']} hit={f['hit_us']} spied={f['spied_us']} trades={f['trades']}/{f['cheats']} "
            f"{f['role']} ratio={f['ratio']} rank={f['rank']}/{f['my_rank']} threats={f['threats']}")


PHRASE = {"neutral": "to keep the peace between us", "pact": "a non-aggression pact: neither of us raids the other",
          "alliance": "an alliance against shared enemies"}


def facts_text(f: dict) -> str:
    since = "never" if f["hours_since_hit"] is None else f"{f['hours_since_hit']} hours ago"
    return "\n".join([
        f"FROM: {f['proposer']}", f"TO: {f['answerer']}",
        f"PROPOSAL: {f['proposed']} ({PHRASE.get(f['proposed'], f['proposed'])})",
        f"RELATION NOW: {f['step']}",
        f"TRUST: {f['trust']}", f"OPINION: {f['opinion']}",
        f"SPIED_ON_US: {f['spied_us']} times (while we were friendly: {f.get('spy_lies', 0)})", f"ATTACKED_US: {f['hit_us']} times (last: {since})", f"BETRAYALS: {f['betrayals']}",
        f"TRADES: {f['trades']} completed honestly, {f['cheats']} cheats",
        f"STRENGTH: they are {f['role']} than us (score ratio {f['ratio']}, their rank {f['rank']}, ours {f['my_rank']})",
        f"THREATS: {f['threats']} recent attackers on us from elsewhere"])


def answer_system(sheet: dict) -> str:
    return (f"You are {sheet['name']}, a player in a space strategy game in the style of OGame (callsign {sheet['handle']}). "
            f"BACKSTORY: {sheet['backstory']}\nTONE: {', '.join(sheet['traits'])}\n"
            f"Another player proposes a diplomatic step. Decide in character, but only from the FACTS you are given. "
            f"Your answer must start with exactly one word: ACCEPT, REFUSE or WAIT. Then, optionally, ONE short reason line "
            f"(at most 20 words, plain text, no markup). Never ask questions.")


def answer_prompt(f: dict) -> str:
    return ("TASK: answer\nSomeone proposes a step on the diplomatic ladder (contact, neutral, friendly, pact, alliance). "
            "Their word is cheap: judge by the facts. Reply ACCEPT, REFUSE or WAIT first, then a short reason.\n" + facts_text(f))


def hello_template(me: str, other: str, step: str) -> str:
    if step == "pact":
        return f"{other}, {me} here. Let us sign a pact: neither of us raids the other."
    if step == "alliance":
        return f"{other}, {me} here. Our enemies are shared: let us ally."
    return f"Greetings {other}, this is {me}. Shall we keep the peace between us?"


def hello_prompt(text: str, step: str) -> str:
    return (f"TASK: hello\nYou propose to another player: {PHRASE.get(step, step)}. Say it in your own voice, one line. "
            f"Use your own words and your own temperament (blunt, warm, wary, businesslike...); do NOT start with 'Greetings'. "
            f"This is only an offer: they have not answered, so never say yes, accepted or agreed. "
            f"Keep every name and number exactly as written.\nTEXT: {text}")


def announce_template(me: str, other: str, step: str) -> str:
    return f"{me} and {other} have signed a non-aggression pact." if step == "pact" else f"{me} and {other} are now allies."


def announce_prompt(text: str) -> str:
    return f"TASK: announce\nAnnounce this to the galaxy in your own voice, one line. Keep every name exactly as written.\nTEXT: {text}"


# ---- reading the model's answer ------------------------------------------------------------------------------------------------------
DECISIONS = ("ACCEPT", "REFUSE", "WAIT")
_LEAD = re.compile(r"^[\s\"'`*_#>\-\[\(]+")
_WORD = re.compile(r"[A-Za-z]+")
_MARKUP = re.compile(r"[<>`*#{}\[\]|\\]|https?://|\bwww\.")


def parse_decision(text) -> tuple[str, str]:
    """(decision, reason) from a model's answer. The FIRST word decides, case does not matter; anything else (garbage, empty, a
    sentence that merely mentions accept later) is WAIT. The reason is the rest of the first line or the next line, cleaned, 120 chars."""
    if not isinstance(text, str) or not text.strip():
        return "WAIT", ""
    body = _LEAD.sub("", text.strip())
    m = _WORD.match(body)
    if not m or m.group(0).upper() not in DECISIONS:
        return "WAIT", ""
    decision = m.group(0).upper()
    rest = body[m.end():]
    lines = [ln.strip(" \t\"'`*_:-.,;") for ln in rest.splitlines()]
    reason = next((ln for ln in lines if ln), "")
    reason = re.sub(r"\s+", " ", _MARKUP.sub("", reason)).strip()
    return decision, reason[:120]


def fake_decision(proposer: str, answerer: str, trust: float) -> str:
    """The canned answer of `--llm fake`: always the same for the same names and trust (a hash decides the borderline cases)."""
    h = zlib.crc32(f"{proposer}|{answerer}".encode()) % 100
    score = trust + (h - 50) / 2.0
    if score >= 40:
        return "ACCEPT\nThe numbers look fine to me."
    if score >= 20:
        return "WAIT\nI want to think it over."
    return "REFUSE\nNot yet."


def fake_answer(prompt: str) -> str | None:
    """Understands the prompt of answer_prompt (for llm.fake_answer). None if it is not one."""
    fields = {}
    for line in prompt.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if "TRUST" not in fields:
        return None
    try:
        trust = float(fields["TRUST"])
    except ValueError:
        trust = 0.0
    return fake_decision(fields.get("FROM", ""), fields.get("TO", ""), trust)


# ---- reports -----------------------------------------------------------------------------------------------------------------------------
def relations_from_events(events: list[dict]) -> list[dict]:
    """The latest known relationship of every (agent, other) pair, from the log events contact / diplo_step / diplo_trust, highest
    trust first. Used by the report and the observatory; never raises on broken lines."""
    pairs: dict[tuple[str, str], dict] = {}
    for e in events or []:
        try:
            ev = e.get("event")
            if ev not in ("contact", "diplo_step", "diplo_trust") or not e.get("agent") or e.get("with") is None:
                continue
            r = pairs.setdefault((str(e["agent"]), str(e["with"])), {"agent": str(e["agent"]), "with": str(e["with"]), "step": "contact",
                                                                      "trust": 0.0, "t": 0})
            step = e.get("to") if ev == "diplo_step" else e.get("step") if ev == "diplo_trust" else None
            if step in STEPS:
                r["step"] = step
            if isinstance(e.get("trust"), (int, float)) and not isinstance(e.get("trust"), bool):
                r["trust"] = float(e["trust"])
            r["t"] = e.get("t") or r["t"]
        except Exception:
            continue
    return sorted(pairs.values(), key=lambda r: (-r["trust"], r["agent"], r["with"]))
