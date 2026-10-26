"""Activity routines: real players are online only a small part of the day, so an agent can be too.

Every agent gets a fixed daily routine worked out from its NAME (crc32), never from the persona random generator, so
switching routines on or off changes nothing about how the agents are built. A routine is a few online "sessions" a day.
Between them the agent is OFFLINE: it makes no API calls at all and just sleeps. Its game keeps running on the server.

  mode "light"  about half the day online (several long sessions)
  mode "real"   about 5-15 % of the day online (2-5 sessions of 5-40 minutes, 4-8 short check-ins for miners/turtles)

Each agent lives in its own time zone (a fixed offset) so the whole swarm is not online at the same moment. Miners and
turtles check in more often and for shorter times; raiders play in the evening; scientists keep fewer, longer sessions.

`time_scale` compresses days: 24 = a routine day passes in one real hour. All public methods take and return REAL seconds
(epoch for "now", plain seconds for waits), so callers never think about the scale.
"""
from __future__ import annotations

import random
import zlib

DAY = 86400.0
MIN = 60.0
GAP = 10 * MIN                 # at least this long between two sessions
MODES = ("off", "light", "real")

# (sessions low, high, minutes low, high), by mode and kind of player
_PLAN = {
    "real": {"default": (2, 5, 10, 40), "checker": (4, 8, 6, 24), "evening": (3, 5, 20, 40), "long": (2, 4, 25, 40)},
    "light": {"default": (4, 6, 140, 260), "checker": (6, 9, 90, 170), "evening": (4, 6, 140, 260), "long": (4, 6, 150, 270)},
}
_KIND = {"miner": "checker", "turtle": "checker", "raider": "evening", "scientist": "long"}

# relative weight of each local hour of day as a session start: evening peak for everybody, a little morning/lunch
_BASE_HOURS = [0.3, 0.1, 0.05, 0.05, 0.05, 0.2, 0.6, 1.0, 1.0, 0.7, 0.6, 0.9, 1.2, 0.8, 0.6, 0.6, 0.8, 1.2, 2.0, 3.0, 3.0, 2.5, 1.5, 0.7]
_EVENING_HOURS = [0.3, 0.1, 0.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.2, 0.2, 0.2, 0.3, 0.4, 0.3, 0.3, 0.4, 0.6, 1.5, 3.0, 4.0, 4.0, 3.5, 2.5, 1.0]


def crc(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF


class Routine:
    def __init__(self, name: str, persona: str = "balanced", mode: str = "real", time_scale: float = 1.0):
        if mode not in _PLAN:
            raise ValueError(f"routine mode must be 'light' or 'real', not {mode!r}")
        self.name, self.persona, self.mode = name, persona, mode
        self.scale = max(float(time_scale), 1e-6)
        self.kind = _KIND.get(persona, "default")
        self.tz = crc(name, "tz") % 24                  # this agent's offset from the common clock, in whole hours
        self._cache: dict[int, list[tuple[float, float]]] = {}

    # ---- the schedule, in "routine seconds" (one day = 86400) -------------------------------------------------
    def windows(self, day: int) -> list[tuple[float, float]]:
        """Sessions of local day number `day` as (start, end) in seconds of that local day: sorted, never overlapping."""
        hit = self._cache.get(day)
        if hit is not None:
            return hit
        rng = random.Random(crc(self.name, "routine", self.mode, day))
        n_lo, n_hi, m_lo, m_hi = _PLAN[self.mode][self.kind]
        n = rng.randint(n_lo, n_hi)
        weights = _EVENING_HOURS if self.kind == "evening" and self.mode == "real" else _BASE_HOURS
        picks = []
        for _ in range(n):
            hour = rng.choices(range(24), weights=weights)[0]
            start = hour * 3600.0 + rng.uniform(0, 3600.0)
            picks.append((start, rng.uniform(m_lo, m_hi) * MIN))
        picks.sort()
        out: list[tuple[float, float]] = []
        floor = 0.0
        for start, length in picks:
            start = max(start, floor)
            end = min(start + length, DAY)
            if end - start >= 5 * MIN:                  # what does not fit in the day is simply dropped
                out.append((start, end))
                floor = end + GAP
        self._cache[day] = out
        if len(self._cache) > 6:
            self._cache.pop(next(iter(self._cache)))
        return out

    def _local(self, now: float) -> float:
        return now * self.scale + self.tz * 3600.0

    def _find(self, now: float) -> tuple[bool, float, float]:
        """(online, session start or end of it, next start) in routine seconds, at real time `now`."""
        t = self._local(now)
        day = int(t // DAY)
        sec = t - day * DAY
        for d in (day, day + 1, day + 2):
            for start, end in self.windows(d):
                a, b = d * DAY + start, d * DAY + end
                if t < a:
                    return False, t, a
                if t < b:
                    return True, b, b
        return False, t, t + DAY

    # ---- the public questions (real seconds) -----------------------------------------------------------------------
    def online(self, now: float) -> bool:
        return self._find(now)[0]

    def session_end(self, now: float) -> float:
        """Real time when the current session ends (only meaningful while online)."""
        online, end, _ = self._find(now)
        return now + (end - self._local(now)) / self.scale if online else now

    def seconds_until_online(self, now: float) -> float:
        """Real seconds to wait until the next session starts (0 while online)."""
        online, _, nxt = self._find(now)
        return 0.0 if online else max(0.0, (nxt - self._local(now)) / self.scale)

    def clip(self, now: float, delay: float) -> float:
        """`delay` (real seconds), cut short at the end of the current session: a real player who logs off does not
        come back mid-pause. Offline = the full delay (nothing to cut)."""
        if not self.online(now):
            return delay
        return max(0.01, min(delay, self.session_end(now) - now + 0.01))

    def share_online(self, days: int = 7, start_day: int = 0) -> float:
        """Fraction of the time spent online over `days` local days (for tests and reports)."""
        total = sum(e - s for d in range(start_day, start_day + days) for s, e in self.windows(d))
        return total / (days * DAY)


def make_routine(mode: str | None, name: str, persona: str, time_scale: float = 1.0) -> Routine | None:
    """A routine, or None for mode off/None (the agent then plays all the time, as before)."""
    if not mode or mode == "off":
        return None
    return Routine(name, persona, mode, time_scale)
