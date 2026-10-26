"""Pact limits: slots by persona, raiders only pact with those they could not raid, expiry and renewal, betrayal by ambition."""
import json
import unittest
from dataclasses import replace

import observatory
import summarize_logs as sl
from swarm import diplomacy as D
from swarm.military import Memory
from swarm.personas import PRESETS
from tests.test_diplomacy import RAIDER, QUIET, T0, World, row

DAY = 86400.0
STATS = {"light_fighter": {"cargo_capacity": 50}, "small_cargo": {"cargo_capacity": 5000}}
HOME = {"light_fighter": 300, "small_cargo": 20}


def pact_rel(now=T0, trust=60.0, **kw):
    rel = D.new_rel(now)
    rel.update(step="pact", trust=trust, **kw)
    D.start_pact(rel, now)
    return rel


class SlotTests(unittest.TestCase):
    def test_slots_by_persona(self):
        got = {k: D.pact_slots(p) for k, p in PRESETS.items()}
        self.assertEqual(got["raider"], 1)
        self.assertEqual(got["balanced"], 3)
        self.assertEqual((got["miner"], got["turtle"]), (6, 6))
        self.assertEqual((got["scientist"], got["trader"]), (5, 5))
        for n in got.values():
            self.assertTrue(D.SLOTS_MIN <= n <= D.SLOTS_MAX)

    def test_trait_boundaries(self):
        p = replace(PRESETS["raider"], aggression=0.6, sociability=0.5)
        self.assertEqual(D.pact_slots(p), 2)
        self.assertEqual(D.pact_slots(replace(p, sociability=0.1)), 1)
        self.assertEqual(D.pact_slots(replace(p, aggression=0.45)), 2)
        self.assertEqual(D.pact_slots(replace(p, aggression=0.3, sociability=0.1)), 2)
        self.assertEqual(D.pact_slots(replace(p, aggression=0.0, caution=0.0, sociability=0.0)), 4)
        self.assertEqual(D.pact_slots(object()), 4)          # no traits at all: peaceful default

    def test_slot_count_counts_valid_pacts_and_alliances(self):
        st = D.new_state()
        st["rel"] = {"1": pact_rel(T0), "2": {**D.new_rel(T0), "step": "alliance"}, "3": {**D.new_rel(T0), "step": "friendly"},
                     "4": pact_rel(T0 - 5 * DAY)}
        self.assertEqual(D.pact_used(st, T0 + 10), 2)        # the pact that ran out does not count


class HardCheckTests(unittest.TestCase):
    def rel(self, **kw):
        r = D.new_rel(T0)
        r.update({"step": "friendly", "trust": 60.0, **kw})
        return r

    def test_full_slots_refuse_a_pact_both_ways(self):
        miner = PRESETS["miner"]
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, miner, slots_used=5))
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, miner, slots_used=6), "pact slots full")
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, None, slots_used=99))     # no persona, no limit (old callers)
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, miner))                    # not counted: not checked

    def test_an_alliance_step_does_not_need_a_new_slot(self):
        rel = self.rel(step="pact", trust=80.0)
        self.assertIsNone(D.hard_check(rel, "alliance", T0, replace(PRESETS["miner"], sociability=0.9), slots_used=6))

    def test_a_raider_refuses_a_pact_with_a_target_or_without_information(self):
        r = PRESETS["raider"]
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=None), "looks like a target")
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info={}), "looks like a target")
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info={"ratio": 0.3}), "looks like a target")
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info={"ratio": 1.0}), "looks like a target")

    def test_a_raider_accepts_a_partner_it_could_not_raid(self):
        r = PRESETS["raider"]
        strong = {"ratio": D.STRONGER_RATIO}
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=strong))
        well_defended = {"ratio": 1.0, "strength": 100_000, "loot": 10_000, "age": 3600}
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=well_defended))
        poor = {"ratio": 0.8, "strength": 10, "loot": r.min_loot - 1, "age": 60}
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=poor))

    def test_weak_and_rich_is_a_target_and_old_reports_do_not_count(self):
        r = PRESETS["raider"]
        rich = {"ratio": 0.8, "strength": 1_000, "loot": 20_000, "age": 3600}
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=rich), "looks like a target")
        stale = {"ratio": 0.8, "strength": 10 ** 9, "loot": 20_000, "age": D.PACT_INTEL_MAX_AGE + 1}
        self.assertEqual(D.hard_check(self.rel(), "pact", T0, r, slots_used=0, pact_info=stale), "looks like a target")

    def test_peaceful_personas_are_not_asked_about_targets(self):
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, PRESETS["turtle"], slots_used=0, pact_info={"ratio": 0.1}))
        self.assertIsNone(D.hard_check(self.rel(), "pact", T0, PRESETS["balanced"], slots_used=0, pact_info={"ratio": 0.1}))   # 0.3 < RAIDER_AT


