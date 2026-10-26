"""Alliances: the rules (pure functions, no network). The network side is alliance_agent.py.

Who founds an alliance, who joins which one, who is let in, what allies tell each other, and when an agent leaves.
Everything depends on the `sociability` of the persona (0 = never, peaceful miners and scientists high, raiders low).

Allies talk in the alliance chat (POST /chat/send with alliance=true), in one fixed short format that is easy to parse:

    INTEL T=1:2:3 O=45 L=12000 D=3 A=120      a spy report: target coordinates, owner id, loot, defenders (? = unknown), age in seconds
    WARN AT=1:3:8 FROM=1:5:5 ETA=300          an attack is on its way to AT, sent from FROM, lands in ETA seconds

Anything else in the chat (a human's words) is ignored. Times are server seconds.
"""
from __future__ import annotations

import random
import re
import zlib

Coord = tuple[int, int, int]

MAX_MEMBERS = 8            # the size of an alliance whose purpose is not known
# A leader stops accepting at a size that depends on what the alliance is for: a few big peaceful ones and small raiding bands.
MAX_BY_PURPOSE = {"mining": 12, "research": 12, "defense": 12, "trade": 10, "mixed": 8, "raiding": 5}
ABSOLUTE_MAX = max(MAX_BY_PURPOSE.values())


def max_members(purpose: str | None) -> int:
    return MAX_BY_PURPOSE.get(purpose or "", MAX_MEMBERS)
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
            "cooldown_until": 0.0, "apply_until": 0.0, "seq": 0, "size": 0, "charter": "", "purpose": "", "tone": "",
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
        if not cand.get("open", True) or cand.get("member_count", 0) >= max_members(cand.get("purpose")):
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


# ---- names: what the alliance wants (purpose) and how it says so (tone) ---------------------------------------------
# The purpose comes from the founder's personality, the tone is drawn from the founding (crc32 of agent name and attempt), so the
# galaxy has cartels, walls and fleets with funny, menacing, solemn, bureaucratic ... names. The name never mentions the founder.
PURPOSE_OF = {"miner": "mining", "scientist": "research", "turtle": "defense", "trader": "trade", "raider": "raiding", "balanced": "mixed"}
PURPOSE_WORDS = {      # how the language model is told what the alliance is for
    "mining": "a cartel of miners who want steady production and plenty of ore",
    "research": "a circle of scientists who want knowledge and new technology",
    "defense": "defenders who want walls, safety and peace",
    "trade": "a guild of traders who want fair deals and a busy market",
    "raiding": "a fleet of raiders who want loot and fear",
    "mixed": "a balanced company of players who want a bit of everything",
}
TONE_WORDS = {
    "funny": "funny, a joke you would smile at", "edgy": "edgy and rebellious", "menacing": "menacing and ominous",
    "solemn": "solemn, with gravitas, like an old order", "bureaucratic": "bureaucratic, like a dull official body",
    "poetic": "poetic and gentle", "mysterious": "mysterious and secretive", "absurd": "absurd and surreal",
    "pompous": "pompous and self-important", "laconic": "laconic, as short and dry as possible",
    "heroic": "heroic, like a band of champions", "folksy": "folksy and homely, like a village club",
}
TONES = tuple(TONE_WORDS)

