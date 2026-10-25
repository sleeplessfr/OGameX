#!/usr/bin/env python3
"""Turn the agents' log files into one short report you can read (or attach to a chat).

    python summarize_logs.py                 -> writes report.txt
    python summarize_logs.py --hours 6       -> only the last 6 hours
    python summarize_logs.py --logs logs --out report.txt

It only reads files; it changes nothing in the game.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

NOTABLE = ("lesson", "trade", "merchant_dismissed", "trade_failed", "defend", "giving_up", "units_refused", "spy_refused", "refused")


def read_events(log_dir: Path, since: float = 0.0) -> list[dict]:
    events = []
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # a half-written last line
                if isinstance(e, dict) and e.get("t", 0) >= since and "event" in e:
                    events.append(e)
    events.sort(key=lambda e: e.get("t", 0))
    return events


def clock(t: float) -> str:
    # note: on the night clocks go back (last Sunday of October) the same hour appears twice
    return datetime.fromtimestamp(t).strftime("%d %H:%M")


def summarize(events: list[dict], personas: dict[str, str] | None = None) -> str:
    out: list[str] = []
    if not events:
        return "No events found. Are you in the agents folder, and are the agents running?\n"
    personas = personas or {}
    first, last = events[0]["t"], events[-1]["t"]
    out.append(f"AGENT REPORT  {clock(first)} to {clock(last)}  ({(last - first) / 3600:.1f} hours, {len(events)} events)")
    out.append("")

    by_agent: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_agent[e.get("agent", "?")].append(e)

    out.append("PER AGENT (count of each kind of event)")
    for agent in sorted(by_agent):
        c = Counter(e["event"] for e in by_agent[agent])
        persona = personas.get(agent, "")
        keys = ("build", "research", "units", "spy", "attack", "battle", "attack_skipped", "threat", "defend", "trade", "lesson")
        shown = ", ".join(f"{k} {c[k]}" for k in keys if c[k])
        out.append(f"  {agent} {persona}: {shown or 'nothing yet'}")
    out.append("")

    # Battles, from each agent's point of view as attacker
    out.append("ATTACKS BY EACH AGENT (what it lost, what it destroyed, what it looted)")
    any_attack = False
    for agent in sorted(by_agent):
        att = [e for e in by_agent[agent] if e["event"] == "battle" and e.get("role") == "attacker"]
        if not att:
            continue
        any_attack = True
        lost = sum(e.get("my_loss", 0) for e in att)
        killed = sum(e.get("their_loss", 0) for e in att)
        loot = sum(e.get("loot", 0) for e in att)
        verdicts = Counter(e.get("verdict") for e in att)
        out.append(f"  {agent}: {len(att)} attacks, lost {lost:,}, destroyed {killed:,}, looted {loot:,}, "
                   f"net {loot + killed - lost:+,}   (good {verdicts['good']}, bad {verdicts['bad']}, other {len(att) - verdicts['good'] - verdicts['bad']})")
    if not any_attack:
        out.append("  none")
    out.append("")

    out.append("TIMES AN AGENT WAS ATTACKED (as defender)")
    any_def = False
    for agent in sorted(by_agent):
        dfn = [e for e in by_agent[agent] if e["event"] == "battle" and e.get("role") == "defender"]
        raids = [e for e in dfn if e.get("loot", 0) > 0 or e.get("my_loss", 0) > 0 or e.get("rounds", 0) > 0]
        if not dfn:
            continue
        any_def = True
        out.append(f"  {agent}: {len(dfn)} reports, robbed of {sum(e.get('loot', 0) for e in dfn):,}, "
                   f"lost {sum(e.get('my_loss', 0) for e in dfn):,} in ships/defense, destroyed {sum(e.get('their_loss', 0) for e in dfn):,} of the enemy; "
                   f"{len(raids)} of them real fights or robberies")
    if not any_def:
        out.append("  none")
    out.append("")

    skipped = Counter()
    sim_skips = []
    for e in events:
        if e["event"] == "attack_skipped":
            why = e.get("why", "")
            kind = "simulated fight too risky" if why.startswith("simulated") else (
                "cannot carry the loot" if "carry" in why else "defenses unknown / other")
            skipped[kind] += 1
            if why.startswith("simulated"):
                sim_skips.append(e)
    out.append("ATTACKS NOT SENT: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))
    for e in sim_skips[-5:]:
        out.append(f"  {clock(e['t'])} {e['agent']} at {e.get('at')}: {e.get('why')}")
    out.append("")

    out.append("BAD ATTACKS (what the agent knew when it sent them)")
    bad = [e for e in events if e["event"] == "battle" and e.get("role") == "attacker" and e.get("verdict") == "bad"]
    for b in bad[-6:]:
        sent = [e for e in by_agent[b["agent"]] if e["event"] == "attack" and e.get("at") == b.get("at") and e["t"] <= b["t"]]
        out.append(f"  {clock(b['t'])} {b['agent']} at {b.get('at')}: lost {b.get('my_loss', 0):,}, destroyed {b.get('their_loss', 0):,}, looted {b.get('loot', 0):,}, {b.get('rounds')} rounds")
        if sent:
            a = sent[-1]
            out.append(f"      sent {clock(a['t'])}: ships {a.get('ships')}")
            out.append(f"      reason: {a.get('why')}")
            if a.get("saw_defense") is not None or a.get("saw_ships") is not None:
                out.append(f"      the spy report showed: ships {a.get('saw_ships')}, defense {a.get('saw_defense')}, research {a.get('saw_research')}")
        if b.get("theirs") is not None:
            out.append(f"      in the real battle: mine {b.get('mine')}, theirs {b.get('theirs')}")
        else:
            out.append("      (no matching attack line in these logs)")
    if not bad:
        out.append("  none")
    out.append("")

    out.append("DEFENCE (threat = a foreign fleet seen coming; defend = defence units ordered)")
    threats = [e for e in events if e["event"] == "threat"]
    defends = [e for e in events if e["event"] == "defend"]
    if threats or defends:
        kinds = Counter(e.get("kind") for e in threats)
        out.append(f"  {len(threats)} threats ({', '.join(f'{k} {v}' for k, v in kinds.items())}), {len(defends)} defence orders")
        for e in defends[-5:]:
            out.append(f"  {clock(e['t'])} {e['agent']} built {e.get('amount')} {e.get('what')} ({e.get('why')})")
    else:
        out.append("  none yet (the defence code may not be installed, or nobody was attacked)")
    out.append("")

    out.append("MERCHANT")
    merch = [e for e in events if e["event"] in ("trade", "merchant_dismissed", "trade_failed")]
    if merch:
        for e in merch[-8:]:
            body = {k: v for k, v in e.items() if k not in ("t", "agent", "event")}
            out.append(f"  {clock(e['t'])} {e['agent']} {e['event']} {body}")
    else:
        out.append("  no trade yet (expected: the rules are strict)")
    out.append("")

    out.append("LESSONS")
    lessons = [e for e in events if e["event"] == "lesson"]
    for e in lessons[-8:]:
        out.append(f"  {clock(e['t'])} {e['agent']}: {e.get('why')}")
    if not lessons:
        out.append("  none")
    out.append("")

    out.append("OPPONENTS KNOWN (what raiders learned about their targets: our attacks, wins, losses, net result)")
    latest: dict = {}
    for e in events:
        if e["event"] == "opponent":
            latest[(e["agent"], e.get("at"))] = e
    seen = {(e["agent"], e.get("at")): e for e in events if e["event"] == "report" and e.get("strength") is not None}
    if latest:
        for (agent, at), e in sorted(latest.items(), key=lambda kv: -kv[1].get("attacks", 0))[:10]:
            strength = (seen.get((agent, at)) or {}).get("strength", e.get("strength"))
            out.append(f"  {agent} on {at}: {e.get('attacks', 0)} attacks, {e.get('wins', 0)} won, {e.get('losses', 0)} lost, "
                       f"net {e.get('net', 0):+,}; last seen strength {strength if strength is not None else '?'}")
    else:
        out.append("  none yet")
    out.append("")

    out.append("LEARNED FIGHT MARGIN (how many times the spy report's defenders the raider assumed, and how it went)")
    margin_rows: dict = defaultdict(lambda: defaultdict(Counter))
    for e in events:
        if e["event"] == "battle" and e.get("margin") is not None and e.get("outcome"):
            margin_rows[e["agent"]][e["margin"]][e["outcome"]] += 1
    if margin_rows:
        for agent in sorted(margin_rows):
            parts = []
            for m in sorted(margin_rows[agent]):
                c = margin_rows[agent][m]
                parts.append(f"x{m:g}: {sum(c.values())} attacks, {c['win']} profit, {c['bad']} bad")
            out.append(f"  {agent}: " + "; ".join(parts))
    else:
        out.append("  none yet")
    out.append("")

    out.append("CHARACTER CLASSES (chosen once by each agent)")
    classes = {e["agent"]: e.get("chosen") for e in events if e["event"] == "class"}
    refused = [e for e in events if e["event"] == "class_refused"]
    if classes or refused:
        for name in sorted(set(classes.values())):
            out.append(f"  {name}: {', '.join(sorted(a for a, c in classes.items() if c == name))}")
        for e in refused[:5]:
            out.append(f"  {clock(e['t'])} {e['agent']} refused: {e.get('code')} {e.get('msg')}")
    else:
        out.append("  none in this period")
    out.append("")

    out.append("COLONIES (expand = wants one, sent = ship on its way, founded = new planet, failed = see why)")
    colony = [e for e in events if e["event"] in ("expand", "colony_sent", "colony_founded", "colony_failed")]
    if colony:
        for e in colony[-25:]:
            detail = e.get("why") or e.get("at") or ""
            extra = f" at {e['at']}" if e.get("at") and e.get("why") else ""
            out.append(f"  {clock(e['t'])} {e['agent']} {e['event']}: {detail}{extra}")
    else:
        out.append("  none")
    out.append("")

    out.append("LOGISTICS (supply = resources sent between own planets, saved = fleet dodged a raid it could not win)")
    sent = [e for e in events if e["event"] == "supply_sent"]
    skips = [e for e in events if e["event"] == "supply_skipped"]
    saved = [e for e in events if e["event"] == "fleet_saved"]
    back = [e for e in events if e["event"] == "fleet_returned"]
    stays = [e for e in events if e["event"] == "fleet_stays"]
    failed = [e for e in events if e["event"] == "fleet_save_failed"]
    if sent or skips or saved or back or stays or failed:
        moved: Counter = Counter()
        for e in sent:
            for r, v in (e.get("resources") or {}).items():
                moved[r] += int(v or 0)
        fuel = sum(int(e.get("fuel") or 0) for e in sent)
        out.append(f"  {len(sent)} supply trips sent ({sum(int(e.get('value') or 0) for e in sent):,} worth: "
                   f"{', '.join(f'{r} {v:,}' for r, v in moved.items()) or 'nothing'}; {fuel:,} deuterium fuel), {len(skips)} skipped")
        for why, n in Counter(str(e.get("why", ""))[:60] for e in skips).most_common(3):
            out.append(f"    skipped {n}x: {why}")
        carried: Counter = Counter()
        for e in saved:
            for r, v in (e.get("resources") or {}).items():
                carried[r] += int(v or 0)
        out.append(f"  {len(saved)} fleets saved (ships worth {sum(int(e.get('value') or 0) for e in saved):,}; "
                   f"resources taken along: {', '.join(f'{r} {v:,}' for r, v in carried.items()) or 'none'}), "
                   f"{sum(1 for e in back if e.get('ok'))} brought home, {sum(1 for e in back if not e.get('ok'))} lost track of, "
                   f"{len(stays)} stayed to fight, {len(failed)} could not run")
        for e in (saved + failed)[-5:]:
            out.append(f"    {clock(e['t'])} {e['agent']} {e['event']}: {e.get('why') or ''} {('ships ' + str(e.get('ships'))) if e.get('ships') else ''}".rstrip())
    else:
        out.append("  none")
    out.append("")

    out.append("FULL PLANETS (no free building fields)")
    full = Counter(e["agent"] for e in events if e["event"] == "wait" and "no free fields" in str(e.get("why", "")))
    heading = Counter(e["agent"] for e in events if e["event"] == "wait" and "free fields left" in str(e.get("why", "")))
    if full or heading:
        for a in sorted(set(full) | set(heading)):
            out.append(f"  {a}: {heading[a]} waits on the way to the terraformer, {full[a]} waits with a full planet")
    else:
        out.append("  none")
    out.append("")

    out.append("ERRORS")
    errs = [e for e in events if e["event"] in ("api_error", "error", "giving_up")]
    if errs:
        groups = Counter((e.get("event"), e.get("status") or e.get("type"), e.get("code")) for e in errs)
        for (ev, status, code), n in groups.most_common(8):
            times = [e["t"] for e in errs if (e.get("event"), e.get("status") or e.get("type"), e.get("code")) == (ev, status, code)]
            out.append(f"  {n}x {ev} status={status} code={code}  first {clock(min(times))}, last {clock(max(times))}")
        for e in errs[-3:]:
            out.append(f"  latest: {clock(e['t'])} {e['agent']} {e.get('msg', '')[:160]} {e.get('request', '')}")
    else:
        out.append("  none")
    out.append("")

    out.append("OTHER NOTABLE EVENTS (last 10)")
    others = [e for e in events if e["event"] in NOTABLE and e["event"] not in ("lesson", "trade", "merchant_dismissed", "trade_failed", "defend")]
    for e in others[-10:]:
        body = {k: v for k, v in e.items() if k not in ("t", "agent", "event")}
        out.append(f"  {clock(e['t'])} {e['agent']} {e['event']} {body}")
    if not others:
        out.append("  none")
    return "\n".join(out) + "\n"


def personas_from_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {name: f"({entry['persona']})" for name, entry in data.items() if isinstance(entry, dict) and entry.get("persona")}
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs", help="folder with the agents' .jsonl logs")
    ap.add_argument("--state", default="agents_state.json", help="used only to show each agent's personality")
    ap.add_argument("--out", default="report.txt")
    ap.add_argument("--hours", type=float, default=0.0, help="only look at the last N hours (0 = everything)")
    args = ap.parse_args()

    log_dir = Path(args.logs)
    if not log_dir.is_dir():
        print(f"No folder named {args.logs!r} here. Run this from the agents folder (the one with run_swarm.py).", file=sys.stderr)
        return 1
    since = time.time() - args.hours * 3600 if args.hours > 0 else 0.0
    text = summarize(read_events(log_dir, since), personas_from_state(Path(args.state)))
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"(also saved to {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
