"""Live population evolution (plan step 4, see claude/plan-real-learning.md).

Every EPOCH (default 12 server hours, aligned to the clock so all agents share the same windows) each agent measures the growth
of its own goal (research levels for a scientist, hourly production value for everyone else; buildlearn.metric) and files a
RECORD in a shared folder:  <memory dir>/evo/<personality>/<window>/<agent>.json.  One window later, when the records of that
window are complete, an agent whose growth was in the BOTTOM QUARTER of its own personality copies the genome of a random
agent from the TOP QUARTER and mutates it a little. Everyone else keeps their genome. No central program: each agent
decides for itself from the files, so it works with several processes too.

Controls (one agent in four, targetlearn.is_control) file records but never change their genome: they are the yardstick.
Genomes (genome.py) only turn bounded dials around the personality, so a miner stays a miner.

Switches: run_swarm.py --evolve on (env OGAMEX_EVOLVE), --evolve-hours N (OGAMEX_EVOLVE_HOURS), --genomes DIR (OGAMEX_GENOMES:
genomes made by train_genome.py are the starting point of every non-control agent, each with a little personal noise).
Damaged files are ignored.
"""
from __future__ import annotations

import json
import os
import random
import zlib
from pathlib import Path

from . import genome as G
from . import targetlearn

MIN_POP = 8            # fewer evolving records than this in a window: nobody is replaced
START_NOISE = 0.06     # personal noise on the starting genome, so a trained genome does not make clones
MUTATION = 0.12
FULL_WINDOW = 0.2      # a window counts only if the agent measured near its start and near its end (share of the window)


def enabled() -> bool:
    return os.environ.get("OGAMEX_EVOLVE", "off").lower() == "on"


def epoch_seconds() -> float:
    try:
        return max(600.0, float(os.environ.get("OGAMEX_EVOLVE_HOURS", "12")) * 3600.0)
    except ValueError:
        return 12 * 3600.0


def genomes_dir() -> str:
    return os.environ.get("OGAMEX_GENOMES", "")


def active() -> bool:
    return enabled() or bool(genomes_dir())


def is_control(agent_name: str) -> bool:
    return targetlearn.is_control(agent_name)


def group_of(agent_name: str) -> str:
    return "off" if not active() else "control" if is_control(agent_name) else "evolving"


def new_state() -> dict:
    return {"genome": None, "gen": 0, "parent": None, "cur": None, "decided": 0}


def clean_state(data) -> dict:
    st = new_state()
    if not isinstance(data, dict):
        return st
    try:
        if isinstance(data.get("genome"), dict):
            st["genome"] = G.clamp(data["genome"])
        st["gen"] = max(0, int(data.get("gen", 0)))
        st["decided"] = int(data.get("decided", 0))
        st["parent"] = data["parent"] if isinstance(data.get("parent"), str) else None
        cur = data.get("cur")
        if isinstance(cur, dict) and all(k in cur for k in ("k", "t", "start")):
            st["cur"] = {"k": int(cur["k"]), "t": float(cur["t"]), "start": float(cur["start"])}
    except (TypeError, ValueError, KeyError):
        return new_state()
    return st


def initial_genome(agent_name: str, persona_name: str) -> dict:
    """Starting genome: the trained one for the personality if --genomes has it, else neutral; plus personal noise (not for controls)."""
    if is_control(agent_name):
        return G.neutral()
    base = G.neutral()
    folder = genomes_dir()
    if folder:
        try:
            data = json.loads((Path(folder) / f"{persona_name}.json").read_text(encoding="utf-8"))
            base = G.clamp(data.get("genome"))
        except (OSError, ValueError, AttributeError):
            pass
    noise = random.Random(zlib.crc32(f"{agent_name}:start".encode()))
    return G.mutate(base, noise, sigma=START_NOISE if folder else START_NOISE * 1.5, rate=0.8)


def window_of(now: float) -> int:
    return int(now // epoch_seconds())


def _record_path(folder: Path, persona_name: str, window: int, agent: str) -> Path:
    return folder / persona_name / str(window) / f"{agent}.json"


def write_record(folder: Path, persona_name: str, window: int, agent: str, growth: float, genome: dict, control: bool) -> None:
    path = _record_path(folder, persona_name, window, agent)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agent": agent, "growth": round(growth, 5), "genome": genome, "control": control}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def read_records(folder: Path, persona_name: str, window: int) -> list[dict]:
    out = []
    try:
        files = sorted((folder / persona_name / str(window)).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            out.append({"agent": str(r["agent"]), "growth": float(r["growth"]), "genome": G.clamp(r.get("genome")), "control": bool(r.get("control"))})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def select(records: list[dict], me: str, rng: random.Random) -> dict:
    """What `me` does after a window: {'action': 'kept'|'replaced'|'none', rank, pop, parent, genome}."""
    mine = next((r for r in records if r["agent"] == me), None)
    pool = sorted((r for r in records if not r["control"]), key=lambda r: (-r["growth"], r["agent"]))
    if mine is None or mine["control"] or len(pool) < MIN_POP:
        return {"action": "none", "rank": None, "pop": len(pool)}
    rank = next(i for i, r in enumerate(pool) if r["agent"] == me) + 1
    quarter = max(1, len(pool) // 4)
    if rank <= len(pool) - quarter:
        return {"action": "kept", "rank": rank, "pop": len(pool)}
    parent = rng.choice(pool[:quarter])
    return {"action": "replaced", "rank": rank, "pop": len(pool), "parent": parent["agent"],
            "genome": G.mutate(parent["genome"], rng, sigma=MUTATION, rate=0.5)}


def step(st: dict, agent: str, persona_name: str, folder: Path | None, now: float, value: float, control: bool) -> dict | None:
    """Call every round with the agent's current goal metric. At a window change files the record of the window just ended and
    decides (from the window before it) whether to replace the genome. Returns a dict for the log when something happened."""
    epoch = epoch_seconds()
    k = window_of(now)
    cur = st.get("cur")
    if cur is None:
        st["cur"] = {"k": k, "t": now, "start": max(value, 1e-9)}
        return None
    if k == cur["k"]:
        return None
    result: dict | None = None
    full = (cur["t"] - cur["k"] * epoch) <= FULL_WINDOW * epoch and k == cur["k"] + 1 and (now - k * epoch) <= FULL_WINDOW * epoch
    if full and folder is not None:
        growth = max(-1.0, min(5.0, value / max(cur["start"], 1e-9) - 1.0))
        genome = st.get("genome") or G.neutral()
        write_record(folder, persona_name, cur["k"], agent, growth, genome, control)
        result = {"window": cur["k"], "growth": round(growth, 4), "action": "recorded", "genome": genome}
        if not control:
            rng = random.Random(f"{agent}:evolve:{cur['k']}")
            picked = select(read_records(folder, persona_name, cur["k"] - 1), agent, rng)
            if picked["action"] != "none":
                result.update({"action": picked["action"], "rank": picked["rank"], "pop": picked["pop"], "decided_window": cur["k"] - 1})
                st["decided"] = st.get("decided", 0) + 1
                if picked["action"] == "replaced":
                    st["genome"], st["parent"], st["gen"] = picked["genome"], picked["parent"], st.get("gen", 0) + 1
                    result.update({"parent": picked["parent"], "new_genome": picked["genome"]})
    st["cur"] = {"k": k, "t": now, "start": max(value, 1e-9)}
    return result