NOUNS = {   # who they are (plural), what they care about (one thing) and what they do (-ing)
    "mining": (("Miners", "Diggers", "Prospectors", "Drillers", "Smelters", "Delvers", "Haulers", "Quarrymen"),
               ("Ore", "Metal", "Crystal", "Deuterium", "Rock", "Slag", "Shaft", "Lode"),
               ("Digging", "Drilling", "Smelting", "Hauling", "Prospecting", "Mining")),
    "defense": (("Wardens", "Sentinels", "Bulwarks", "Shieldwalls", "Garrisons", "Guardians", "Bastions", "Watchmen"),
                ("Wall", "Bastion", "Shield", "Rampart", "Fortress", "Peace", "Gate", "Watch"),
                ("Holding", "Guarding", "Watching", "Enduring", "Sheltering", "Standing")),
    "raiding": (("Reavers", "Raiders", "Corsairs", "Marauders", "Wolves", "Vultures", "Plunderers", "Looters"),
                ("Loot", "Plunder", "Spoils", "Tide", "Blade", "Hunt", "Raid", "Pillage"),
                ("Raiding", "Plundering", "Hunting", "Looting", "Striking", "Reaving")),
    "research": (("Scholars", "Savants", "Tinkerers", "Theorists", "Alchemists", "Observers", "Lamplighters", "Cartographers"),
                 ("Reason", "Theory", "Wonder", "Insight", "Proof", "Lens", "Method", "Inquiry"),
                 ("Learning", "Wondering", "Testing", "Measuring", "Pondering", "Inquiring")),
    "trade": (("Traders", "Brokers", "Merchants", "Factors", "Hagglers", "Chandlers", "Peddlers", "Syndics"),
              ("Exchange", "Trade", "Market", "Ledger", "Contract", "Tariff", "Bazaar", "Commerce"),
              ("Trading", "Haggling", "Brokering", "Bartering", "Dealing", "Auditing")),
    "mixed": (("Union", "Compact", "Concord", "Fellows", "Company", "League", "Assembly", "Circle"),
              ("Balance", "Accord", "Fortune", "Harbor", "Horizon", "Common Cause", "Middle Way", "Fair Share"),
              ("Sharing", "Wandering", "Building", "Gathering", "Settling", "Trying")),
}
# per tone: adjectives {a} and patterns; {n} = group noun, {t} = the thing they care about, {d} = what they do, {k} = a number 1-99
TONE_STYLE = {
    "funny": (("Questionable", "Accidental", "Overcaffeinated", "Mildly Cursed", "Slightly Damp", "Chaotic", "Sleepy", "Unlicensed"),
              ("{n} Anonymous", "{a} {n}", "Ministry of Silly {t}", "{n} and Sons", "Just {d} Honestly", "Probably {t}", "{t} Happens", "Send {t} Not Questions")),
    "edgy": (("Rogue", "Savage", "Feral", "Lawless", "Unholy", "Rusted", "Burnt", "Black"),
             ("{a} {n}", "{n} of the Void", "No Mercy No {t}", "{t} or Death", "Dead {n} Society", "Hell Bent on {t}", "{a} {t} Syndicate")),
    "menacing": (("Red", "Crimson", "Silent", "Iron", "Hollow", "Grim", "Black", "Cold"),
                 ("{a} {n}", "The {a} {t}", "{n} of the {a} Hour", "Beware the {n}", "{a} {t} Pact", "Night of the {n}", "{n} Unleashed")),
    "solemn": (("Long", "Last", "Eternal", "Ancient", "Sacred", "Faithful", "Quiet", "Enduring"),
               ("The {a} {t}", "Order of the {a} {t}", "The {a} {n}", "Keepers of {t}", "{n} of the Covenant", "Oath of {t}", "The {a} Vigil")),
    "bureaucratic": (("Interstellar", "Federal", "Regional", "Provisional", "Standing", "Joint", "Central", "Municipal"),
                     ("{a} {t} Committee", "Bureau of {t}", "{a} {n} Directorate", "Dept of {t} Div {k}", "{t} Commission No {k}", "Office of {d}",
                      "{a} {t} Authority", "Interstellar {t} Committee Dept {k}")),
    "poetic": (("Quiet", "Silver", "Gentle", "Distant", "Amber", "Fading", "Last", "Tender"),
               ("Lanterns of {a} {t}", "Songs of {t}", "The {a} {t}", "Whispering {n}", "{n} of Distant Light", "Where {t} Sleeps", "{a} {n} of Dusk")),
    "mysterious": (("Veiled", "Hidden", "Unseen", "Nameless", "Umbral", "Occult", "Sealed", "Silent"),
                   ("The {a} {n}", "Order of the {a} {t}", "{t} Cipher", "The Unnamed {n}", "Whispers in {t}", "{a} {t}", "The {t} Rumour")),
    "absurd": (("Inflatable", "Sentient", "Flammable", "Fermented", "Upside-Down", "Sideways", "Wobbly", "Cubic"),
               ("{a} {n}", "Society for {a} {t}", "{t} Goose Collective", "Church of the {a} {t}", "Seven {a} {n}", "Hats for {t}", "Friends of Moist {t}")),
    "pompous": (("Magnificent", "Illustrious", "Supreme", "Grand", "Sovereign", "Eminent", "Glorious", "Exalted"),
                ("The Most {a} {n}", "Grand {t} Dominion", "His Majesty's {n}", "{a} {n} Supreme", "Imperial {t} Society", "{n} Triumphant",
                 "The {a} Order of {t}")),
    "laconic": (("Dry", "Plain", "Grim", "Flat", "Lean", "Short", "Blunt", "Stark"),
                ("Just {t}", "{n}", "{t} Co", "{a} {n}", "No Fuss {t}", "Mere {n}", "{n} Basically", "{t} Only")),
    "heroic": (("Valiant", "Bold", "Dauntless", "Radiant", "Unbroken", "Stalwart", "Noble", "Gallant"),
               ("{a} {n}", "Heroes of {t}", "{n} of Glory", "The {a} {t} Brigade", "Champions of {t}", "Rise of the {n}", "{a} {t} Legion")),
    "folksy": (("Friendly", "Honest", "Humble", "Cosy", "Neighbourly", "Old-Fashioned", "Plain", "Cheerful"),
               ("{a} {n}", "{n} Social Club", "Friends of {t}", "Down-Home {t}", "{a} Folks of {t}", "The {t} Barn Dance", "Uncle Joe's {t}")),
}
TAG_SKIP = {"the", "of", "for", "and", "a", "an", "to", "in", "on", "no", "or", "at", "by", "his", "with", "from", "just", "most", "no", "co", "dept"}
NAME_OK = re.compile(r"^[A-Za-z0-9 '.\-]+$")
TAG_OK = re.compile(r"^[A-Za-z0-9]{3,8}$")
NAME_MIN, NAME_MAX, TAG_MIN, TAG_MAX = 3, 30, 3, 8


