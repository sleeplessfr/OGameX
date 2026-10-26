import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import summarize_logs as sl
from swarm import alliance as A
from swarm.agent import Agent, TokenStore
from swarm.military import Memory, pick_target
from swarm.personas import PRESETS, ROTATION, make_persona
from tests.fakealliance import AllianceWorld
from tests.fakegame import FakeGame

T0 = 10_000.0
QUIET = replace(PRESETS["miner"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, expansion=0.0, caution=0.0,
                expedition_interest=0.0, trade_interest=0.0, sociability=0.8)
RAIDER = replace(QUIET, name="raider", aggression=0.9, min_loot=100, sociability=0.5)


def mission(id=1, kind="attack", left=90, origin=(1, 3, 7)):
    return {"id": id, "mission": kind, "is_mine": False, "is_return": False, "seconds_remaining": left,
            "from": {"galaxy": origin[0], "system": origin[1], "position": origin[2]}, "arrive_at": T0 + left}


def other(uid, coord, score=0):
    return {"user_id": uid, "username": f"user{uid}", "coord": coord, "score": score,
            "resources": {"metal": 40000, "crystal": 20000, "deuterium": 10000}, "ships": {}, "defense": {"rocket_launcher": 0}}


class Board:
    """The galaxy board, as far as the agents see it."""
    enabled = True

    def __init__(self):
        self.posts = []

    def post(self, kind, text, data=None, ttl=None):
        self.posts.append((kind, text, data))
        return {"id": len(self.posts)}

    def read(self, since=0, kind=None, limit=50):
        return {"ok": False, "now": 0.0, "last_id": since, "posts": []}


# ---- the rules ---------------------------------------------------------------------------------------------------------
class RuleTests(unittest.TestCase):
    def test_messages_round_trip_and_junk_is_ignored(self):
        text = A.fmt_intel((1, 2, 3), 45, 12000, 3, 120.7)
        self.assertEqual(text, "INTEL T=1:2:3 O=45 L=12000 D=3 A=120")
        self.assertEqual(A.parse_message(text), {"kind": "INTEL", "T": (1, 2, 3), "O": 45, "L": 12000, "D": 3, "A": 120})
        unknown = A.parse_message(A.fmt_intel((1, 2, 3), None, 5, None, 0))
        self.assertEqual((unknown["O"], unknown["D"]), (None, None))
        self.assertEqual(A.parse_message(A.fmt_warn((1, 3, 8), (1, 5, 5), 300)), {"kind": "WARN", "AT": (1, 3, 8), "FROM": (1, 5, 5), "ETA": 300})
        for junk in ("hello there", "", None, "INTEL", "INTEL T=x L=1", "INTEL T=1:2 L=1", "WARN AT=1:1:1", "INTEL T=1:2:3 L=abc", "WARN AT=1:1:1 FROM=2:2:2 ETA=x"):
            self.assertIsNone(A.parse_message(junk), junk)

    def test_state_survives_a_round_trip_and_junk(self):
        m = Memory()
        m.alliance.update(id=4, tag="MINE01", allies=[2, 3], shared={"1:2:3": 5.0})
        back = Memory.from_dict(m.to_dict())
        self.assertEqual((back.alliance["id"], back.alliance["tag"], back.alliance["allies"]), (4, "MINE01", [2, 3]))
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).alliance, A.new_state())
        self.assertEqual(Memory.from_dict({"alliance": "junk"}).alliance, A.new_state())
        clean = A.clean_state({"id": [1], "allies": "x", "shared": [], "next_at": "soon", "off": 1, "size": "3"})
        self.assertEqual((clean["id"], clean["allies"], clean["shared"], clean["next_at"], clean["off"], clean["size"]), (None, [], {}, 0.0, True, 3))

    def test_identity_is_valid_and_unique(self):
        taken_tags, taken_names = [], []
        for number in (1, 1, 1, 101, 7):
            tag, name = A.make_identity("miner", number, taken_tags, taken_names)
            self.assertTrue(3 <= len(tag) <= 8, tag)
            self.assertTrue(3 <= len(name) <= 30, name)
            self.assertNotIn(tag.lower(), [t.lower() for t in taken_tags])
            self.assertNotIn(name.lower(), [n.lower() for n in taken_names])
            taken_tags.append(tag)
            taken_names.append(name)
        self.assertTrue(A.make_identity("scientist", 12, [], [])[0].startswith("SCI"))
        self.assertEqual(A.agent_number("agent0042"), 42)
        self.assertEqual(A.agent_number("x"), 0)

    def test_who_may_found(self):
        ok = dict(points=500, age=3600, agent_count=10, alliances=1, now=0.0, cooldown_until=0.0)
        self.assertTrue(A.can_found(QUIET, **ok)[0])
        self.assertFalse(A.can_found(replace(QUIET, sociability=0.5), **ok)[0])         # not sociable enough
        self.assertFalse(A.can_found(RAIDER, **ok)[0])
        self.assertFalse(A.can_found(QUIET, **{**ok, "points": 10})[0])
        self.assertFalse(A.can_found(QUIET, **{**ok, "age": 60})[0])
        self.assertFalse(A.can_found(QUIET, **{**ok, "alliances": 2})[0])               # 10 agents, 2 alliances: enough
        self.assertTrue(A.can_found(QUIET, **{**ok, "alliances": 0, "agent_count": 3})[0])
        self.assertFalse(A.can_found(QUIET, **{**ok, "alliances": 1, "agent_count": 3})[0])   # always room for one, no more at 3 agents
        self.assertFalse(A.can_found(QUIET, **{**ok, "cooldown_until": 10.0, "now": 5.0})[0])
        self.assertEqual([A.founding_cap(n) for n in (1, 4, 5, 9, 10, 25)], [1, 1, 1, 1, 2, 5])

    def test_compatibility_prefers_neighbours_and_like_minds(self):
        near = A.compat("miner", (1, 3, 8), [(1, 5, 6)], ["miner"])
        far = A.compat("miner", (1, 3, 8), [(1, 400, 6)], ["miner"])
        enemy = A.compat("miner", (1, 3, 8), [(1, 5, 6)], ["raider"])
        self.assertGreater(near, far)
        self.assertGreater(near, enemy)
        self.assertEqual(A.compat("miner", (1, 3, 8), [], []), 0.0)
        self.assertGreater(A.similarity("miner", "turtle"), A.similarity("miner", "raider"))
        self.assertEqual(A.similarity("miner", None), 0.5)

    def test_picking_an_alliance(self):
        def cand(i, coords, personas, members=3, open_=True):
            return {"id": i, "open": open_, "member_count": members, "coords": coords, "personas": personas}

        good = cand(1, [(1, 5, 6)], ["miner"])
        far = cand(2, [(1, 400, 6)], ["miner"])
        pick = lambda cands, aggressors=None, opponents=None: A.pick_alliance("miner", (1, 3, 8), cands, opponents or {}, aggressors or {}, 100.0)
        self.assertEqual(pick([far, good])[0]["id"], 1)
        self.assertIsNone(pick([far]))                                                   # nobody close enough
        self.assertIsNone(pick([cand(1, [(1, 5, 6)], ["miner"], open_=False)]))
        self.assertIsNone(pick([cand(1, [(1, 5, 6)], ["miner"], members=A.MAX_MEMBERS)]))
        self.assertIsNone(pick([good], aggressors={(1, 5, 6): 90.0}))                    # a member robbed us
        self.assertIsNone(pick([good], opponents={(1, 5, 6): {"losses": 2, "net": -900}}))
        self.assertIsNotNone(pick([good], aggressors={(1, 5, 6): 100.0 - A.GRUDGE_WINDOW - 1}))   # an old grudge fades

    def test_a_leaders_answer(self):
        base = dict(member_count=3, applicant_coord=(1, 5, 6), opponents={}, aggressors={}, attackers={}, now=1000.0)
        self.assertEqual(A.decide_application(**base), (True, "ok"))
        self.assertFalse(A.decide_application(**{**base, "member_count": A.MAX_MEMBERS})[0])
        self.assertEqual(A.decide_application(**{**base, "aggressors": {(1, 5, 6): 900.0}}), (False, "attacked us"))
        self.assertEqual(A.decide_application(**{**base, "attackers": {"1:5:6": 900.0}}), (False, "attacked an ally"))
        self.assertEqual(A.decide_application(**{**base, "opponents": {(1, 5, 6): {"losses": 1, "net": -5}}}), (False, "beat us in a fight"))
        self.assertTrue(A.decide_application(**{**base, "opponents": {(1, 5, 6): {"losses": 1, "net": 500}}})[0])   # we won overall: no grudge
        self.assertTrue(A.decide_application(**{**base, "applicant_coord": None})[0])

    def test_leaving_and_kicking(self):
        kw = dict(is_founder=False, robbed_by={}, now=100_000.0, joined_at=99_000.0, active_at=99_500.0)
        self.assertIsNone(A.should_leave(**kw))
        self.assertEqual(A.should_leave(**{**kw, "robbed_by": {"2": 99_900.0}}), "robbed by an ally")
        self.assertIsNone(A.should_leave(**{**kw, "robbed_by": {"2": 100_000.0 - A.ROBBED_WINDOW - 1}}))
        self.assertIn("long time", A.should_leave(**{**kw, "joined_at": 0.0, "active_at": 100_000.0 - A.IDLE_LEAVE - 1}))
        self.assertIsNone(A.should_leave(**{**kw, "is_founder": True, "robbed_by": {"2": 99_900.0}}))   # a founder cannot leave
        self.assertEqual(A.members_to_kick(True, {"2": 99_900.0}, {}, {}, 100_000.0), [2])
        self.assertEqual(A.members_to_kick(True, {}, {"1:5:5": 99_900.0}, {"3": "1:5:5", "4": "1:6:6"}, 100_000.0), [3])
        self.assertEqual(A.members_to_kick(False, {"2": 99_900.0}, {}, {}, 100_000.0), [])

    def test_what_to_share_and_what_to_use(self):
        intel = {(1, 2, 3): {"at": 950.0, "loot": 5000, "defenders": 2, "owner_id": 9},
                 (1, 2, 4): {"at": 980.0, "loot": 9000, "defenders": None, "owner_id": 10},
                 (1, 2, 5): {"at": 50.0, "loot": 99999, "defenders": 0, "owner_id": 11},          # too old
                 (1, 2, 6): {"at": 990.0, "loot": 10, "defenders": 0, "owner_id": 12},             # too poor
                 (1, 2, 7): {"at": 990.0, "loot": 50000, "defenders": 0, "owner_id": 2}}           # an ally
        c, _ = A.pick_intel_to_share(intel, {}, 1000.0, 500, ally_ids={2})
        self.assertEqual(c, (1, 2, 4))
        c, _ = A.pick_intel_to_share(intel, {"1:2:4": 980.0}, 1000.0, 500, ally_ids={2})           # that one went out already
        self.assertEqual(c, (1, 2, 3))
        self.assertIsNone(A.pick_intel_to_share({}, {}, 1000.0, 500))
        msgs = [{"id": 1, "from_user_id": 2, "text": A.fmt_intel((1, 9, 9), 77, 8000, 4, 60), "created_at": 900},
                {"id": 2, "from_user_id": 1, "text": A.fmt_intel((1, 9, 8), 78, 8000, 4, 0), "created_at": 990},      # my own
                {"id": 3, "from_user_id": 2, "text": A.fmt_intel((1, 9, 7), 3, 8000, 4, 0), "created_at": 990},       # about an ally
                {"id": 4, "from_user_id": 2, "text": A.fmt_intel((1, 9, 6), 79, 8000, 4, 0), "created_at": 1000 - A.INTEL_USE_MAX_AGE - 5},
                {"id": 5, "from_user_id": 2, "text": A.fmt_intel((1, 9, 5), 80, 50, 4, 0), "created_at": 990},         # too poor
                {"id": 6, "from_user_id": 2, "text": "hello everyone", "created_at": 990}]
        got = A.intel_from_chat(msgs, 1, 1000.0, {3}, 500)
        self.assertEqual([c for c, _ in got], [(1, 9, 9)])
        self.assertEqual(got[0][1]["at"], 1000.0 - 100 - 60)
        self.assertEqual(len(A.warnings_from_chat([{"from_user_id": 2, "text": A.fmt_warn((1, 1, 1), (2, 2, 2), 5)},
                                                   {"from_user_id": 1, "text": A.fmt_warn((1, 1, 1), (2, 2, 2), 5)}], 1)), 1)


