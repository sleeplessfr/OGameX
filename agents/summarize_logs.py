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


def summarize(events: list[dict], personas: dict[str, str] | None = None, llm_calls: list[dict] | None = None) -> str:
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
            if "intel_source" in a:
                out.append(f"      intel: {a.get('intel_source')}, age {a.get('intel_age')}s, composition known {a.get('composition_known')}, simulated win chance {a.get('sim_win_chance')}")
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

    out.append("RECYCLING (recycle_sent = recyclers sent to a debris field, recycle_skipped = not worth it or no such field)")
    sent = [e for e in events if e["event"] == "recycle_sent"]
    skipped_r = [e for e in events if e["event"] == "recycle_skipped"]
    bought = [e for e in events if e["event"] == "units" and e.get("what") == "recycler"]
    if sent or skipped_r or bought:
        for agent in sorted({e["agent"] for e in sent + skipped_r + bought}):
            mine = [e for e in sent if e["agent"] == agent]
            haul = sum(int(e.get("haul") or 0) for e in mine)
            fuel = sum(int(e.get("fuel") or 0) for e in mine)
            n_bought = sum(int(e.get("amount") or 0) for e in bought if e["agent"] == agent)
            n_skipped = sum(1 for e in skipped_r if e["agent"] == agent)
            out.append(f"  {agent}: {len(mine)} trips, about {haul:,} metal+crystal expected, {fuel:,} deuterium burned, "
                       f"{n_bought} recyclers built, {n_skipped} skipped")
        for e in skipped_r[-3:]:
            out.append(f"  {clock(e['t'])} {e['agent']} skipped {e.get('at')}: {e.get('why')}")
    else:
        out.append("  none")
    out.append("")

    out.append("DOCTRINE (doctrine = the warship an agent buys and the one it aims for, from simulated fights; army_push = it spends more on ships because its targets are out of reach)")
    doc = [e for e in events if e["event"] == "doctrine"]
    push = [e for e in events if e["event"] == "army_push"]
    if doc or push:
        for agent in sorted({e["agent"] for e in doc + push}):
            mine = [e for e in doc if e["agent"] == agent]
            pushes = [e for e in push if e["agent"] == agent]
            if mine:
                last = mine[-1]
                table = ", ".join(f"{r[0]} {r[1]}" for r in (last.get("table") or [])[:4] if isinstance(r, (list, tuple)) and len(r) == 2)
                out.append(f"  {agent}: buys {last.get('chosen')}, aims for {last.get('aim')} ({len(mine)} change{'s' if len(mine) != 1 else ''})")
                out.append(f"    why: {last.get('why')}")
                if table:
                    out.append(f"    scores: {table}")
            if pushes:
                out.append(f"    army push: {len(pushes)} time{'s' if len(pushes) != 1 else ''}, share now {pushes[-1].get('share')} (normally {pushes[-1].get('base')}): {pushes[-1].get('why')}")
    else:
        out.append("  none")
    out.append("")

    out.append("EXPEDITIONS (sent, what came back, net resources and dark matter found, ships found and lost)")
    exp_events = [e for e in events if e["event"] in ("expedition_sent", "expedition_result", "expedition_refused", "expedition_skipped")]
    if exp_events:
        for a in sorted({e["agent"] for e in exp_events}):
            mine = [e for e in exp_events if e["agent"] == a]
            results = [e for e in mine if e["event"] == "expedition_result"]
            outcomes = Counter(e.get("outcome", "other") for e in results)
            found = {r: sum(int(e.get(r) or 0) for e in results) for r in ("metal", "crystal", "deuterium", "dark_matter")}
            ships_found: Counter = Counter()
            ships_lost: Counter = Counter()
            for e in results:
                ships_found.update({k: int(v) for k, v in (e.get("ships") or {}).items()})
                ships_lost.update({k: int(v) for k, v in (e.get("lost") or {}).items()})
            lost_value = sum(int(e.get("lost_value") or 0) for e in results)
            gained = found["metal"] + found["crystal"] + found["deuterium"]
            sent = sum(1 for e in mine if e["event"] == "expedition_sent")
            refused = sum(1 for e in mine if e["event"] == "expedition_refused")
            out.append(f"  {a}: {sent} sent, {len(results)} reports"
                       + (f", {refused} refused" if refused else "")
                       + (": " + ", ".join(f"{k} {v}" for k, v in outcomes.most_common()) if outcomes else ""))
            out.append(f"    found metal {found['metal']:,}, crystal {found['crystal']:,}, deuterium {found['deuterium']:,}, "
                       f"dark matter {found['dark_matter']:,}; ships lost worth {lost_value:,}; net resources {gained - lost_value:+,}")
            if ships_found:
                out.append("    ships found: " + ", ".join(f"{n} {k}" for k, n in sorted(ships_found.items())))
            if ships_lost:
                out.append("    ships lost: " + ", ".join(f"{n} {k}" for k, n in sorted(ships_lost.items())))
            stayed = Counter(str(e.get("why", "")).split(":")[0] for e in mine if e["event"] == "expedition_skipped")
            if stayed:
                out.append("    stayed home: " + ", ".join(f"{k} x{n}" for k, n in stayed.most_common(4)))
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

    out += moons_section(events)
    out += alliances_section(events)

    out.append("BOARD AND TRADE (galaxy board posts; deals with other agents, goods go in small instalments, a cheat is shunned)")
    posts = [e for e in events if e["event"] == "board_post"]
    reads = [e for e in events if e["event"] == "board_read"]
    offers = [e for e in events if e["event"] == "trade_offer"]
    tsent = [e for e in events if e["event"] == "trade_sent"]
    trecv = [e for e in events if e["event"] == "trade_received"]
    cheated = [e for e in events if e["event"] == "trade_cheated"]
    if posts or reads or offers or tsent or trecv or cheated:
        kinds = Counter(str(e.get("kind")) for e in posts)
        out.append(f"  {len(posts)} posts ({', '.join(f'{k} {n}' for k, n in kinds.most_common()) or 'none'}), "
                   f"{len(reads)} reads with news ({sum(int(e.get('warnings') or 0) for e in reads)} warnings, "
                   f"{sum(int(e.get('revenge') or 0) for e in reads)} revenge calls, guard raised {sum(1 for e in reads if e.get('guard'))}x)")
        acts = Counter(str(e.get("action")) for e in offers)
        out.append(f"  deals: {acts['posted']} offers posted, {acts['answered']} answered, {acts['accepted']} accepted, {acts['done']} completed, "
                   f"{acts['skipped'] + acts['refused']} instalments skipped or refused")
        moved: Counter = Counter()
        for e in tsent:
            for r, v in (e.get("resources") or {}).items():
                moved[r] += int(v or 0)
        out.append(f"  goods: {len(tsent)} instalments sent (worth {sum(int(e.get('value') or 0) for e in tsent):,}: "
                   f"{', '.join(f'{r} {v:,}' for r, v in moved.items()) or 'nothing'}), {len(trecv)} deliveries received "
                   f"(worth {sum(int(e.get('value') or 0) for e in trecv):,}), {len(cheated)} cheats")
        per: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])
        for e in posts:
            per[e["agent"]][0] += 1
        for e in tsent:
            per[e["agent"]][1] += int(e.get("value") or 0)
        for e in trecv:
            per[e["agent"]][2] += int(e.get("value") or 0)
        for e in cheated:
            per[e["agent"]][3] += 1
        for a in sorted(per):
            n, out_v, in_v, bad = per[a]
            out.append(f"    {a}: {n} posts, sent {out_v:,}, received {in_v:,}{f', cheated {bad}x' if bad else ''}")
        for e in cheated[-5:]:
            out.append(f"    {clock(e['t'])} {e['agent']} was cheated by {e.get('partner')}: owed {int(e.get('owed') or 0):,} "
                       f"(sent {int(e.get('sent') or 0):,}, got {int(e.get('received') or 0):,}); shunned")
        learned = [e for e in events if e["event"] == "price_learned"]
        if learned:
            out.append("  PRICES LEARNED (markup r = value asked / value given, 1.00 = fair 3:2:1; 'ask' = our offers, 'accept' = the most we take)")
            books: dict = {}
            for e in learned:
                kind = str(e.get("kind") or "ask")
                first = e.get("start") if kind == "ask" and e.get("start") is not None else e.get("old")
                b = books.setdefault((e["agent"], str(e.get("pair")), kind),
                                     {"start": first, "now": e.get("new"), "answered": 0, "unanswered": 0, "completed": 0, "loosened": 0})
                b["now"] = e.get("new")
                if e.get("why") in ("answered", "unanswered", "completed"):
                    b[e["why"]] += 1
                elif e.get("why") == "need":
                    b["loosened"] += 1
            for (a, pair, kind), b in sorted(books.items()):
                tail = (f"answered {b['answered']}, unanswered {b['unanswered']}, completed {b['completed']}" if kind == "ask"
                        else f"loosened {b['loosened']}x after unmet need, deals completed {b['completed']}")
                out.append(f"    {a} {pair} {kind}: start {float(b['start']):.2f} -> now {float(b['now']):.2f} ({tail})")
    else:
        out.append("  none (no board in use: start board.py and run_swarm.py --board URL)")
    out.append("")

    out.append("BOUNTIES AND REVENGE (revenge joins; bounties posted, claimed, confirmed by the referee, paid or not)")
    joins = [e for e in events if e["event"] == "revenge_join"]
    dones = [e for e in events if e["event"] == "revenge_done"]
    bposts = [e for e in events if e["event"] == "bounty_post"]
    bclaims = [e for e in events if e["event"] == "bounty_claim"]
    bverdicts = [e for e in events if e["event"] == "bounty_verdict"]
    bpaid = [e for e in events if e["event"] == "bounty_paid"]
    bunpaid = [e for e in events if e["event"] == "bounty_unpaid"]
    if joins or dones or bposts or bclaims or bverdicts or bpaid or bunpaid:
        by_join = Counter(e["agent"] for e in joins)
        out.append(f"  revenge: {len(joins)} joins ({', '.join(f'{a} {n}' for a, n in by_join.most_common()) or 'none'}), "
                   f"{len(dones)} ended by a raid on the named raider")
        for e in joins[-5:]:
            out.append(f"    {clock(e['t'])} {e['agent']} joined {e.get('victim')}'s call against {e.get('raider_at')} (player {e.get('raider_id')})")
        reward_total = sum(int(e.get("value") or 0) for e in bposts)
        mine_ok = [e for e in bverdicts if e.get("role") == "claimant" and e.get("ok")]
        mine_no = [e for e in bverdicts if e.get("role") == "claimant" and not e.get("ok")]
        pay_out = [e for e in bpaid if e.get("side") == "poster"]
        pay_in = [e for e in bpaid if e.get("side") == "claimant"]
        late_claimant = [e for e in bunpaid if e.get("side") == "claimant"]
        late_poster = [e for e in bunpaid if e.get("side") == "poster"]
        out.append(f"  bounties: {len(bposts)} posted (rewards worth {reward_total:,}), {len(bclaims)} claims, {len(mine_ok)} confirmed, "
                   f"{len(mine_no)} rejected, {len(pay_out)} paid by the poster (worth {sum(int(e.get('value') or 0) for e in pay_out):,}), "
                   f"{len(pay_in)} received, {len(late_claimant)} never paid, {len(late_poster)} the poster gave up on")
        for e in bposts[-6:]:
            key = str(e.get("bounty"))
            claim = next((c for c in bclaims if str(c.get("bounty")) == key), None)
            verdict = next((v for v in bverdicts if str(v.get("bounty")) == key and v.get("role") == "claimant"), None)
            paid = any(str(p.get("bounty")) == key for p in bpaid)
            unpaid = any(str(p.get("bounty")) == key for p in bunpaid)
            state = ("paid" if paid else "NOT PAID" if unpaid else
                     "confirmed, payment due" if verdict and verdict.get("ok") else "claim rejected" if verdict else
                     f"claimed by {claim['agent']}, no verdict yet" if claim else "open")
            out.append(f"    {clock(e['t'])} {e['agent']} wants {e.get('target')} (player {e.get('target_id')}) for {_fmt_res(e.get('reward'))}: {state}")
        for e in mine_no[-3:]:
            out.append(f"    {clock(e['t'])} {e['agent']}'s claim on bounty {e.get('bounty')} was rejected: {e.get('why')}")
        for e in late_claimant[-3:]:
            out.append(f"    {clock(e['t'])} {e['agent']} was never paid by {e.get('poster')} (bounty {e.get('bounty')}); shunned")
    else:
        out.append("  none (needs the board and the referee: start board.py, referee.py and run_swarm.py --board URL)")
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

    out += llm_section(events, llm_calls or [])

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