def purpose_for(persona) -> str:
    """What the alliance of this founder wants: one of PURPOSE_WORDS. From the persona name (see GROUPS), else from its traits."""
    name = persona if isinstance(persona, str) else getattr(persona, "name", "")
    if name in PURPOSE_OF:
        return PURPOSE_OF[name]
    if isinstance(persona, str):
        return "mixed"
    if getattr(persona, "aggression", 0.0) >= 0.6:
        return "raiding"
    if getattr(persona, "trade_interest", 0.0) >= 0.7:
        return "trade"
    if getattr(persona, "research_share", 0.0) >= 0.9 and getattr(persona, "aggression", 0.0) < 0.2:
        return "research"
    if getattr(persona, "caution", 0.0) >= 0.8:
        return "defense"
    return "mixed"


def pick_tone(agent_name: str, attempt: int = 0) -> str:
    return TONES[zlib.crc32(f"{agent_name}|{attempt}|tone".encode()) % len(TONES)]


def fit_name(text: str) -> str:
    """A name of at most 30 characters, cut at a word boundary, only allowed characters."""
    text = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 '.\-]", " ", str(text))).strip()
    words = text.split()
    while len(" ".join(words)) > NAME_MAX and len(words) > 1:
        words.pop()
        while len(words) > 1 and words[-1].lower() in TAG_SKIP:
            words.pop()
    return " ".join(words)[:NAME_MAX].strip(" .-'")


def make_tag(name: str, taken_tags=()) -> str:
    """A tag of 3-8 uppercase letters and digits from the name (initials first), unique among the taken ones (case-insensitive)."""
    taken = {t.lower() for t in taken_tags}
    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    sig = [w for w in words if w.lower() not in TAG_SKIP] or words or ["ALLY"]
    cands = ["".join(w[0] for w in sig)[:6], "".join(w[:2] for w in sig)[:6], sig[0][:3] + "".join(w[:2] for w in sig[1:])[:5],
             sig[0][:4], sig[0][:5], "".join(sig)[:8], "".join(words)[:8], sig[0][:8]]
    for c in cands:
        if TAG_MIN <= len(c) <= TAG_MAX and c.lower() not in taken:
            return c
    base = (next((c for c in cands if len(c) >= TAG_MIN), "") or (cands[-1] + "ALLY"))[:6]
    for i in range(2, 100000):
        c = f"{base[:TAG_MAX - len(str(i))]}{i}"
        if c.lower() not in taken:
            return c
    return "ALLY0000"