class ExpiryTests(unittest.TestCase):
    def test_a_pact_holds_until_the_end_and_is_renewed_in_the_last_stretch(self):
        rel = pact_rel(T0)
        self.assertEqual(rel["pact_until"], T0 + D.PACT_LIFE)
        events = []
        self.assertEqual(D.settle(rel, T0 + DAY, events=events), [])
        self.assertEqual((events, rel["pact_until"]), ([], T0 + D.PACT_LIFE))
        late = T0 + D.PACT_LIFE - 3600
        self.assertEqual(D.settle(rel, late, events=events), [])
        self.assertEqual(events, ["renewed"])
        self.assertEqual((rel["step"], rel["pact_until"], rel["renewals"]), ("pact", late + D.PACT_LIFE, 1))
        self.assertEqual(D.settle(rel, late + 60, events=events), [])      # not again straight away
        self.assertEqual(events, ["renewed"])

    def test_without_trust_or_with_a_grudge_it_expires_to_friendly(self):
        rel = pact_rel(T0, trust=30.0)                                      # still a pact (above 25) but under PACT_AT
        events = []
        self.assertEqual(D.settle(rel, T0 + D.PACT_LIFE - 60, events=events), [])
        self.assertEqual(D.settle(rel, T0 + D.PACT_LIFE, events=events), [("pact", "friendly")])
        self.assertEqual((events, rel["step"], rel["pact_until"]), (["expired"], "friendly", 0.0))
        self.assertGreater(rel["cool_until"], T0 + D.PACT_LIFE)
        grudge = pact_rel(T0, last_hit_at=T0 + D.PACT_LIFE - 60)
        ev = []
        self.assertEqual(D.settle(grudge, T0 + D.PACT_LIFE, events=ev), [("pact", "friendly")])
        self.assertEqual(ev, ["expired"])

    def test_the_agents_own_check_can_stop_a_renewal(self):
        rel = pact_rel(T0)
        ev = []
        self.assertEqual(D.settle(rel, T0 + D.PACT_LIFE, renew_ok=False, events=ev), [("pact", "friendly")])
        self.assertEqual(ev, ["expired"])

    def test_an_alliance_never_expires_and_falling_back_to_a_pact_starts_a_clock(self):
        rel = D.new_rel(T0)
        rel.update(step="alliance", trust=80.0)
        self.assertEqual(D.settle(rel, T0 + 100 * DAY), [])
        rel["trust"] = 30.0
        self.assertEqual(D.settle(rel, T0 + 100 * DAY), [("alliance", "pact")])
        self.assertEqual(rel["pact_until"], T0 + 100 * DAY + D.PACT_LIFE)

    def test_peace_ids_only_lists_valid_pacts(self):
        st = D.new_state()
        st["rel"] = {"1": pact_rel(T0), "2": {**D.new_rel(T0), "step": "alliance"}, "3": {**D.new_rel(T0), "step": "neutral"}}
        self.assertEqual(D.peace_ids(st, T0 + 10), {1, 2})
        self.assertEqual(D.peace_ids(st, T0 + D.PACT_LIFE), {2})
        self.assertEqual(D.peace_ids(st), {1, 2})                            # no clock given: as before

    def test_an_old_saved_pact_without_dates_stays_valid_and_gets_a_clock(self):
        raw = {"rel": {"12": {"step": "pact", "trust": 70, "name": "x"}}}
        st = D.clean_state(raw)
        rel = st["rel"]["12"]
        self.assertEqual((rel["pact_until"], rel["renewals"], rel["we_betrayed"]), (0.0, 0, 0))
        self.assertEqual(D.peace_ids(st, T0), {12})
        D.settle(rel, T0)
        self.assertEqual(rel["pact_until"], T0 + D.PACT_LIFE)
        self.assertEqual(st["betray_at"], 0.0)
        junk = D.clean_rel({"step": "pact", "pact_until": "soon", "renewals": None, "we_betrayed": float("nan")})
        self.assertEqual((junk["pact_until"], junk["renewals"], junk["we_betrayed"]), (0.0, 0, 0))

    def test_it_survives_a_save_and_load(self):
        mem = Memory()
        mem.diplomacy["rel"]["12"] = pact_rel(T0)
        mem.diplomacy["betray_at"] = 5.0
        back = D.clean_state(json.loads(json.dumps(mem.diplomacy)))
        self.assertEqual(back["rel"]["12"]["pact_until"], T0 + D.PACT_LIFE)
        self.assertEqual(back["betray_at"], 5.0)


