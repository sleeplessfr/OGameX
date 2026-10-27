#!/usr/bin/env python3
"""Does the economy simulator match the real game?  (step 5, milestone 1; gate: within about 10-15 %)

    python validate_econsim.py                       (reads logs/*.jsonl, economy speed x100)
    python validate_econsim.py --logs logs --speed 100 --hours 12

It reads two kinds of lines the agents write:
  econ_snapshot  every 30 min: levels, hourly production, energy and storage of every planet, research levels
  build/research the real construction and research durations the game gave
and prints how far swarm/econsim.py is from them. Nothing is changed. The report is written to econsim_report.txt too.
The deuterium output depends on the planet's temperature, which the API does not show: the check estimates it from the
position, and also reports the temperature the real output implies (it must lie in the position's range).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from swarm import econsim
from swarm.colonise import TEMPERATURE

BUILD_NAMES = {"metal_mine", "crystal_mine", "deuterium_synthesizer", "solar_plant", "fusion_plant", "metal_store", "crystal_store",
               "deuterium_store", "robot_factory", "shipyard", "research_lab", "nano_factory", "terraformer"}


def read_events(folder: Path, hours: float | None) -> list[dict]:
    events: list[dict] = []
    for path in sorted(folder.glob("agent*.jsonl")):
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"econ_snapshot"' not in line and '"event": "build"' not in line and '"event": "research"' not in line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    if hours and events:
        cut = max(e["t"] for e in events) - hours * 3600
        events = [e for e in events if e["t"] >= cut]
    return sorted(events, key=lambda e: e["t"])


def temp_estimate(position: int) -> float:
    lo, hi = TEMPERATURE.get(position, (0, 40))
    return (lo + hi) / 2 - 20       # temp_max is drawn in [lo, hi]; the game uses (temp_min + temp_max) / 2 = temp_max - 20


def temp_range(position: int) -> tuple[float, float]:
    lo, hi = TEMPERATURE.get(position, (0, 40))
    return lo - 20, hi - 20


def rel(sim: float, real: float) -> float | None:
    if real <= 0 and sim <= 0:
        return 0.0
    if real <= 0:
        return None
    return (sim - real) / real


def check_snapshots(events: list[dict], speed: float) -> dict:
    errs: dict[str, list[float]] = {"metal": [], "crystal": [], "deuterium": [], "energy": [], "storage": []}
    worst: list[tuple[float, str]] = []
    temps_out = 0
    temps_total = 0
    n = 0
    for e in events:
        if e.get("event") != "econ_snapshot":
            continue
        research = e.get("research") or {}
        collector = e.get("klass") == "collector"
        for p in e.get("planets") or []:
            levels, prod, pos = p.get("levels") or {}, p.get("prod") or {}, p.get("pos")
            if not prod or not pos or not any(levels.get(k) for k in econsim.MINES):
                continue
            n += 1
            sim = econsim.production(levels, int(pos), temp_estimate(int(pos)), speed, int(research.get("energy_technology", 0) or 0),
                                     int(research.get("plasma_technology", 0) or 0), collector)
            for r in ("metal", "crystal", "deuterium"):
                x = rel(sim[r], float(prod.get(r, 0)))
                if x is not None:
                    errs[r].append(x)
                    if abs(x) > 0.15 and r != "deuterium":
                        worst.append((abs(x), f"{e['agent']} planet {p['id']} {r}: sim {sim[r]:,.0f} real {prod.get(r, 0):,} ({x:+.0%}) levels {levels}"))
            d_real, levels_d = float(prod.get("deuterium", 0)), levels.get("deuterium_synthesizer", 0)
            if d_real > 0 and levels_d and not levels.get("fusion_plant"):   # which temperature would give exactly the real output?
                at0 = econsim.production(levels, int(pos), 0.0, speed, int(research.get("energy_technology", 0) or 0),
                                         int(research.get("plasma_technology", 0) or 0), collector)["deuterium"]
                at100 = econsim.production(levels, int(pos), 100.0, speed, int(research.get("energy_technology", 0) or 0),
                                           int(research.get("plasma_technology", 0) or 0), collector)["deuterium"]
                if at0 != at100:
                    t = (d_real - at0) / (at100 - at0) * 100.0
                    lo, hi = temp_range(int(pos))
                    temps_total += 1
                    if not (lo - 6 <= t <= hi + 6):
                        temps_out += 1
            make, need = econsim.energy(levels, int(research.get("energy_technology", 0) or 0), collector)
            if p.get("energy") is not None and (make - need) != 0:
                errs["energy"].append((make - need - float(p["energy"])) / max(abs(make), abs(need), 1.0))
            st = p.get("storage")
            if st:
                cap = econsim.storage(levels)
                for r in econsim.RESOURCES:
                    if st.get(r):
                        errs["storage"].append((cap[r] - st[r]) / st[r])
    return {"n": n, "errs": errs, "worst": sorted(worst, reverse=True)[:8], "temps_out": temps_out, "temps_total": temps_total}


def check_durations(events: list[dict], speed: float) -> dict:
    """For every logged build/research the duration the game gave against the formula, using robot/nano/lab levels from the
    snapshots and from the agent's own earlier builds."""
    by_agent: dict[str, list[dict]] = {}
    for e in events:
        by_agent.setdefault(e["agent"], []).append(e)
    rows = {"build": [0, 0], "research": [0, 0]}
    wrong: list[str] = []
    for agent, evs in by_agent.items():
        known: dict[int, dict[str, int]] = {}      # planet -> building -> level (completed or started)
        discoverer = False
        for e in evs:
            if e.get("event") == "econ_snapshot":
                discoverer = e.get("klass") == "discoverer"
                for p in e.get("planets") or []:
                    k = known.setdefault(p["id"], {})
                    for name, lv in (p.get("levels") or {}).items():
                        k[name] = max(k.get(name, 0), lv)
                continue
            kind, pid, what, level, sec = e["event"], e.get("planet"), e.get("what"), e.get("level"), e.get("seconds")
            if what is None or level is None or not sec:
                continue
            k = known.setdefault(pid, {})
            if kind == "build":
                if what in BUILD_NAMES and what in econsim.OBJECTS:
                    sim = econsim.build_seconds(what, int(level), k.get("robot_factory", 0), k.get("nano_factory", 0), speed)
                    rows["build"][1] += 1
                    if abs(sim - sec) <= max(2, 0.02 * sec):
                        rows["build"][0] += 1
                    elif len(wrong) < 6:
                        wrong.append(f"{agent} build {what} L{level}: game {sec}s, formula {sim}s (robot {k.get('robot_factory', 0)}, nano {k.get('nano_factory', 0)})")
                k[what] = max(k.get(what, 0), int(level) - 1)    # the level being built is not finished yet
            else:
                if what in econsim.OBJECTS:
                    lab = k.get("research_lab", 0)
                    sim = econsim.research_seconds(what, int(level), lab, speed, discoverer=discoverer)
                    rows["research"][1] += 1
                    if abs(sim - sec) <= max(2, 0.02 * sec):
                        rows["research"][0] += 1
                    elif len(wrong) < 12:
                        wrong.append(f"{agent} research {what} L{level}: game {sec}s, formula {sim}s (lab {lab})")
    return {"rows": rows, "wrong": wrong}


