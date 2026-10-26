"""Alliances: the rules (pure functions, no network). The network side is alliance_agent.py.

Who founds an alliance, who joins which one, who is let in, what allies tell each other, and when an agent leaves.
Everything depends on the `sociability` of the persona (0 = never, peaceful miners and scientists high, raiders low).

Allies talk in the alliance chat (POST /chat/send with alliance=true), in one fixed short format that is easy to parse:

    INTEL T=1:2:3 O=45 L=12000 D=3 A=120      a spy report: target coordinates, owner id, loot, defenders (? = unknown), age in seconds
    WARN AT=1:3:8 FROM=1:5:5 ETA=300          an attack is on its way to AT, sent from FROM, lands in ETA seconds

Anything else in the chat (a human's words) is ignored. Times are server seconds.
"""
from __future__ import annotations

import re

Coord = tuple[int, int, int]

MAX_MEMBERS = 8            # a leader stops accepting at this size
FOUNDER_BAR = 0.6          # sociability needed to found an alliance
FOUND_MIN_POINTS = 300     # score needed to found one
FOUND_MIN_AGE = 1800.0     # seconds an agent has been playing before it founds
FOUND_CHANCE = 0.35        # per look, so founders do not all start at the same moment
AGENTS_PER_ALLIANCE = 5    # at most one alliance per this many agents (at least one is always allowed)
JOIN_MIN_AGE = 900.0       # seconds before an agent looks for an alliance to join
CHECK_EVERY = 120.0        # seconds between two looks at the alliance (GET /alliance)
APPLY_EVERY = 900.0        # an applicant waits this long for an answer before it tries another alliance
REAPPLY_AFTER = 4 * 3600.0
READ_EVERY = 120.0         # seconds between two reads of the alliance chat
SHARE_GAP = 240.0          # at most one intel message per this many seconds
WARN_GAP = 60.0            # at most one warning per this many seconds
INTEL_SHARE_MAX_AGE = 900.0    # only a report younger than this is worth sharing
INTEL_USE_MAX_AGE = 1200.0     # an ally's report older than this is not used (military.INTEL_MAX_AGE is 1500)
GRUDGE_WINDOW = 24 * 3600.0    # an aggressor is remembered this long when judging who to let in
ROBBED_WINDOW = 6 * 3600.0     # an attack by an ally this recent makes a member leave
IDLE_LEAVE = 12 * 3600.0       # a member leaves an alliance in which nobody has said anything for this long
AD_EVERY = 3600.0              # a leader advertises on the galaxy board at most this often
APPLY_MIN_SCORE = 0.45         # compatibility needed to apply


def new_state() -> dict:
    return {"off": False, "born": 0.0, "next_at": 0.0, "id": None, "tag": None, "founder": False, "joined_at": 0.0,
            "cursor": 0, "read_at": 0.0, "share_at": 0.0, "warn_at": 0.0, "active_at": 0.0, "ad_at": 0.0,
            "cooldown_until": 0.0, "apply_until": 0.0, "seq": 0, "size": 0,
            "applied": {}, "shared": {}, "warned": [], "allies": [], "ally_coords": {}, "attackers": {}, "robbed_by": {}, "handled": []}


def clean_state(raw) -> dict:
    """A state read from a saved memory file: anything missing or malformed goes back to its default."""
    st = new_state()
    if not isinstance(raw, dict):
        return st
    for key, default in new_state().items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            st[key] = bool(value)
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                st[key] = type(default)(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, dict):
            st[key] = {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}
        elif isinstance(default, list):
            st[key] = [x for x in value if isinstance(x, (int, str))][-200:] if isinstance(value, list) else []
        else:   # id, tag: None or a value
            st[key] = value if isinstance(value, (int, str)) else None
    return st


def fmt_coord(c) -> str:
    return "%d:%d:%d" % tuple(c)


def parse_coord(text) -> Coord | None:
    try:
        g, s, p = (int(x) for x in str(text).split(":"))
    except (TypeError, ValueError):
        return None
    return (g, s, p)