class PersonaTests(unittest.TestCase):
    def test_sociability_by_character(self):
        for name in ("miner", "scientist", "turtle"):
            self.assertGreaterEqual(PRESETS[name].sociability, 0.7)
        self.assertLess(PRESETS["raider"].sociability, 0.3)
        self.assertTrue(0.3 <= PRESETS["balanced"].sociability <= 0.7)
        for i in range(30):
            p = make_persona(i, random.Random(i))
            self.assertTrue(0.0 <= p.sociability <= 1.0)
            self.assertAlmostEqual(p.sociability, PRESETS[ROTATION[i % len(ROTATION)]].sociability, delta=0.100001)
        self.assertEqual(replace(PRESETS["miner"]).sociability, PRESETS["miner"].sociability)
        self.assertEqual(replace(PRESETS["miner"], sociability=0.0).sociability, 0.0)

    def test_it_does_not_use_up_the_agents_random_stream(self):
        for name in ROTATION:
            rng = random.Random(5)
            before = make_persona(0, rng, [name])
            after = rng.random()
            rng2 = random.Random(5)
            replaced = PRESETS[name]
            PRESETS[name] = replace(replaced, sociability=0.0)
            try:
                make_persona(0, rng2, [name])
            finally:
                PRESETS[name] = replaced
            self.assertEqual(after, rng2.random(), name)
            self.assertEqual(before.sociability, make_persona(0, random.Random(5), [name]).sociability)   # same seed, same character

    def test_it_is_drawn_last_so_no_other_trait_moves(self):
        for name in ROTATION:
            full = make_persona(0, random.Random(5), [name])
            saved = PRESETS[name]
            PRESETS[name] = replace(saved, sociability=0.0)
            try:
                without = make_persona(0, random.Random(5), [name])
            finally:
                PRESETS[name] = saved
            self.assertEqual(replace(full, sociability=0.0), without)