def read_llm_calls(log_dir: Path, since: float = 0.0) -> list[dict]:
    """The calls in <logs>/llm.jsonl (swarm/llm.py). Missing file or broken lines: fewer calls, never an error."""
    calls = []
    try:
        with (log_dir / "llm.jsonl").open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("kind") == "llm_call" and (e.get("t") or 0) >= since:
                    calls.append(e)
    except OSError:
        pass
    return calls


def llm_section(events: list[dict], calls: list[dict]) -> list[str]:
    """LLM: calls, failures, latency, and what the voice did with the answers (only when a language model was used)."""
    rewritten = [e for e in events if e["event"] == "voice_post"]
    diary = [e for e in events if e["event"] == "diary"]
    if not (calls or rewritten or diary):
        return []
    out = ["LLM (the inner voice: text only, it changes no decision; see llm.jsonl for every prompt and answer)"]
    if calls:
        failed = [c for c in calls if not c.get("ok")]
        lat = [float(c["latency"]) for c in calls if c.get("ok") and isinstance(c.get("latency"), (int, float))]
        tokens = sum(int(c["tokens"]) for c in calls if isinstance(c.get("tokens"), int))
        out.append(f"  {len(calls)} calls, {len(failed)} failed, average latency {sum(lat) / len(lat):.1f} s"
                   f" (slowest {max(lat):.1f} s), {tokens:,} tokens reported" if lat else
                   f"  {len(calls)} calls, {len(failed)} failed, no answer in time")
        for why, n in Counter(str(c.get("error", ""))[:50] for c in failed).most_common(3):
            out.append(f"    failed {n}x: {why}")
        for purpose, n in Counter(str(c.get("purpose", "?")).split(":")[0] for c in calls).most_common():
            out.append(f"    {n}x {purpose}")
    else:
        out.append("  no call log found (llm.jsonl)")
    out.append(f"  {len(rewritten)} board posts rewritten, {len(diary)} diary lines")
    for e in (rewritten + diary)[-4:]:
        what = f"{e.get('kind')} post: {e.get('text')}" if e["event"] == "voice_post" else f"diary: {e.get('text')}"
        out.append(f"    {clock(e['t'])} {e['agent']} {what}"[:200])
    out.append("")
    return out