class AgentExpiryTests(World):
    def test_an_expired_pact_no_longer_protects_and_is_logged(self):
        a = self.agent(1, persona=QUIET)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=30.0, coord="1:5:7")
        D.start_pact(rel, T0)
        a._players = (T0, [row(12, system=5, position=7)], 100)
        self.assertEqual(a._diplo_peace_ids(), {12})
        self.assertTrue(a._diplo_protected((1, 5, 7), 12))
        a.now = T0 + D.PACT_LIFE + 5
        a._diplo_settle(12, rel)
        self.assertEqual(rel["step"], "friendly")
        self.assertEqual(a._diplo_peace_ids(), set())
        self.assertFalse(a._diplo_protected((1, 5, 7), 12))
        self.assertEqual(len(self.events(a, "diplo_expired")), 1)
        self.assertEqual(self.events(a, "diplo_step")[-1]["why"], "pact expired")

    def test_peaceful_time_renews_and_logs(self):
        a = self.agent(1, persona=QUIET)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=60.0)
        D.start_pact(rel, T0)
        a.now = T0 + D.PACT_LIFE - 600
        a._diplo_settle(12, rel)
        self.assertEqual(rel["step"], "pact")
        self.assertEqual(len(self.events(a, "diplo_renewed")), 1)

    def test_a_raider_does_not_renew_a_pact_with_someone_it_could_raid(self):
        a = self.agent(1, persona=RAIDER)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=60.0, coord="1:5:7")
        D.start_pact(rel, T0)
        a._players = (T0, [row(12, system=5, position=7, score=50)], 100)
        a.now = T0 + D.PACT_LIFE - 600
        a._diplo_settle(12, rel)
        self.assertEqual(rel["step"], "pact")           # the end is not here yet
        a.now = T0 + D.PACT_LIFE + 1
        a._diplo_settle(12, rel)
        self.assertEqual(rel["step"], "friendly")
        # ... but with a far stronger partner it renews
        b = self.agent(2, persona=RAIDER)
        rb = self.meet(b, 11)
        rb.update(step="pact", trust=60.0, coord="1:5:7")
        D.start_pact(rb, T0)
        b._players = (T0, [row(11, system=5, position=7, score=5000)], 100)
        b.now = T0 + D.PACT_LIFE - 600
        b._diplo_settle(11, rb)
        self.assertEqual(len(self.events(b, "diplo_renewed")), 1)

    def test_slots_full_blocks_a_proposal_and_an_answer(self):
        a = self.agent(1, persona=PRESETS["balanced"])      # 3 slots
        for uid in (21, 22, 23):
            r = D.new_rel(T0)
            r.update(step="pact", trust=60.0)
            D.start_pact(r, T0)
            a.memory.diplomacy["rel"][str(uid)] = r
        rel = self.meet(a, 12)
        rel.update(step="friendly", trust=60.0, opinion=20.0)
        self.assertEqual(a._diplo_hard(12, rel, "pact"), "pact slots full")
        self.assertIsNone(self.agent(2, persona=QUIET)._diplo_hard(11, {**D.new_rel(T0), "step": "friendly", "trust": 60.0}, "pact"))
        a._players = (T0, [row(12)], 100)
        a.memory.diplomacy["scan_at"] = T0 + 10 ** 6
        self.assertIsNone(a._diplo_pick())
        a._diplo_pick()
        a.memory.diplomacy["rel"]["12"]["step"] = "friendly"
        a._diplo_pick()
        self.assertEqual([e["reason"] for e in self.events(a, "diplo_declined")], ["pact slots full"])   # logged once a day

    def test_advancing_to_a_pact_sets_the_dates(self):
        a = self.agent(1, persona=QUIET)
        rel = self.meet(a, 12)
        rel["step"] = "friendly"
        a._diplo_advance(12, rel, "pact", "test")
        self.assertEqual((rel["pact_since"], rel["pact_until"]), (T0, T0 + D.PACT_LIFE))

    def test_slots_line_in_the_log(self):
        a = self.agent(1, persona=QUIET)
        rel = self.meet(a, 12)
        rel.update(step="pact", trust=60.0)
        D.start_pact(rel, T0)
        a._diplo_log_slots()
        a._diplo_log_slots()
        ev = self.events(a, "diplo_slots")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["used"], ev[0]["slots"], ev[0]["next_expiry"]), (1, 6, round(T0 + D.PACT_LIFE)))


