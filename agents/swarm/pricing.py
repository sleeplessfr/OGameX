"""Learning what to ask for on the galaxy board, and whether an agent can really deliver what it offers.

Pure functions plus one small dict per agent, saved in its memory (Memory.social["prices"], missing in old files = empty).
No network, no clock of its own: times are passed in.

THE IDEA, FOR A NON-PROGRAMMER
A fair swap uses the merchant's usual 3:2:1 reference prices (brain.VALUE: 1 metal = 1, 1 crystal = 1.5, 1 deuterium = 3).
For every pair "I give X, I want Y" the agent keeps ONE number, the MARKUP r:

    r = value of what I ask / value of what I give          (1.0 = exactly fair, 1.10 = I ask 10% too much for a buyer)

It starts at 1.03 (the old fixed 3% margin) and then learns by trial and error, like a shopkeeper adjusting a price tag:

  * nobody answered my offer in half an hour  -> lower r by 8% (a bit more generous), never below 0.7;
  * somebody answered it (or the deal was completed) -> raise r by 3% (I was too cheap), never above 1.3;
  * the highest r that was ever answered is remembered ("best"); after a failure above it the price falls straight back
    to it instead of creeping down in small steps;
  * now and then (1 offer in 4) the posted price is nudged by up to 3% either way ("exploration"), so the agent keeps
    discovering whether a slightly different price sells. The nudge uses the agent's OWN private random stream, so it
    never disturbs the dice the rest of the agent uses.
In maths terms this is a one-dimensional hill climb with a safety fence, or a tiny multi-armed bandit with one arm per
pair: raise while it sells, drop when it does not.

THE BUYING SIDE
When an agent looks for an offer to take, it accepts a post whose r is at most `accept` for that pair. That limit starts at
1 / 0.95 (about 5% worse than fair, the old rule) and loosens by 2% per hour in which it needed something and found no
acceptable offer (cap 1.25); a completed deal tightens it again by 1%. So an agent that needs crystal badly for many hours
slowly stops being picky.

CAN IT DELIVER?
An agent posts or answers an offer only if it has cargo ships at home that carry at least half of the amount, or it is a
persona allowed to build cargos (logistics.BUILDERS) and can order enough small cargos right now (which is what the
sending code will do). Otherwise the offer would only be a promise it could not keep.
"""
from __future__ import annotations

import math
import random


START_RATIO = 1.03          # same as social.ASK_MARGIN: markup r a new pair starts with
MIN_RATIO = 0.7             # never give away more than this (30% under the reference)
MAX_RATIO = 1.3             # never ask more than this
DROP = 0.92                 # an unanswered offer multiplies r by this
RAISE = 1.03                # an answered / completed one multiplies r by this
EXPLORE_CHANCE = 0.25       # share of posts that get a small nudge...
EXPLORE_SPAN = 0.03         # ...of up to plus or minus this
ACCEPT_START = 1 / 0.95     # the answering side takes posts up to this r (social.ACCEPT_FLOOR)
ACCEPT_MAX = 1.25
ACCEPT_STEP = 0.02          # looser by this per hour of unmet need
ACCEPT_EASE = 0.01          # tighter by this after a completed deal
NEED_HOUR = 3600.0
KEEP_PAIRS = 12             # (3 resources -> 6 pairs; a little room)


def pair_key(give: str, want: str) -> str:
    return f"{give}>{want}"


def _num(x, default, lo, hi):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v)) if v == v else default


