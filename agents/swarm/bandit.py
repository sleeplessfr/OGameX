"""A small Thompson-sampling bandit: pick one of a few options, learn from how each one turned out.

Used for ONE decision so far: how many times more defenders than the spy report showed the fight judge should assume
(see military.DEFENDER_MARGIN). A higher margin is safer but skips more fights, so caution is not free: each arm's
sampled success rate is multiplied by a weight that shrinks with the margin. Without that, "always the most careful
arm" would win every time, since doing nothing never loses.

Everything is plain numbers, saved in the agent's memory file; damaged data falls back to the defaults.
"""
from __future__ import annotations

import random

MARGINS = (1.2, 1.5, 2.0, 3.0)      # never below 1.2: the estimate must not trust a spy report blindly
DEFAULT_MARGIN = 1.5
DECAY = 0.97                         # every update pulls all evidence a little back to the prior, so old results fade
PRIOR_FAVOURITE = 2.0                # prior "successes" of the default arm (the others start with 1)
FAILURE_WEIGHT = 2.0                 # a clearly bad attack (lost much more than gained) counts as two failures


class Bandit:
    def __init__(self, arms: tuple[float, ...] = MARGINS, default: float = DEFAULT_MARGIN):
        self.arms = tuple(arms)
        self.default = default
        self.a = {m: (PRIOR_FAVOURITE if m == default else 1.0) for m in self.arms}   # successes (+ prior)
        self.b = {m: 1.0 for m in self.arms}                                          # failures (+ prior)
        self.pulls = 0

    def __eq__(self, other) -> bool:
        return isinstance(other, Bandit) and self.to_dict() == other.to_dict()

    __hash__ = None  # type: ignore[assignment]

    def _prior(self, m: float) -> tuple[float, float]:
        return (PRIOR_FAVOURITE if m == self.default else 1.0), 1.0

    def weight(self, m: float) -> float:
        """Value of being bold: the more it assumes about the defenders, the more fights it gives up."""
        return self.default / m

    def choose(self, rng: random.Random) -> float:
        best, best_score = self.default, -1.0
        for m in self.arms:
            score = rng.betavariate(self.a[m], self.b[m]) * self.weight(m)
            if score > best_score:
                best, best_score = m, score
        self.pulls += 1
        return best

    def update(self, margin: float, outcome: str) -> None:
        """outcome: 'win' (made a profit), 'loss' (did not), 'bad' (lost far more than gained)."""
        if margin not in self.a:
            return
        for m in self.arms:                      # fade old evidence towards the prior
            pa, pb = self._prior(m)
            self.a[m] = pa + (self.a[m] - pa) * DECAY
            self.b[m] = pb + (self.b[m] - pb) * DECAY
        if outcome == "win":
            self.a[margin] += 1.0
        elif outcome == "bad":
            self.b[margin] += FAILURE_WEIGHT
        else:
            self.b[margin] += 1.0

    def rates(self) -> dict[float, float]:
        return {m: self.a[m] / (self.a[m] + self.b[m]) for m in self.arms}

    def to_dict(self) -> dict:
        return {"a": {str(m): v for m, v in self.a.items()}, "b": {str(m): v for m, v in self.b.items()}, "pulls": self.pulls}

    @classmethod
    def from_dict(cls, data) -> "Bandit":
        bandit = cls()
        if not isinstance(data, dict):
            return bandit
        for name, target in (("a", bandit.a), ("b", bandit.b)):
            values = data.get(name)
            if not isinstance(values, dict):
                continue
            for key, v in values.items():
                try:
                    m, v = float(key), float(v)
                except (TypeError, ValueError):
                    continue
                if m in target and 0.01 <= v <= 1e6:
                    target[m] = v
        try:
            bandit.pulls = max(0, int(data.get("pulls", 0)))
        except (TypeError, ValueError):
            pass
        return bandit


def outcome_of(verdict: str | None, gained: int, lost: int) -> str:
    """Boil a finished attack down to win / loss / bad. `gained` = loot + what we destroyed, `lost` = our losses."""
    if verdict == "bad":
        return "bad"
    if gained > lost:
        return "win"
    return "loss"