class BetrayalTests(World):
    def lucky_day(self, name, lucky=True, start=20):
        return next(d for d in range(start, start + 500) if D.betray_draw(name, d) == lucky)

    def ruthless(self, i=1, **kw):
        a = self.agent(i, persona=replace(RAIDER, min_loot=100, **kw))
        a.client = Fake()
        a.ship_stats = STATS
        a.my_tech = (0, 0, 0)
        return a

    def partner(self, a, step="pact"):
        rel = self.meet(a, 12)
        rel.update(step=step, trust=70.0, coord="1:5:7")
        D.start_pact(rel, a.now)
        a._players = (a.now, [row(12, system=5, position=7)], 100)
        return rel

    def set_day(self, a, day):
        a.now = day * DAY + 100
        a.memory.diplomacy["rel"]["12"]["pact_until"] = a.now + D.PACT_LIFE

    def test_the_dice_are_deterministic_and_about_fifteen_percent(self):
        self.assertEqual([D.betray_draw("agent0001", d) for d in range(50)], [D.betray_draw("agent0001", d) for d in range(50)])
        hits = sum(D.betray_draw(f"agent{n:04d}", d) for n in range(40) for d in range(100))
        self.assertTrue(0.10 < hits / 4000 < 0.20, hits)

    def test_the_draw_does_not_touch_the_persona_rng(self):
        a = self.ruthless()
        self.partner(a)
        state = a.rng.getstate()
        for d in range(20, 40):
            self.set_day(a, d)
            a._diplo_betray_open()
        self.assertEqual(a.rng.getstate(), state)

    def test_only_a_ruthless_agent_with_a_valid_pact_on_a_lucky_day(self):
        a = self.ruthless()
        self.partner(a)
        self.set_day(a, self.lucky_day(a.name, False))
        self.assertEqual(a._diplo_betray_open(), set())
        self.set_day(a, self.lucky_day(a.name, True))
        self.assertEqual(a._diplo_betray_open(), {12})
        self.assertEqual(a._diplo_peace_ids(), set())                       # open to spying and raiding today
        a.memory.diplomacy["rel"]["12"]["pact_until"] = a.now - 1            # expired pact: nothing to betray
        self.assertEqual(a._diplo_betray_open(), set())
        mild = self.agent(2, persona=replace(RAIDER, aggression=0.5, min_loot=100))
        mild.now = a.now
        r = self.meet(mild, 11)
        r.update(step="pact", trust=60.0)
        D.start_pact(r, mild.now)
        d = self.lucky_day(mild.name)
        mild.now = d * DAY + 100
        r["pact_until"] = mild.now + D.PACT_LIFE
        self.assertEqual(mild._diplo_betray_open(), set())                  # aggression 0.5 < 0.6

    def test_never_an_alliance_member_or_an_in_game_ally(self):
        a = self.ruthless()
        rel = self.partner(a, step="alliance")
        self.set_day(a, self.lucky_day(a.name))
        self.assertEqual(a._diplo_betray_open(), set())
        self.assertIn(12, a._diplo_peace_ids())
        rel["step"] = "pact"
        self.assertEqual(a._diplo_betray_open(), {12})
        a.memory.alliance["allies"] = [12]
        self.assertEqual(a._diplo_betray_open(), set())
        self.assertNotIn(12, a._without_allies({12, 13}))                  # an in-game ally is still protected

    def test_one_betrayal_per_day_cap(self):
        a = self.ruthless()
        self.partner(a)
        self.set_day(a, self.lucky_day(a.name))
        a.memory.diplomacy["betray_at"] = a.now - 3600
        self.assertEqual(a._diplo_betray_open(), set())
        a.memory.diplomacy["betray_at"] = a.now - D.BETRAY_EVERY - 1
        self.assertEqual(a._diplo_betray_open(), {12})

    def spy_report(self, a, loot, defense=None):
        a.memory.intel[(1, 5, 7)] = {"at": a.now, "loot": loot, "defenders": sum((defense or {}).values()), "owner_id": 12,
                                     "ships": {}, "defense": defense if defense is not None else {}}

    def test_a_very_profitable_clear_win_breaks_the_pact(self):
        a = self.ruthless()
        rel = self.partner(a)
        self.set_day(a, self.lucky_day(a.name))
        self.spy_report(a, 40_000)
        self.assertTrue(a._try_attack({"id": 1}, a.now, dict(HOME)))
        self.assertEqual(len(a.client.posts), 1)
        self.assertEqual((rel["step"], rel["trust"], rel["we_betrayed"]), ("contact", 0.0, 1))
        self.assertEqual(rel["betrayed_at"], a.now)
        self.assertEqual(a.memory.diplomacy["betray_at"], a.now)
        ev = self.events(a, "diplo_betrayal")
        self.assertEqual((len(ev), ev[0]["with"], ev[0]["was"], ev[0]["loot"]), (1, "agent0002", "pact", 40_000))
        self.assertGreaterEqual(ev[0]["win"], D.BETRAY_WIN_CHANCE)
        self.assertEqual(self.events(a, "diplo_step")[-1]["we_betrayed"], True)
        self.assertEqual(D.hard_check(rel, "neutral", a.now + 60), "they betrayed us, not forgotten yet")   # BETRAY_COOL blocks it
        self.assertEqual(a.memory.diplomacy["rel"]["12"]["step"], "contact")

    def test_small_loot_is_not_worth_it(self):
        a = self.ruthless()
        rel = self.partner(a)
        self.set_day(a, self.lucky_day(a.name))
        self.spy_report(a, D.BETRAY_LOOT_FLOOR - 1)
        self.assertFalse(a._try_attack({"id": 1}, a.now, dict(HOME)))
        self.assertEqual((rel["step"], a.client.posts), ("pact", []))
        self.assertIn("not worth breaking a pact", self.events(a, "diplo_blocked")[0]["why"])
        self.assertFalse(D.betray_loot_ok(a.persona, 700))
        self.assertTrue(D.betray_loot_ok(replace(a.persona, min_loot=2000), 16_000))
        self.assertFalse(D.betray_loot_ok(replace(a.persona, min_loot=2000), 15_999))

    def test_no_clear_win_no_betrayal(self):
        a = self.ruthless()
        rel = self.partner(a)
        self.set_day(a, self.lucky_day(a.name))
        self.spy_report(a, 40_000, {"rocket_launcher": 400})
        self.assertFalse(a._try_attack({"id": 1}, a.now, dict(HOME)))
        self.assertEqual((rel["step"], a.client.posts, self.events(a, "diplo_betrayal")), ("pact", [], []))

    def test_on_an_unlucky_day_the_pact_protects_as_before(self):
        a = self.ruthless()
        rel = self.partner(a)
        self.set_day(a, self.lucky_day(a.name, False))
        self.spy_report(a, 400_000)
        self.assertFalse(a._try_attack({"id": 1}, a.now, dict(HOME)))
        self.assertEqual((rel["step"], a.client.posts), ("pact", []))
        self.assertEqual(self.events(a, "diplo_blocked")[0]["why"], "pact or alliance partner")

    def test_the_victim_sees_a_betrayal(self):
        b = self.agent(2)
        rel = self.meet(b, 11)
        rel.update(step="pact", trust=80.0)
        b._diplo_battle({"role": "defender", "their_id": 11, "probes_only": False, "loot": 2000, "my_loss": 3000})
        self.assertEqual((rel["step"], rel["trust"]), ("contact", 0.0))
        self.assertTrue(self.events(b, "diplo_step")[-1]["betrayal"])