def make_name(purpose: str, tone: str, seed: str, attempt: int = 0) -> str:
    """One name for (purpose, tone): deterministic from the seed and the attempt number. 3-30 characters."""
    rng = random.Random(zlib.crc32(f"{seed}|{purpose}|{tone}|{attempt}".encode()))
    nouns, things, deeds = NOUNS.get(purpose, NOUNS["mixed"])
    adjectives, patterns = TONE_STYLE.get(tone, TONE_STYLE["laconic"])
    pattern = rng.choice(patterns)
    return fit_name(pattern.format(a=rng.choice(adjectives), n=rng.choice(nouns), t=rng.choice(things), d=rng.choice(deeds), k=rng.randint(1, 99)))


def fallback_identity(purpose: str, tone: str, seed: str, taken_tags, taken_names, attempt: int = 0) -> tuple[str, str]:
    """(tag, name) with no language model: rich, deterministic, unique among the taken ones, name 3-30 and tag 3-8 characters."""
    taken_names = {n.lower() for n in taken_names}
    name = ""
    for extra in range(80):
        name = make_name(purpose, tone, seed, attempt * 100 + extra)
        if len(name) >= NAME_MIN and name.lower() not in taken_names:
            break
    else:
        base = make_name(purpose, tone, seed, attempt * 100)
        for n in range(2, 100000):
            name = fit_name(f"{base[:NAME_MAX - len(str(n)) - 1]} {n}")
            if name.lower() not in taken_names:
                break
    return make_tag(name, taken_tags), name


# ---- naming with the language model (llm prompt, parse, check) -----------------------------------------------------------
CHARTER_MAX = 150
CHARTER_MIN = 12
AIMS = {   # what the alliance wants and does, plain words; the founder's charter when no model writes one
    "mining": ("We dig for steady ore and welcome miners who share the load.", "Our mines never sleep; bring a pickaxe and a plan.",
               "A cartel for steady production: more metal, fewer wars.", "We want full storage and quiet skies for the diggers."),
    "defense": ("We build walls and keep the peace; defenders and quiet neighbours are welcome.", "Our aim is safety: strong defences and no raids on friends.",
                "We stand together behind high walls and share every warning.", "We want peace, and we make it expensive to break."),
    "raiding": ("We hunt loot together and share what our probes find.", "Our fleets strike weak targets; raiders with ships are welcome.",
                "We want spoils and a feared name; bring your fleet.", "A raiding fleet that shares targets and splits the plunder."),
    "research": ("We chase knowledge and new technology; scholars and tinkerers are welcome.", "Our aim is research: every lab and every idea counts.",
                 "We share what we learn and want a lead in technology.", "A circle of scientists who want better drives and sharper minds."),
    "trade": ("We trade fairly and often; merchants and dealmakers are welcome.", "Our aim is a busy market and honest deals between friends.",
              "A guild for traders who want good prices and safe routes.", "We swap resources, share news and punish no one but cheats."),
    "mixed": ("We do a bit of everything and look after each other.", "A balanced company: mine, trade, fight when we must, and share news.",
              "We want steady growth and good neighbours.", "Open to anyone who plays fair and shares intel."),
}
CALLS = {   # a closing line in the tone
    "funny": "Snacks may be provided.", "edgy": "Weak ones need not apply.", "menacing": "Cross us and see what follows.",
    "solemn": "We keep our oaths.", "bureaucratic": "Apply in triplicate.", "poetic": "Come as you are, like dusk.",
    "mysterious": "You will be found.", "absurd": "Bring a sentient hat.", "pompous": "Join the finest of all alliances.",
    "laconic": "Apply.", "heroic": "Stand with us!", "folksy": "Pull up a chair, friend.",
}
NAME_SYSTEM = ("You invent names for alliances in a space strategy game. You are given what the alliance wants and a tone. "
               "Answer with exactly ONE line: NAME | TAG | CHARTER. NAME is 3 to 30 characters (letters, digits, spaces, apostrophes, hyphens, periods), "
               "TAG is 3 to 8 letters or digits, CHARTER is one or two plain sentences (at most 150 characters) saying what the alliance wants "
               "and who it welcomes. No quotes, no markup, no explanations. Never reuse a taken name or tag.")
