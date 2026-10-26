"""Moons: building a sensor phalanx and reading what it shows. Pure functions, no network (moons_agent.py does the calls).

A moon has no mines, so everything it needs is carried there from the planet it belongs to. The sensor phalanx
(`POST /planets/{moon}/phalanx`, 5,000 deuterium a scan from the moon's own stock) lists the fleets flying to a rival planet
in range and the predicted return flights of fleets that left it. That gives two things:

  WARNING   a rival's return flight whose origin is one of our planets is an attack on its way to us: when it lands is
            `departure_time` (the moment it reaches its target). Fed into the threat / fleet-saving flow as an extra Attack.
  RAIDING   a rival planet whose fleets are out has fewer defenders at home: its target score is raised a little (away_factor).

Scans cost deuterium, so they are capped per hour, and who gets scanned is ordered: known aggressors first, then the targets a
raider is about to hit, then other players in range.
"""
from __future__ import annotations

from .brain import RESOURCES, VALUE
from .combat import harmless_fleet, unit_value

SCAN_COST = 5000            # deuterium per scan, on the moon (the game's price)
SYSTEMS = 499               # systems per galaxy (a galaxy wraps around)
SCAN_GAP = 180.0            # server seconds between two scans by one moon...
SCAN_GAP_ALERT = 90.0       # ...shorter while a hostile fleet is about or after a recent raid
MAX_SCANS_HOUR = {"war": 6, "peace": 3}   # scans per rolling hour (military personas / the rest)
RESCAN_AFTER = {"aggressor": 600.0, "target": 900.0, "other": 3600.0}   # server seconds before the same planet is scanned again
NOGO_SECONDS = 6 * 3600.0   # a planet the game refused to scan (out of range, moon, ...) is left alone this long
RICH_VALUE = 400_000        # a non-military agent builds a phalanx only when a planet holds this much (metal-equivalents)
MILITARY_LEVEL, CIVIL_LEVEL = 3, 2   # phalanx level wanted (range 8 / 3 systems)
FEED_RESERVE = 0.3          # the planet that feeds the moon keeps at least this share of each resource
FEED_COOLDOWN = 300.0       # at least this long between two deliveries to one moon
WARN_KEEP = 120.0           # a warning stays this long after the landing time (the report may be a little late)
AWAY_MAX_SHARE = 0.3        # most a rival's absent fleet can raise the worth of a raid: +30%
AWAY_MIN_LEAD = 60.0        # the fleet must stay away at least this much longer for it to count


def new_state() -> dict:
    """Moon memory (Memory.moons). Every key is optional: a missing one means 'nothing yet'."""
    return {"seen": [], "levels": {}, "scans": [], "scanned": {}, "nogo": {}, "warn": {}, "away": {}, "feed_until": {},
            "build_next": {}, "scan_next": 0.0, "off": False}


def clean_state(data) -> dict:
    """Tolerant loader for a saved state."""
    st = new_state()
    if not isinstance(data, dict):
        return st
    for key in ("levels", "scanned", "nogo", "warn", "away", "feed_until", "build_next"):
        if isinstance(data.get(key), dict):
            st[key] = data[key]
    for key in ("seen", "scans"):
        if isinstance(data.get(key), list):
            st[key] = data[key]
    try:
        st["scan_next"] = float(data.get("scan_next", 0.0))
    except (TypeError, ValueError):
        pass
    st["off"] = bool(data.get("off", False))
    return st


def key_of(coord) -> str:
    return ":".join(str(int(x)) for x in coord)


def coord_from(d) -> tuple[int, int, int]:
    return (int(d.get("galaxy") or 0), int(d.get("system") or 0), int(d.get("position") or 0))


# ---- who wants a phalanx ---------------------------------------------------------------------------------------
def is_military(persona) -> bool:
    return persona.aggression >= 0.25 or persona.character_class == "general"


def stock_value(res: dict) -> float:
    return sum(float(res.get(r, 0) or 0) * VALUE[r] for r in RESOURCES)


def wanted_level(persona, richest_stock: float) -> int:
    """Phalanx level this agent wants: fighters (raider, balanced, general class) always; the others only when rich."""
    if is_military(persona):
        return MILITARY_LEVEL
    return CIVIL_LEVEL if richest_stock >= RICH_VALUE else 0