class Fake:
    """Just enough client for _try_attack: records fleet posts."""
    def __init__(self):
        self.posts = []

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"mission": {"seconds_remaining": 60}}

    def get(self, path):
        return {"ships": []}


class ReportTests(unittest.TestCase):
    def events(self):
        e = lambda event, agent, **kw: {"event": event, "agent": agent, "t": 1000, **kw}
        return [e("diplo_step", "a1", to="pact", **{"with": "a2"}), e("diplo_step", "a2", to="pact", **{"with": "a1"}),
                e("diplo_renewed", "a1", **{"with": "a2"}), e("diplo_expired", "a2", **{"with": "a1"}),
                e("diplo_betrayal", "a3", **{"with": "a4"}, loot=50000, win=0.97),
                e("diplo_slots", "a1", used=1, slots=6), e("diplo_slots", "a2", used=0, slots=6), e("diplo_slots", "a3", used=1, slots=1),
                e("diplo_slots", "a3", used=0, slots=1),
                e("diplo_answer", "a5", side="answerer", decision="REFUSE", step="pact", source="hard rule", reason="pact slots full"),
                e("diplo_answer", "a5", side="answerer", decision="REFUSE", step="pact", source="hard rule", reason="looks like a target"),
                e("diplo_answer", "a6", side="answerer", decision="REFUSE", step="pact", source="llm", reason="no"),
                e("diplo_declined", "a7", step="pact", reason="looks like a target", **{"with": "a1"})]

    def test_summary_lines(self):
        text = "\n".join(sl.diplomacy_section(self.events()))
        self.assertIn("pacts: 2 formed, 1 renewed, 1 expired, 1 broken on purpose", text)
        self.assertIn("agents with at least one pact or alliance: 1 of 3 (33%)", text)
        self.assertIn("pact slots: 0.3 used on average of 4.3 available", text)
        self.assertIn("1 pact slots full", text)
        self.assertIn("1 looks like a target,", text)
        self.assertIn("1 looks like a target (not proposed)", text)
        self.assertIn("broke its pact with a4 (loot 50000, win chance 0.97)", text)

    def test_the_section_survives_junk(self):
        junk = [{"event": "diplo_slots", "agent": "x", "used": "many", "slots": None}, {"event": "diplo_betrayal"},
                {"event": "diplo_answer", "side": "answerer", "decision": "REFUSE", "step": "pact", "reason": None}]
        self.assertTrue(sl.diplomacy_section(junk))

    def test_the_observatory_shows_slots_and_expiry(self):
        view = observatory.diplomacy_view(self.events() + [{"event": "diplo_slots", "agent": "a1", "used": 2, "slots": 6, "next_expiry": 123, "t": 5}])
        a1 = next(r for r in view["slots"] if r["agent"] == "a1")
        self.assertEqual((a1["used"], a1["slots"], a1["next_expiry"]), (2, 6, 123))
        self.assertEqual(observatory.diplomacy_view([{"event": 5}, None]).get("slots", []), [])


if __name__ == "__main__":
    unittest.main()