_BAD_NAME = re.compile(r"\bagent\s*-?\d+|\bagent\d+|\bplayer\s*\d+|\buser\s*\d+|\bbot\b|\bAI\b", re.I)
_CHARTER_BAD = re.compile(r"[<>`*#{}\[\]|\\]|https?://|\bwww\.|\bagent\s*-?\d+|\bagent\d+", re.I)


def fallback_charter(purpose: str, tone: str, seed: str, attempt: int = 0) -> str:
    """A short charter with no language model: the purpose's aim and a closing line in the tone. At most CHARTER_MAX characters."""
    rng = random.Random(zlib.crc32(f"{seed}|{purpose}|{tone}|{attempt}|charter".encode()))
    text = f"{rng.choice(AIMS.get(purpose, AIMS['mixed']))} {CALLS.get(tone, '')}".strip()
    return text if len(text) <= CHARTER_MAX else text[:CHARTER_MAX].rsplit(" ", 1)[0].rstrip(" ,;")


def clean_charter(text) -> str | None:
    """A charter text if it may be used (12-150 characters, one line, no markup or links, no agent names), else None."""
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text.strip().strip("\"'`*_\u201c\u201d ").strip())
    if not (CHARTER_MIN <= len(t) <= CHARTER_MAX) or _CHARTER_BAD.search(t) or not re.search(r"[A-Za-z]{3}", t):
        return None
    return t


def name_prompt(purpose: str, tone: str, taken_tags, taken_names) -> str:
    """The naming request. Says what the alliance is for and the tone, never who founds it."""
    names = "; ".join(sorted(taken_names)[:40]) or "none"
    tags = ", ".join(sorted(taken_tags)[:40]) or "none"
    return (f"TASK: alliance_name\nKEY: {purpose}/{tone}\nPURPOSE: {PURPOSE_WORDS.get(purpose, PURPOSE_WORDS['mixed'])}\n"
            f"TONE: {TONE_WORDS.get(tone, tone)}\nTAKEN NAMES: {names}\nTAKEN TAGS: {tags}\n"
            f"Invent one new alliance name that shows what the alliance wants, in that tone, a short tag, and a charter in the same tone.\n"
            f"FORMAT: NAME | TAG | CHARTER")


def _unwrap(text: str) -> str:
    return text.strip().strip("\"'`*_\u201c\u201d ").strip()


def parse_name_answer(answer, taken_tags=(), taken_names=()) -> tuple[str, str, str | None] | None:
    """(tag, name, charter or None) from a model answer 'NAME | TAG | CHARTER', or None. Tolerates extra lines, quotes, 'Name:' labels and
    a lowercase tag; the first line that has a valid name and tag wins; a missing or bad charter gives None for it (the caller then uses
    fallback_charter). Checked: name 3-30 allowed characters, no markup or agent-like words, not taken; tag 3-8 letters or digits, not
    taken (case-insensitive)."""
    if not isinstance(answer, str):
        return None
    tags = {t.lower() for t in taken_tags}
    names = {n.lower() for n in taken_names}
    for line in answer.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name = _unwrap(re.sub(r"^\W*(name)\s*[:=-]\s*", "", parts[0].strip(), flags=re.I))
        tag = _unwrap(re.sub(r"^\W*(tag)\s*[:=-]\s*", "", parts[1].strip(), flags=re.I))
        if not (NAME_MIN <= len(name) <= NAME_MAX and NAME_OK.match(name)) or not re.search(r"[A-Za-z]{2}", name) or _BAD_NAME.search(name):
            continue
        if not TAG_OK.match(tag) or tag.lower() in tags or name.lower() in names or name.lower() == tag.lower():
            continue
        charter = clean_charter(re.sub(r"^\W*(charter)\s*[:=-]\s*", "", parts[2].strip(), flags=re.I)) if len(parts) == 3 else None
        return tag.upper(), re.sub(r"\s+", " ", name), charter
    return None


def _fields(prompt: str) -> dict:
    fields = {}
    for line in str(prompt).splitlines():
        key, _, value = line.partition(":")
        fields.setdefault(key.strip(), value.strip())
    return fields