def moons_section(events: list[dict]) -> list[str]:
    """MOONS: sensor phalanx building and scans, the warnings they gave, deuterium spent."""
    out = ["MOONS (sensor phalanx: scans cost 5,000 deuterium each; a warning is an attack seen flying to one of our planets)"]
    names = ("moon_seen", "moon_fed", "phalanx_built", "phalanx_scan", "phalanx_warning", "phalanx_skipped")
    ev = [e for e in events if e["event"] in names]
    if not ev:
        return out + ["  none", ""]
    seen = {(e["agent"], e.get("moon")) for e in ev if e["event"] == "moon_seen"}
    scans = [e for e in ev if e["event"] == "phalanx_scan"]
    warns = [e for e in ev if e["event"] == "phalanx_warning"]
    fed = [e for e in ev if e["event"] == "moon_fed"]
    built = [e for e in ev if e["event"] == "phalanx_built"]
    skips = [e for e in ev if e["event"] == "phalanx_skipped"]
    carried: Counter = Counter()
    for e in fed:
        for r, v in (e.get("resources") or {}).items():
            carried[r] += int(v or 0)
    spent = sum(int(e.get("cost") or 0) for e in scans)
    out.append(f"  {len(seen)} moons seen, {len(built)} phalanx upgrades finished, {len(fed)} deliveries to moons "
               f"({', '.join(f'{r} {v:,}' for r, v in carried.items()) or 'nothing'})")
    out.append(f"  {len(scans)} scans ({spent:,} deuterium; {sum(1 for e in scans if e.get('fleets'))} showed fleets, "
               f"{sum(1 for e in scans if e.get('away'))} showed a rival fleet away), {len(warns)} warnings")
    for e in warns[-5:]:
        out.append(f"    {clock(e['t'])} {e['agent']} attack from {e.get('attacker')} on {e.get('planet')}: {e.get('ships')} ships, "
                   f"{e.get('lands_in')} s ahead")
    for why, n in Counter(str(e.get("why", ""))[:70] for e in skips).most_common(3):
        out.append(f"    skipped {n}x: {why}")
    out.append("")
    return out