def choose_moon_build(persona, buildings: list[dict], richest_stock: float) -> dict | None:
    """The building to queue on the moon: the sensor phalanx, or the lunar base first when the phalanx cannot be built
    yet (requirement) or the moon is full. `buildings` is GET /planets/{moon}/buildings. None = nothing wanted."""
    by_name = {b["machine_name"]: b for b in buildings}
    phalanx, base = by_name.get("sensor_phalanx"), by_name.get("lunar_base")
    want = wanted_level(persona, richest_stock)
    if phalanx is None or want <= 0 or int(phalanx.get("level", 0)) >= want:
        return None
    if phalanx.get("requirements_met", True):
        return phalanx
    if base is not None and base.get("requirements_met", True):
        return base
    return None


def shortage(item: dict, stock: dict) -> dict[str, int]:
    """What the moon lacks to pay for `item` (a row of the buildings list)."""
    cost = item.get("cost") or {}
    return {r: max(0, int(cost.get(r, 0) or 0) - int(stock.get(r, 0) or 0)) for r in RESOURCES if int(cost.get(r, 0) or 0) > int(stock.get(r, 0) or 0)}


def scan_shortage(stock: dict) -> dict[str, int]:
    """Deuterium to bring so the moon can pay two scans."""
    have = int(stock.get("deuterium", 0) or 0)
    return {"deuterium": 2 * SCAN_COST - have} if have < SCAN_COST else {}


def feed_amounts(src: dict, need: dict, fuel: int, capacity: int) -> dict[str, int] | None:
    """What to carry to the moon: all of `need`, or None if the source cannot spare it (reserve and fuel kept) or the ships cannot carry it."""
    out = {}
    for r, v in need.items():
        spare = int(int(src.get(r, 0) or 0) * (1 - FEED_RESERVE))
        if r == "deuterium":
            spare = min(spare, int(src.get(r, 0) or 0) - fuel)
        if v > spare:
            return None
        out[r] = int(v)
    if not out or sum(out.values()) > capacity:
        return None
    return out


# ---- scanning ------------------------------------------------------------------------------------------------
def system_distance(a: int, b: int) -> int:
    d = abs(int(a) - int(b))
    return min(d, SYSTEMS - d)


def scan_range(level: int, bonus: float = 1.0) -> int:
    return int((int(level) * int(level) - 1) * bonus)


def in_range(moon: tuple, level: int, target: tuple, bonus: float = 1.0) -> bool:
    return level > 0 and moon[0] == target[0] and system_distance(moon[1], target[1]) <= scan_range(level, bonus)


def hourly_cap(persona) -> int:
    return MAX_SCANS_HOUR["war" if is_military(persona) else "peace"]


def scan_gap(alert: bool) -> float:
    return SCAN_GAP_ALERT if alert else SCAN_GAP


def may_scan(persona, st: dict, now: float, deuterium: float, alert: bool = False) -> tuple[bool, str]:
    """(ok, why not): a scan is due, the moon can pay, and the hourly budget is not used up."""
    if now < float(st.get("scan_next", 0.0)):
        return False, "not due yet"
    if deuterium < SCAN_COST:
        return False, f"moon has {int(deuterium)} deuterium, a scan costs {SCAN_COST}"
    recent = [t for t in st.get("scans", []) if now - float(t) < 3600.0]
    if len(recent) >= hourly_cap(persona):
        return False, f"{len(recent)} scans in the last hour (cap {hourly_cap(persona)})"
    return True, ""


def record_scan(st: dict, now: float, target: tuple, alert: bool = False) -> None:
    st["scans"] = [t for t in st.get("scans", []) if now - float(t) < 3600.0] + [now]
    st["scanned"][key_of(target)] = now
    st["scan_next"] = now + scan_gap(alert)


def pick_scan_target(persona, st: dict, now: float, moon: tuple, level: int, own: set, aggressors: dict, targets: dict,
                     players: list[dict], bonus: float = 1.0) -> tuple[tuple, str] | None:
    """Who to scan next: (coordinates, why). `aggressors` {coord: when seen}, `targets` {coord: worth} for a raider's candidates,
    `players` rows of GET /highscore (main planet of each player). Only planets in range, not refused lately, not scanned too recently."""
    options: list[tuple[float, float, tuple, str]] = []

    def consider(c: tuple, kind: str, weight: float) -> None:
        if c in own or not in_range(moon, level, c, bonus):
            return
        key = key_of(c)
        if now < float(st.get("nogo", {}).get(key, 0.0)) or now - float(st.get("scanned", {}).get(key, -1e12)) < RESCAN_AFTER[kind]:
            return
        options.append((weight, float(st.get("scanned", {}).get(key, 0.0)), c, kind))   # best weight first, then the longest unscanned

    for c, seen in aggressors.items():
        if now - float(seen) < 24 * 3600.0:
            consider(c, "aggressor", 100.0)
    if is_military(persona):
        for c, worth in targets.items():
            consider(c, "target", 50.0 + min(40.0, float(worth) / 1000.0))
    for row in players:
        mp = row.get("main_planet") or {}
        if row.get("is_me") or not mp:
            continue
        consider(coord_from(mp), "other", 10.0)
    if not options:
        return None
    options.sort(key=lambda o: (-o[0], o[1]))
    return options[0][2], options[0][3]