def fake_name_answer(prompt: str) -> str | None:
    """The canned model answer for `--llm fake`: a valid, repeatable 'NAME | TAG | CHARTER' made from the prompt (rule names)."""
    fields = _fields(prompt)
    purpose, _, tone = fields.get("KEY", "mixed/laconic").partition("/")
    split = lambda v: [x.strip() for x in re.split(r"[;,]", v) if x.strip() and x.strip() != "none"]
    seed = str(zlib.crc32(prompt.encode()))
    tag, name = fallback_identity(purpose, tone, seed, split(fields.get("TAKEN TAGS", "")), split(fields.get("TAKEN NAMES", "")))
    return f"{name} | {tag} | {fallback_charter(purpose, tone, seed)}"


# ---- choosing an alliance with the language model ----------------------------------------------------------------------------------
def choice_system(sheet: dict) -> str:
    return (f"You are {sheet['name']}, a player in a space strategy game in the style of OGame (callsign {sheet['handle']}). "
            f"BACKSTORY: {sheet['backstory']}\nTONE: {', '.join(sheet['traits'])}\n"
            f"An alliance is recruiting. Decide in character, but only from the FACTS you are given, whether to apply to it. "
            f"Your answer must start with exactly one word: ACCEPT (apply), REFUSE or WAIT. Then, optionally, ONE short reason line "
            f"(at most 20 words, plain text, no markup). Never ask questions.")


def choice_prompt(facts: dict) -> str:
    lines = [f"{k}: {v}" for k, v in facts.items() if v not in (None, "")]
    return ("TASK: alliance_choice\nAn alliance asks for members. Its charter says what it wants. The rules already checked that it is open, "
            "has room, and that no member attacked you. Reply ACCEPT to apply, REFUSE or WAIT first, then a short reason.\n" + "\n".join(lines))


def choice_facts(*, name: str, tag: str, charter: str | None, purpose: str | None, members: int, fit: float, traits: list[str],
                 persona_name: str) -> dict:
    return {"ALLIANCE": f"{name} [{tag}]", "CHARTER": charter or "unknown (no advert found)",
            "ITS PURPOSE": PURPOSE_WORDS.get(purpose or "", "") or "unknown", "MEMBERS": f"{members} of {max_members(purpose)}",
            "FIT": f"{fit:.2f} (0 = poor, 1 = ideal; from distance and like-minded members)", "ME": f"a {persona_name}, {', '.join(traits)}",
            "RULES": "open, room left, no attacker among its members"}


def fake_choice_answer(prompt: str) -> str | None:
    """The canned answer for `--llm fake`: ACCEPT when the fit is decent, else REFUSE; always the same for the same prompt."""
    fields = _fields(prompt)
    try:
        fit = float(fields.get("FIT", "0.5").split()[0])
    except (ValueError, IndexError):
        fit = 0.5
    h = zlib.crc32(prompt.encode()) % 100
    if fit + (h - 50) / 200.0 >= 0.45:
        return "ACCEPT\nThe charter suits me."
    return "REFUSE\nNot the right company for me."


def parse_choice(answer) -> tuple[str, str]:
    """(ACCEPT|REFUSE|WAIT, reason) from a model's answer: the first word decides, anything else is WAIT."""
    from . import diplomacy
    return diplomacy.parse_decision(answer)


def application_text(persona_name: str, agent_name: str, coord: Coord) -> str:
    lines = {"peaceful": "Peaceful {p} here, living at {c}. Looking for quiet neighbours to share news with.",
             "middle": "Ordinary {p} at {c}. Happy to trade intel and watch each other's backs.",
             "military": "{p} at {c}. I bring ships and I share what my probes find."}
    group = GROUPS.get(persona_name, "middle")
    return (lines[group].format(p=persona_name, c=fmt_coord(coord)) + f" ({agent_name})")[:200]


# ---- letting people in ----------------------------------------------------------------------------------------------
def decide_application(*, member_count: int, applicant_coord: Coord | None, opponents: dict, aggressors: dict, attackers: dict, now: float, limit: int = MAX_MEMBERS) -> tuple[bool, str]:
    """A leader's answer to one application. Never lets in someone who attacked us or any member, or beat us in a fight."""
    if member_count >= limit:
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