def pct(values: list[float], q: float) -> float:
    v = sorted(abs(x) for x in values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else 0.0


def report(snap: dict, dur: dict) -> tuple[str, bool]:
    lines = ["ECONOMY SIMULATOR CHECK (simulator against what the real game said)", ""]
    ok = True
    lines.append(f"Production: {snap['n']} planet snapshots compared")
    for r in ("metal", "crystal", "deuterium", "energy", "storage"):
        e = snap["errs"][r]
        if not e:
            lines.append(f"  {r:9s}: no data")
            continue
        med, p90 = statistics.median(abs(x) for x in e), pct(e, 0.9)
        verdict = "good" if p90 <= 0.15 else "OFF"
        if r in ("metal", "crystal") and p90 > 0.15:
            ok = False
        lines.append(f"  {r:9s}: typical error {med:.1%}, 90 % of planets within {p90:.1%}  [{verdict}]"
                     + ("   (deuterium also depends on the unknown temperature)" if r == "deuterium" else ""))
    if snap["temps_total"]:
        lines.append(f"  temperature implied by the real deuterium output inside the position's range: "
                     f"{snap['temps_total'] - snap['temps_out']} of {snap['temps_total']}")
    for w in snap["worst"]:
        lines.append("    " + w[1])
    lines.append("")
    lines.append("Durations (game vs formula, within 2 %):")
    for kind, (good, total) in dur["rows"].items():
        share = good / total if total else 0.0
        if total and share < 0.9:
            ok = False
        lines.append(f"  {kind:9s}: {good} of {total} match" + (f" ({share:.0%})" if total else ""))
    for w in dur["wrong"]:
        lines.append("    " + w)
    lines.append("")
    lines.append("VERDICT: " + ("the simulator matches the game well enough to build on" if ok else "NOT yet: send this report so the formulas can be fixed"))
    return "\n".join(lines), ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--speed", type=float, default=100.0, help="economy speed of the universe (yours is x100)")
    ap.add_argument("--hours", type=float, default=None, help="only the last N hours of the logs")
    ap.add_argument("--out", default="econsim_report.txt")
    args = ap.parse_args()
    folder = Path(args.logs)
    if not folder.is_dir():
        print(f"no log folder {folder}")
        return 2
    events = read_events(folder, args.hours)
    if not any(e.get("event") == "econ_snapshot" for e in events):
        print("no econ_snapshot lines yet: run update 62 for at least an hour, then try again")
        return 2
    text, ok = report(check_snapshots(events, args.speed), check_durations(events, args.speed))
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