def mark_nogo(st: dict, target: tuple, now: float) -> None:
    st["nogo"][key_of(target)] = now + NOGO_SECONDS


# ---- reading a scan ------------------------------------------------------------------------------------------
def read_scan(fleets: list[dict], own_coords: set, target: tuple, now: float) -> tuple[list[dict], dict | None]:
    """(warnings, away): hostile attacks flying to one of our planets, and how long the scanned planet's own fleets stay out.

    A warning is a hostile return flight whose origin is one of our planets: it left the scanned planet and reaches us at
    `departure_time`. away = {'until', 'ships'} when ships of the scanned planet are out and return (None if nothing is out)."""
    warnings, out_until, out_ships = [], 0.0, {}
    for f in fleets:
        try:
            origin, dest = coord_from(f.get("origin") or {}), coord_from(f.get("destination") or {})
            lands = float(f.get("departure_time") or 0)
            back = float(f.get("arrival_time") or 0)
        except (TypeError, ValueError):
            continue
        if f.get("hostile") and f.get("is_return_trip") and origin in own_coords and lands > now and not harmless_fleet(f.get("ships")):
            warnings.append({"id": int(f.get("mission_id") or 0), "kind": "attack", "lands": lands, "origin": target, "to": origin,
                             "ships": int(f.get("ship_count") or 0), "owner_id": f.get("owner_id")})
        if f.get("is_return_trip") and dest == target and back > now:
            out_until = max(out_until, back)
            for n, k in (f.get("ships") or {}).items():
                out_ships[n] = out_ships.get(n, 0) + int(k or 0)
    away = {"until": out_until, "ships": out_ships} if out_until else None
    return warnings, away


def remember_scan(st: dict, now: float, target: tuple, warnings: list[dict], away: dict | None) -> list[dict]:
    """Store the result; returns the warnings that are new (the caller logs those)."""
    key = key_of(target)
    if away:
        st["away"][key] = {"until": away["until"], "ships": away["ships"], "seen": now}
    else:
        st["away"].pop(key, None)
    fresh = []
    for w in warnings:
        wk = f"{w['id']}:{key_of(w['to'])}"
        if wk not in st["warn"]:
            fresh.append(w)
        st["warn"][wk] = {"lands": w["lands"], "origin": list(w["origin"]), "to": list(w["to"]), "ships": w["ships"], "seen": now}
    return fresh


def prune(st: dict, now: float) -> None:
    st["warn"] = {k: w for k, w in st["warn"].items() if float(w.get("lands", 0)) + WARN_KEEP > now}
    st["away"] = {k: a for k, a in st["away"].items() if float(a.get("until", 0)) > now}


def active_warnings(st: dict, now: float) -> list[dict]:
    """Attacks still to land: [{'id', 'lands', 'origin', 'to', 'arrives_in'}], soonest first."""
    out = []
    for k, w in st["warn"].items():
        if float(w.get("lands", 0)) > now:
            try:
                out.append({"id": int(k.split(":")[0]), "lands": float(w["lands"]), "origin": tuple(int(x) for x in w["origin"]),
                            "to": tuple(int(x) for x in w["to"]), "arrives_in": float(w["lands"]) - now})
            except (TypeError, ValueError, KeyError):
                continue
    return sorted(out, key=lambda w: w["arrives_in"])


def away_factor(st, coord, now: float, strength: float | None = None) -> float:
    """Raid worth multiplier (1.0 .. 1.3) for a planet whose fleet was seen away and stays away a while. Its share of the
    planet's known strength sets the bonus; with unknown strength a flat half share."""
    try:
        a = (st or {}).get("away", {}).get(key_of(coord))
        if not a or float(a.get("until", 0)) < now + AWAY_MIN_LEAD:
            return 1.0
        out_value = sum(unit_value(n) * int(k) for n, k in (a.get("ships") or {}).items())
    except (KeyError, TypeError, ValueError):
        return 1.0
    share = 0.5 if not strength else min(1.0, out_value / float(strength))
    return 1.0 + AWAY_MAX_SHARE * share