# ---- grudges and compatibility --------------------------------------------------------------------------------------
def grudge(opponents: dict, aggressors: dict, coord: Coord | None, now: float, extra_attackers: dict | None = None) -> str | None:
    """Why we do not want the player at `coord`, or None. A raider that attacked us (or, for a leader, any member) lately, or one that
    beat us in a fight of ours (opponents.py: it has beaten us and we are down against it)."""
    if coord is None:
        return None
    if now - aggressors.get(coord, -1e18) < GRUDGE_WINDOW:
        return "attacked us"
    key = fmt_coord(coord)
    if extra_attackers and now - float(extra_attackers.get(key, -1e18)) < GRUDGE_WINDOW:
        return "attacked an ally"
    rec = opponents.get(coord)
    if rec and rec.get("losses", 0) > 0 and rec.get("net", 0) < 0:
        return "beat us in a fight"
    return None


GROUPS = {"miner": "peaceful", "scientist": "peaceful", "turtle": "peaceful", "trader": "peaceful", "balanced": "middle", "raider": "military"}


def similarity(a: str | None, b: str | None) -> float:
    """How well two personalities get along: the same one fully, the same kind well, a raider and a turtle poorly."""
    if a is None or b is None:
        return 0.5
    if a == b:
        return 1.0
    ga, gb = GROUPS.get(a, "middle"), GROUPS.get(b, "middle")
    if ga == gb:
        return 0.75
    if "middle" in (ga, gb):
        return 0.55
    return 0.2


def system_distance(a: Coord, b: Coord) -> int:
    return abs(a[0] - b[0]) * 500 + abs(a[1] - b[1])


def compat(my_persona: str, my_coord: Coord, member_coords: list[Coord], member_personas: list[str | None]) -> float:
    """0..1: how well an alliance suits us, from how close its nearest member lives and how alike the members are to us."""
    if not member_coords:
        return 0.0
    near = min(system_distance(my_coord, c) for c in member_coords)
    closeness = max(0.0, 1.0 - near / 250.0)
    like = sum(similarity(my_persona, p) for p in member_personas) / max(1, len(member_personas)) if member_personas else 0.5
    return round(0.6 * closeness + 0.4 * like, 3)


def pick_alliance(my_persona: str, my_coord: Coord, candidates: list[dict], opponents: dict, aggressors: dict, now: float,
                  bar: float = APPLY_MIN_SCORE) -> tuple[dict, float] | None:
    """The best alliance to apply to. A candidate is {'id', 'member_count', 'open', 'coords': [...], 'personas': [...]}.
    Closed or full ones, and any with a member who attacked us or beat us, are out."""
    best = None
    for cand in candidates:
        if not cand.get("open", True) or cand.get("member_count", 0) >= MAX_MEMBERS:
            continue
        if any(grudge(opponents, aggressors, c, now) for c in cand.get("coords", [])):
            continue
        score = compat(my_persona, my_coord, cand.get("coords", []), cand.get("personas", []))
        if score >= bar and (best is None or score > best[1]):
            best = (cand, score)
    return best