# ---- agents --------------------------------------------------------------------------------------------------------------
class AgentAllianceTests(unittest.TestCase):
    def setUp(self):
        self.world = AllianceWorld()
        self.store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        self.agents: dict[str, Agent] = {}
        self.board = None

    def add(self, name, uid, coord, persona=QUIET, score=500, *, born=1.0, humans=False, others=()):
        game = FakeGame()
        game.alliance_stuff, game.world, game.uid, game.home, game.my_score = True, self.world, uid, coord, score
        game.now = T0
        game.res = {"metal": 500.0, "crystal": 500.0, "deuterium": 0.0}
        game.bq = {"name": "metal_mine", "level": 1, "end": 10 ** 9, "dur": 10 ** 9}    # the planet stays busy: only the alliance acts
        game.others = [other(*o) for o in others]
        game.slots_max = 2
        self.world.names[uid] = name
        self.store.put(name, {"user_id": uid, "persona": persona.name, "token": "x"})
        agent = Agent(name, persona, "x", self.store, random.Random(uid), client=game, attack_humans=humans)
        agent.user_id = uid
        agent.logs = []
        agent.log = lambda event, _a=agent, **f: _a.logs.append((event, f))
        agent.memory.alliance["born"] = born
        if self.board is not None:
            agent.board = self.board
        self.agents[name] = agent
        return agent

    def step(self, seconds=130.0, *names):
        for name in names or self.agents:
            self.agents[name].client.advance(seconds)
        for name in names or self.agents:
            self.agents[name].tick()

    def events(self, agent, kind):
        return [f for e, f in agent.logs if e == kind]

    def found(self, agent, tag="MINE01", name="Quiet Miners 1"):
        return self.world.post(agent.user_id, "/alliance", {"tag": tag, "name": name}, T0)["alliance"]["id"]

    def join(self, aid, agent):
        self.world.alliances[aid]["members"].append(agent.user_id)
        self.world.user_alliance[agent.user_id] = aid

    def two_neighbours(self, persona_b=QUIET):
        a = self.add("agent0001", 1, (1, 3, 8), others=[(2, (1, 5, 6))])
        b = self.add("agent0002", 2, (1, 5, 6), persona_b, others=[(1, (1, 3, 8))])
        return a, b

    # -- off switches --
    def test_a_game_without_alliance_endpoints_is_left_alone(self):
        a = self.add("agent0001", 1, (1, 3, 8))
        a.client.alliance_stuff = False
        calls = []
        real_get = a.client.get
        a.client.get = lambda path: (calls.append(path), real_get(path))[1]
        for _ in range(4):
            self.step(200.0)
        self.assertTrue(a.memory.alliance["off"])
        self.assertEqual([c for c in calls if c.startswith(("/alliance", "/chat"))], ["/alliance"])    # asked once, then never again
        self.assertEqual([p for p, _ in a.client.post_log if p.startswith(("/alliance", "/chat"))], [])
        self.assertEqual([e for e, _ in a.logs if e.startswith(("alliance", "intel", "ally"))], ["alliance_skipped"])

    def test_no_sociability_no_alliance_calls(self):
        a = self.add("agent0001", 1, (1, 3, 8), replace(QUIET, sociability=0.0))
        calls = []
        real_get = a.client.get
        a.client.get = lambda path: (calls.append(path), real_get(path))[1]
        self.step(200.0)
        self.assertEqual([c for c in calls if c.startswith(("/alliance", "/chat"))], [])
        self.assertFalse(a.memory.alliance["off"])

    # -- founding --
    def test_a_sociable_agent_founds_an_alliance_and_the_cap_holds(self):
        a = self.add("agent0001", 1, (1, 3, 8), born=T0)
        c = self.add("agent0003", 3, (1, 200, 8), born=T0)
        for n in (4, 5):
            self.store.put(f"agent000{n}", {"user_id": n, "persona": "miner", "token": "x"})      # 5 agents in all: room for one alliance
        for _ in range(40):
            self.step(130.0, "agent0001")
            if self.world.alliances:
                break
        self.assertEqual(len(self.world.alliances), 1)
        (founded,) = self.events(a, "alliance_founded")
        self.assertTrue(3 <= len(founded["alliance"]) <= 8)
        self.assertTrue(founded["alliance"].startswith("MINE"))
        self.assertEqual(self.world.user_alliance[1], founded["alliance_id"])
        self.assertTrue(a.memory.alliance["founder"])
        # c is sociable and old enough but far away: it must not found a second alliance (one per 5 agents), and does not join (too far)
        c.client.advance(3 * 3600)
        for _ in range(12):
            self.step(200.0, "agent0003")
        self.assertEqual(len(self.world.alliances), 1)
        self.assertEqual(self.events(c, "alliance_founded"), [])

    def test_a_poor_or_unsociable_agent_does_not_found(self):
        poor = self.add("agent0001", 1, (1, 3, 8), score=10)
        grumpy = self.add("agent0002", 2, (1, 9, 8), replace(QUIET, sociability=0.4))
        for _ in range(30):
            self.step(200.0)
        self.assertEqual(self.world.alliances, {})
        self.assertEqual(self.events(poor, "alliance_founded") + self.events(grumpy, "alliance_founded"), [])

    # -- applying and accepting --
    def test_a_neighbour_applies_the_leader_accepts_and_they_become_allies(self):
        a, b = self.two_neighbours()
        aid = self.found(a)
        self.step(130.0, "agent0002")
        (applied,) = self.events(b, "alliance_applied")
        self.assertEqual(applied["alliance_id"], aid)
        self.assertLessEqual(len(applied["text"]), 200)
        self.assertIn("1:5:6", applied["text"])
        self.step(130.0, "agent0001")
        (accepted,) = self.events(a, "alliance_accepted")
        self.assertEqual(accepted["applicant"], 2)
        self.step(130.0, "agent0002")
        self.assertEqual(self.events(b, "alliance_joined")[0]["alliance_id"], aid)
        self.assertEqual(b.memory.alliance["allies"], [1])
        self.step(130.0, "agent0001")
        self.assertEqual(a.memory.alliance["allies"], [2])
        self.assertEqual(self.world.alliances[aid]["members"], [1, 2])
        # it does not apply again
        self.step(130.0, "agent0002")
        self.assertEqual(len(self.events(b, "alliance_applied")), 1)

    def test_nobody_applies_to_an_alliance_of_someone_who_robbed_them(self):
        a, b = self.two_neighbours()
        self.found(a)
        b.memory.aggressors[(1, 3, 8)] = T0
        self.step(130.0, "agent0002")
        self.assertEqual(self.events(b, "alliance_applied"), [])

    def test_a_leader_rejects_a_grudge_and_an_attacker_of_a_member(self):
        a, b = self.two_neighbours()
        c = self.add("agent0003", 3, (1, 6, 6), others=[(1, (1, 3, 8))])
        aid = self.found(a)
        self.world.post(2, "/alliances/%d/apply" % aid, {"text": "hi"}, T0)
        self.world.post(3, "/alliances/%d/apply" % aid, {"text": "hi"}, T0)
        a.memory.aggressors[(1, 5, 6)] = T0 - 100                                 # b robbed the leader
        a.memory.alliance["attackers"]["1:6:6"] = T0 - 100                         # c robbed a member (the member said so in the chat)
        a.client.others = [other(2, (1, 5, 6)), other(3, (1, 6, 6))]
        self.step(130.0, "agent0001")
        self.assertEqual(sorted(e["applicant"] for e in self.events(a, "alliance_rejected")), [2, 3])
        self.assertEqual(self.events(a, "alliance_accepted"), [])
        self.assertEqual(self.world.alliances[aid]["members"], [1])

    def test_a_leader_stops_at_the_member_limit(self):
        a, b = self.two_neighbours()
        aid = self.found(a)
        self.world.alliances[aid]["members"] += list(range(100, 100 + A.MAX_MEMBERS - 1))
        self.world.post(2, "/alliances/%d/apply" % aid, {"text": "hi"}, T0)
        self.step(130.0, "agent0001")
        self.assertEqual(self.events(a, "alliance_accepted"), [])
        self.assertEqual(self.events(a, "alliance_rejected")[0]["why"], "alliance full")
        # and an applicant does not even try a full one
        c = self.add("agent0003", 3, (1, 4, 4), others=[(1, (1, 3, 8))])
        self.step(130.0, "agent0003")
        self.assertEqual(self.events(c, "alliance_applied"), [])

    # -- allies are not targets --
    def test_allowed_targets_leave_out_allies(self):
        a = self.add("agent0001", 1, (1, 3, 8), RAIDER, humans=True)
        a._players = (0.0, [{"user_id": 1}, {"user_id": 2}, {"user_id": 3}], 0)
        self.assertIsNone(a._allowed_target_ids())                                # no allies, humans allowed: anyone
        a.memory.alliance["allies"] = [2]
        self.assertEqual(a._allowed_target_ids(), {1, 3})
        a.attack_humans = False
        self.store.put("agent0003", {"user_id": 3, "persona": "miner"})
        self.store.put("agent0002", {"user_id": 2, "persona": "miner"})
        self.assertEqual(a._allowed_target_ids(), {1, 3})
        a.memory.alliance["allies"] = []
        self.assertEqual(a._allowed_target_ids(), {1, 2, 3})

    def test_a_raider_does_not_spy_on_its_ally_but_does_on_a_stranger(self):
        raider = replace(RAIDER, scout_interest=1.0, probes_wanted=2, aggression=0.9)
        for ally in (True, False):
            self.world = AllianceWorld()
            self.agents = {}
            r = self.add("agent0001", 1, (1, 3, 8), raider, humans=True, others=[(2, (1, 1, 5))])
            r.client.ships["espionage_probe"] = 3
            aid = self.found(r)
            if ally:
                self.world.alliances[aid]["members"].append(2)
                self.world.user_alliance[2] = aid
            for _ in range(8):
                self.step(130.0)
            spied = [b for p, b in r.client.post_log if p == "/planets/10/fleet" and b.get("mission") == "espionage"]
            self.assertEqual(bool(spied), not ally, f"ally={ally}")

    def test_old_reports_on_allies_are_forgotten(self):
        a, b = self.two_neighbours()
        aid = self.found(a)
        self.join(aid, b)
        a.memory.intel[(1, 5, 6)] = {"at": T0, "loot": 9000, "defenders": 0, "owner_id": None}
        a.memory.intel[(1, 9, 9)] = {"at": T0, "loot": 9000, "defenders": 0, "owner_id": 2}
        a.memory.intel[(1, 9, 8)] = {"at": T0, "loot": 9000, "defenders": 0, "owner_id": 55}
        self.step(130.0, "agent0001")
        self.assertEqual(sorted(a.memory.intel), [(1, 9, 8)])

    # -- sharing, using, warning --
    def alliance_of_three(self):
        a = self.add("agent0001", 1, (1, 3, 8), others=[(2, (1, 5, 6)), (3, (1, 6, 6))])
        b = self.add("agent0002", 2, (1, 5, 6), others=[(1, (1, 3, 8)), (3, (1, 6, 6))])
        c = self.add("agent0003", 3, (1, 6, 6), RAIDER, others=[(1, (1, 3, 8)), (2, (1, 5, 6))])
        aid = self.found(a)
        self.join(aid, b)
        self.join(aid, c)
        return a, b, c

    def chat(self):
        return [m["text"] for m in self.world.chat]

    def test_intel_is_shared_in_the_fixed_format_at_most_once_in_a_while_and_used_by_allies(self):
        a, b, c = self.alliance_of_three()
        now = T0 + 130
        b.memory.intel[(1, 9, 9)] = {"at": now - 10, "loot": 5000, "defenders": 2, "owner_id": 77}
        b.memory.intel[(1, 9, 8)] = {"at": now - 10, "loot": 4000, "defenders": None, "owner_id": 78}
        self.step(130.0, "agent0002")
        shared = [t for t in self.chat() if t.startswith("INTEL")]
        self.assertEqual(len(shared), 1)
        self.assertTrue(shared[0].startswith("INTEL T=1:9:9 O=77 L=5000 D=2 A="), shared[0])
        (event,) = self.events(b, "intel_shared")
        self.assertEqual((event["at"], event["loot"]), ("1:9:9", 5000))
        self.step(60.0, "agent0002")                                              # too soon for the second
        self.assertEqual(len([t for t in self.chat() if t.startswith("INTEL")]), 1)
        # an ally reads it and gets a target (only an agent that attacks does)
        self.step(130.0, "agent0003")
        self.assertIn((1, 9, 9), c.memory.intel)
        self.assertEqual(c.memory.intel[(1, 9, 9)]["loot"], 5000)
        self.assertEqual(c.memory.intel[(1, 9, 9)]["owner_id"], 77)
        self.assertEqual(self.events(c, "intel_used")[0]["at"], "1:9:9")
        self.step(130.0, "agent0001")
        self.assertNotIn((1, 9, 9), a.memory.intel)                              # a peaceful one just reads
        # later the second report goes out (it is still fresh enough)
        b.memory.intel[(1, 9, 8)]["at"] = b.now + 130
        self.step(130.0, "agent0002")
        self.assertEqual(len([t for t in self.chat() if t.startswith("INTEL")]), 2)

    def test_allies_are_warned_of_an_attack_once(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0002")                                             # b learns who its allies are
        b.client.hostile = [mission(7, "attack", left=60, origin=(1, 4, 4))]
        b.client.hostile[0]["arrive_at"] = b.client.now + 60
        self.step(5.0, "agent0002")
        warnings = [t for t in self.chat() if t.startswith("WARN")]
        self.assertEqual(warnings, ["WARN AT=1:5:6 FROM=1:4:4 ETA=55"])
        self.assertEqual(self.events(b, "ally_warned")[0]["mission"], 7)
        self.step(130.0, "agent0002")
        self.assertEqual(len([t for t in self.chat() if t.startswith("WARN")]), 1)
        # the others learn who is out to raid; the leader remembers the attacker of a member
        self.step(130.0, "agent0001")
        self.assertIn("1:4:4", a.memory.alliance["attackers"])
        self.assertIn((1, 4, 4), a.memory.aggressors)

    def test_a_spy_probe_is_no_reason_to_warn(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0002")
        b.client.hostile = [mission(7, "espionage", left=60, origin=(1, 4, 4))]
        self.step(5.0, "agent0002")
        self.assertEqual([t for t in self.chat() if t.startswith("WARN")], [])

    # -- leaving --
    def test_a_member_robbed_by_an_ally_leaves(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0002")
        b.client.hostile = [mission(9, "attack", left=60, origin=(1, 6, 6))]      # from c's home
        b.client.hostile[0]["arrive_at"] = b.client.now + 60
        self.step(130.0, "agent0002")
        self.assertEqual(self.events(b, "alliance_left")[0]["reason"], "robbed by an ally")
        self.assertNotIn(2, self.world.user_alliance)
        # no way back to the same alliance for a while
        b.client.hostile = []
        self.step(500.0, "agent0002")
        self.assertEqual(self.events(b, "alliance_applied"), [])

    def test_a_leader_robbed_by_a_member_throws_it_out(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0001")
        a.client.hostile = [mission(9, "attack", left=60, origin=(1, 5, 6))]      # from b's home
        a.client.hostile[0]["arrive_at"] = a.client.now + 60
        self.step(130.0, "agent0001")
        self.assertEqual(self.world.kicked, [2])
        self.assertEqual(self.events(a, "alliance_kicked")[0]["member"], 2)
        self.assertEqual(self.events(a, "alliance_left"), [])                     # a founder never leaves

    def test_a_member_leaves_a_silent_alliance_but_not_a_lively_one(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0002")
        st = b.memory.alliance
        st["joined_at"] = st["active_at"] = b.now - A.IDLE_LEAVE - 100
        self.step(130.0, "agent0002")
        self.assertIn("long time", self.events(b, "alliance_left")[0]["reason"])
        self.assertNotIn(2, self.world.user_alliance)

        self.join(1, b)
        self.step(200.0, "agent0002")
        st = b.memory.alliance
        st["joined_at"] = st["active_at"] = b.now - A.IDLE_LEAVE - 100
        self.world.say(1, "good morning", b.client.now)                           # somebody speaks
        self.step(130.0, "agent0002")
        self.assertEqual(len(self.events(b, "alliance_left")), 1)

    def test_kicked_out_is_noticed(self):
        a, b, c = self.alliance_of_three()
        self.step(130.0, "agent0002")
        self.world.alliances[1]["members"].remove(2)
        del self.world.user_alliance[2]
        self.step(130.0, "agent0002")
        self.assertEqual(self.events(b, "alliance_left")[0]["reason"], "kicked or disbanded")
        self.assertEqual(b.memory.alliance["allies"], [])
        self.assertIsNone(b._allowed_target_ids() if b.attack_humans else None)

    # -- the board --
    def test_the_leader_advertises_on_the_board_at_most_once_an_hour(self):
        self.board = Board()
        a, b = self.two_neighbours()
        self.found(a)
        for _ in range(4):
            self.step(130.0, "agent0001")
        ads = [p for p in self.board.posts if p[0] == "ALLIANCE"]
        self.assertEqual(len(ads), 1)
        self.assertIn("MINE01", ads[0][1])
        self.step(3700.0, "agent0001")
        self.assertEqual(len([p for p in self.board.posts if p[0] == "ALLIANCE"]), 2)

    def test_everything_works_without_a_board(self):
        a, b = self.two_neighbours()
        self.found(a)
        self.step(130.0, "agent0001")
        self.assertEqual(self.events(a, "alliance_skipped"), [])

    def test_a_server_error_never_costs_the_round(self):
        a = self.add("agent0001", 1, (1, 3, 8))
        real_get = a.client.get

        def broken(path):
            if path == "/alliance":
                from swarm.client import ApiError
                raise ApiError(500, "http_error", "boom")
            return real_get(path)

        a.client.get = broken
        a.tick()
        self.assertFalse(a.memory.alliance["off"])
        self.assertEqual(a.stats["refused"], 1)


class SummaryTests(unittest.TestCase):
    def test_summary_has_an_alliances_section(self):
        t = 1_700_000_000
        events = [
            {"t": t, "agent": "a1", "event": "alliance_founded", "alliance": "MINE01", "name": "Quiet Miners 1", "alliance_id": 1},
            {"t": t + 1, "agent": "a2", "event": "alliance_applied", "alliance": "MINE01", "alliance_id": 1},
            {"t": t + 2, "agent": "a1", "event": "alliance_accepted", "alliance": "MINE01", "applicant": 2},
            {"t": t + 3, "agent": "a3", "event": "alliance_applied", "alliance": "MINE01", "alliance_id": 1},
            {"t": t + 4, "agent": "a1", "event": "alliance_rejected", "alliance": "MINE01", "applicant": 3, "why": "attacked us"},
            {"t": t + 5, "agent": "a2", "event": "alliance_joined", "alliance": "MINE01", "alliance_id": 1},
            {"t": t + 6, "agent": "a2", "event": "intel_shared", "at": "1:9:9", "loot": 5000},
            {"t": t + 7, "agent": "a4", "event": "intel_used", "at": "1:9:9", "loot": 5000},
            {"t": t + 8, "agent": "a2", "event": "ally_warned", "mission": 7},
            {"t": t + 9, "agent": "a2", "event": "alliance_left", "alliance": "MINE01", "reason": "robbed by an ally"},
            {"t": t + 10, "agent": "a1", "event": "alliance_kicked", "alliance": "MINE01", "member": 5},
        ]
        text = "\n".join(sl.alliances_section(events))
        self.assertIn("ALLIANCES", text)
        self.assertIn("1 founded, 2 applications (1 accepted, 1 rejected), 1 joined, 1 left, 1 kicked out", text)
        self.assertIn("MINE01", text)
        self.assertIn("(+1 accepted since)", text)
        self.assertIn("1 spy reports shared (5,000 loot", text)
        self.assertIn("1 used by an ally", text)
        self.assertIn("1 warnings", text)
        self.assertIn("robbed by an ally", text)
        self.assertIn("attacked us", text)
        self.assertIn("ALLIANCES", sl.summarize(events))

    def test_empty_section_says_none(self):
        self.assertIn("none", "\n".join(sl.alliances_section([])))


if __name__ == "__main__":
    unittest.main()