def alliances_section(events: list[dict]) -> list[str]:
    """ALLIANCES: who founded one, who applied, was let in or left, and what allies told each other."""
    out = ["ALLIANCES (agents team up: they share spy reports in the alliance chat and warn each other of attacks)"]
    names = ("alliance_founded", "alliance_applied", "alliance_accepted", "alliance_rejected", "alliance_joined", "alliance_left",
             "alliance_kicked", "intel_shared", "intel_used", "ally_warned", "alliance_skipped")
    ev = [e for e in events if e["event"] in names]
    if not ev:
        return out + ["  none", ""]
    by = defaultdict(list)
    for e in ev:
        by[e["event"]].append(e)
    out.append(f"  {len(by['alliance_founded'])} founded, {len(by['alliance_applied'])} applications "
               f"({len(by['alliance_accepted'])} accepted, {len(by['alliance_rejected'])} rejected), {len(by['alliance_joined'])} joined, "
               f"{len(by['alliance_left'])} left, {len(by['alliance_kicked'])} kicked out")
    for e in by["alliance_founded"][-8:]:
        members = sum(1 for a in by["alliance_accepted"] if a.get("alliance") == e.get("alliance"))
        out.append(f"    {clock(e['t'])} {e['agent']} founded {e.get('alliance')} \"{e.get('name')}\" (+{members} accepted since)")
    shared, used = by["intel_shared"], by["intel_used"]
    out.append(f"  {len(shared)} spy reports shared ({sum(int(e.get('loot') or 0) for e in shared):,} loot in them), {len(used)} used by an ally as a target "
               f"({sum(int(e.get('loot') or 0) for e in used):,} loot), {len(by['ally_warned'])} warnings of incoming attacks")
    for why, n in Counter(str(e.get("reason", "")) for e in by["alliance_left"]).most_common(3):
        out.append(f"    left {n}x: {why}")
    for why, n in Counter(str(e.get("why", "")) for e in by["alliance_rejected"]).most_common(3):
        out.append(f"    rejected {n}x: {why}")
    for why, n in Counter(str(e.get("why", ""))[:70] for e in by["alliance_skipped"]).most_common(3):
        out.append(f"    skipped {n}x: {why}")
    out.append("")
    return out


def _fmt_res(d) -> str:
    if not isinstance(d, dict):
        return "?"
    return ", ".join(f"{int(v or 0):,} {r}" for r, v in d.items()) or "nothing"


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
    text = summarize(read_events(log_dir, since), personas_from_state(Path(args.state)), read_llm_calls(log_dir, since))
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"(also saved to {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