# ---- founding -------------------------------------------------------------------------------------------------------
def founding_cap(agent_count: int) -> int:
    return max(1, agent_count // AGENTS_PER_ALLIANCE)


def can_found(persona, *, points: int, age: float, agent_count: int, alliances: int, now: float, cooldown_until: float) -> tuple[bool, str]:
    if persona.sociability < FOUNDER_BAR:
        return False, "not sociable enough to lead"
    if now < cooldown_until:
        return False, "cooldown"
    if age < FOUND_MIN_AGE:
        return False, "too young"
    if points < FOUND_MIN_POINTS:
        return False, "too few points"
    if alliances >= founding_cap(agent_count):
        return False, "enough alliances already"
    return True, "ok"


PREFIX = {"miner": "MINE", "scientist": "SCI", "turtle": "WALL", "trader": "TRADE", "balanced": "ALLY", "raider": "RAID"}
ADJECTIVE = ("Quiet", "Steady", "Bright", "Northern", "Silver", "Calm", "Iron", "Amber", "Swift", "Lucky")
NOUN = {"miner": "Miners", "scientist": "Scholars", "turtle": "Wardens", "trader": "Traders", "balanced": "Union", "raider": "Wolves"}


def agent_number(name: str) -> int:
    m = re.search(r"(\d+)\s*$", name or "")
    return int(m.group(1)) if m else 0


def make_identity(persona_name: str, number: int, taken_tags, taken_names, attempt: int = 0) -> tuple[str, str]:
    """(tag, name) from the personality and the agent's number, unique among the taken ones. Tag 3-8 characters, name 3-30."""
    prefix = PREFIX.get(persona_name, "ALLY")
    taken_tags = {t.lower() for t in taken_tags}
    taken_names = {n.lower() for n in taken_names}
    adjective = ADJECTIVE[(number + attempt) % len(ADJECTIVE)]
    noun = NOUN.get(persona_name, "Union")
    for extra in range(0, 400):
        n = number + extra + attempt
        tag = f"{prefix}{n % 100:02d}"[:8]
        if extra >= 100:
            tag = f"{prefix[:4]}{n % 1000:03d}"[:8]
        name = f"{adjective} {noun} {n}"[:30]
        if tag.lower() not in taken_tags and name.lower() not in taken_names:
            return tag, name
    return f"{prefix[:3]}{number % 10000:04d}"[:8], f"{adjective} {noun} {number}"[:30]


def application_text(persona_name: str, agent_name: str, coord: Coord) -> str:
    lines = {"peaceful": "Peaceful {p} here, living at {c}. Looking for quiet neighbours to share news with.",
             "middle": "Ordinary {p} at {c}. Happy to trade intel and watch each other's backs.",
             "military": "{p} at {c}. I bring ships and I share what my probes find."}
    group = GROUPS.get(persona_name, "middle")
    return (lines[group].format(p=persona_name, c=fmt_coord(coord)) + f" ({agent_name})")[:200]


# ---- letting people in ----------------------------------------------------------------------------------------------
def decide_application(*, member_count: int, applicant_coord: Coord | None, opponents: dict, aggressors: dict, attackers: dict, now: float) -> tuple[bool, str]:
    """A leader's answer to one application. Never lets in someone who attacked us or any member, or beat us in a fight."""
    if member_count >= MAX_MEMBERS:
        return False, "alliance full"
    why = grudge(opponents, aggressors, applicant_coord, now, attackers)
    if why:
        return False, why
    return True, "ok"


def should_leave(*, is_founder: bool, robbed_by: dict, now: float, joined_at: float, active_at: float, idle_leave: float = IDLE_LEAVE) -> str | None:
    """Why a member should leave, or None. The founder never leaves (the game does not let it)."""
    if is_founder:
        return None
    if any(now - when < ROBBED_WINDOW for when in robbed_by.values()):
        return "robbed by an ally"
    if now - max(joined_at, active_at) > idle_leave:
        return "nobody has said anything for a long time"
    return None


def members_to_kick(is_founder: bool, robbed_by: dict, attackers: dict, ally_coords: dict, now: float) -> list[int]:
    """A founder cannot leave, so a member that robbed it (or, by a warning in the chat, another member) is thrown out instead.
    `ally_coords` maps a member's user id to its coordinates as 'g:s:p'."""
    if not is_founder:
        return []
    out = {int(uid) for uid, when in robbed_by.items() if now - when < ROBBED_WINDOW}
    for uid, key in ally_coords.items():
        if now - float(attackers.get(key, -1e18)) < ROBBED_WINDOW:
            out.add(int(uid))
    return sorted(out)


# ---- what allies tell each other ------------------------------------------------------------------------------------
def fmt_intel(coord: Coord, owner_id, loot: int, defenders, age: float) -> str:
    d = "?" if defenders is None else str(int(defenders))
    o = "" if owner_id is None else f" O={int(owner_id)}"
    return f"INTEL T={fmt_coord(coord)}{o} L={int(loot)} D={d} A={int(age)}"


def fmt_warn(at: Coord, origin: Coord, eta: float) -> str:
    return f"WARN AT={fmt_coord(at)} FROM={fmt_coord(origin)} ETA={int(max(0, eta))}"


def parse_message(text: str) -> dict | None:
    """{'kind': 'INTEL', 'T': coord, 'O': int or None, 'L': int, 'D': int or None, 'A': int} or {'kind': 'WARN', 'AT', 'FROM', 'ETA'}; None for anything else."""
    parts = str(text or "").split()
    if not parts or parts[0] not in ("INTEL", "WARN"):
        return None
    fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    try:
        if parts[0] == "INTEL":
            target = parse_coord(fields.get("T"))
            if target is None:
                return None
            d = fields.get("D", "?")
            return {"kind": "INTEL", "T": target, "O": int(fields["O"]) if "O" in fields else None, "L": max(0, int(fields["L"])),
                    "D": None if d == "?" else max(0, int(d)), "A": max(0, int(fields.get("A", 0)))}
        at, origin = parse_coord(fields.get("AT")), parse_coord(fields.get("FROM"))
        if at is None or origin is None:
            return None
        return {"kind": "WARN", "AT": at, "FROM": origin, "ETA": max(0, int(fields.get("ETA", 0)))}
    except (KeyError, ValueError):
        return None


def may_share(state: dict, now: float) -> bool:
    return now >= float(state.get("share_at", 0.0))


def may_warn(state: dict, now: float) -> bool:
    return now >= float(state.get("warn_at", 0.0))


def pick_intel_to_share(intel: dict, shared: dict, now: float, min_loot: int, ally_ids=(), ally_coords=()) -> tuple[Coord, dict] | None:
    """The best fresh spy report not shared yet: the biggest loot first. Allies' own planets are never shared."""
    best = None
    for c, info in intel.items():
        age = now - float(info.get("at", -1e18))
        if age > INTEL_SHARE_MAX_AGE or info.get("owner_id") in ally_ids or c in ally_coords:
            continue
        if float(shared.get(fmt_coord(c), -1e18)) >= float(info["at"]):    # this very report went out already
            continue
        loot = int(info.get("loot", 0))
        if loot < max(1, min_loot):
            continue
        if best is None or loot > int(best[1].get("loot", 0)):
            best = (c, info)
    return best


def intel_from_chat(messages: list[dict], me: int, now: float, ally_ids, min_loot: int) -> list[tuple[Coord, dict]]:
    """Reports of other allies worth using now: [(coordinates, memory.intel entry)], freshest age first. Needs 'text', 'from_user_id', 'created_at'."""
    found = []
    for m in messages:
        if m.get("from_user_id") == me:
            continue
        p = parse_message(m.get("text"))
        if not p or p["kind"] != "INTEL":
            continue
        sent = float(m.get("created_at") or now)
        age = max(0.0, now - sent) + p["A"]
        if age > INTEL_USE_MAX_AGE or p["L"] < min_loot or p["O"] in ally_ids or p["O"] == me:
            continue
        found.append((p["T"], {"at": now - age, "loot": p["L"], "defenders": p["D"], "owner_id": p["O"], "ships": None, "defense": None,
                               "research": None, "from_ally": m.get("from_user_id")}))
    found.sort(key=lambda t: -t[1]["at"])
    return found


def warnings_from_chat(messages: list[dict], me: int) -> list[dict]:
    """WARN messages of other members."""
    out = []
    for m in messages:
        if m.get("from_user_id") == me:
            continue
        p = parse_message(m.get("text"))
        if p and p["kind"] == "WARN":
            out.append({**p, "from_user_id": m.get("from_user_id")})
    return out