def clean_prices(raw) -> dict:
    """A saved price book, tolerant of anything missing or malformed."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, e in list(raw.items())[:KEEP_PAIRS]:
        if not isinstance(e, dict) or not isinstance(k, str) or ">" not in k:
            continue
        best = e.get("best")
        out[k] = {"r": _num(e.get("r"), START_RATIO, MIN_RATIO, MAX_RATIO),
                  "start": _num(e.get("start"), START_RATIO, MIN_RATIO, MAX_RATIO),
                  "best": None if best is None else _num(best, START_RATIO, MIN_RATIO, MAX_RATIO),
                  "answered": int(_num(e.get("answered"), 0, 0, 10 ** 6)), "unanswered": int(_num(e.get("unanswered"), 0, 0, 10 ** 6)),
                  "done": int(_num(e.get("done"), 0, 0, 10 ** 6)),
                  "accept": _num(e.get("accept"), ACCEPT_START, ACCEPT_START, ACCEPT_MAX),
                  "need_since": _num(e.get("need_since"), 0.0, 0.0, 1e15), "need_bump": _num(e.get("need_bump"), 0.0, 0.0, 1e15)}
    return out


def entry(prices: dict, give: str, want: str, start: float = START_RATIO) -> dict:
    k = pair_key(give, want)
    e = prices.get(k)
    if e is None:
        e = prices[k] = {"r": start, "start": start, "best": None, "answered": 0, "unanswered": 0, "done": 0,
                         "accept": ACCEPT_START, "need_since": 0.0, "need_bump": 0.0}
        if len(prices) > KEEP_PAIRS:
            del prices[next(iter(prices))]
    return e


def _clip(r: float) -> float:
    return min(MAX_RATIO, max(MIN_RATIO, r))


# ---- asking side --------------------------------------------------------------------------------------------------
def ask_ratio(prices: dict, give: str, want: str, rng: random.Random | None = None) -> float:
    """The markup to put on the next offer for this pair: the learned r, now and then nudged (exploration)."""
    r = entry(prices, give, want)["r"]
    if rng is not None and rng.random() < EXPLORE_CHANCE:
        r = _clip(r * (1 + rng.uniform(-EXPLORE_SPAN, EXPLORE_SPAN)))
    return r


def on_unanswered(prices: dict, give: str, want: str) -> tuple[float, float]:
    """An offer ran its time without a reply: be more generous. Returns (old r, new r)."""
    e = entry(prices, give, want)
    old = e["r"]
    new = old * DROP
    if e["best"] is not None and old > e["best"]:
        new = max(e["best"], new)          # we are above the best known price: fall back to it, not below
    e["r"] = _clip(new)
    e["unanswered"] += 1
    return old, e["r"]


def on_answered(prices: dict, give: str, want: str, posted: float | None = None) -> tuple[float, float]:
    """Somebody answered (`posted` = the markup that offer really had, jitter included): ask a little more next time."""
    e = entry(prices, give, want)
    old = e["r"]
    base = posted if posted is not None else old
    e["best"] = base if e["best"] is None else max(e["best"], base)
    e["r"] = _clip(max(old, base) * RAISE)
    e["answered"] += 1
    return old, e["r"]


def on_done(prices: dict, give: str, want: str) -> tuple[float, float]:
    """A deal at this price was completed: the price is good; ask a little more."""
    e = entry(prices, give, want)
    old = e["r"]
    e["r"] = _clip(old * RAISE)
    e["best"] = max(e["best"] or 0.0, old)
    e["done"] += 1
    return old, e["r"]


# ---- answering side -----------------------------------------------------------------------------------------------
def accept_ratio(prices: dict | None, give: str, want: str) -> float:
    """The highest markup (of a poster who gives `give` for `want`) we accept."""
    if not prices:
        return ACCEPT_START
    e = prices.get(pair_key(give, want))
    return e["accept"] if e else ACCEPT_START


def on_need_unmet(prices: dict, give: str, want: str, now: float) -> tuple[float, float] | None:
    """We needed `give` and found no acceptable post. Each full hour of that loosens the limit a little."""
    e = entry(prices, give, want)
    if not e["need_since"]:
        e["need_since"] = e["need_bump"] = now
        return None
    if now - e["need_bump"] < NEED_HOUR or e["accept"] >= ACCEPT_MAX:
        return None
    old = e["accept"]
    e["accept"] = min(ACCEPT_MAX, old + ACCEPT_STEP)
    e["need_bump"] = now
    return old, e["accept"]


def on_need_met(prices: dict, give: str, want: str) -> None:
    e = prices.get(pair_key(give, want))
    if e:
        e["need_since"] = e["need_bump"] = 0.0


def on_accept_done(prices: dict, give: str, want: str) -> tuple[float, float] | None:
    e = prices.get(pair_key(give, want))
    if not e or e["accept"] <= ACCEPT_START:
        return None
    old = e["accept"]
    e["accept"] = max(ACCEPT_START, old - ACCEPT_EASE)
    return old, e["accept"]


# ---- can it deliver? ----------------------------------------------------------------------------------------------
def deliverable(persona, units: list[dict], stock: dict, ships_here: dict, stats: dict, amount: int) -> bool:
    """True if cargo ships at home carry at least half of `amount`, or a builder persona will be able to order enough."""
    from . import logistics as L          # (imported here: logistics -> military -> social -> pricing would be a circle)
    need = max(1, math.ceil(amount / 2))
    _, capacity = L.pick_cargo(ships_here, stats, need)
    if capacity >= need:
        return True
    build = L.choose_cargo_build(persona, units, stock, ships_here)
    if build is None:
        return False
    return capacity + build[1] * (L.cap_of(stats, build[0]) or 5000) >= need
