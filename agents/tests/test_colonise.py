import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm import colonise
from swarm.agent import Agent, TokenStore
from swarm.personas import PRESETS, make_persona
from swarm.military import Memory
from tests.fakegame import FakeGame


def home(left):
    return {"fields": {"used": 163 - left, "max": 163}}


def view(system, taken=()):
    return {"galaxy": 1, "system": system,
            "positions": [{"position": p, "planet": ({"id": 1} if p in taken else None)} for p in range(1, 16)]}


class RuleTests(unittest.TestCase):
    def test_astrophysics_needed_grows_with_planets(self):
        self.assertEqual([colonise.astro_needed(n) for n in (1, 2, 3, 4)], [1, 3, 5, 7])

    def test_nobody_wants_a_colony_with_plenty_of_fields(self):
        p = replace(PRESETS["scientist"], expansion=1.0)
        self.assertFalse(colonise.wants_colony(p, home(120), 1, random.Random(1)))
        self.assertTrue(colonise.wants_colony(p, home(40), 1, random.Random(1)))

    def test_a_persona_without_appetite_or_at_its_planet_limit_never_expands(self):
        rng = random.Random(1)
        self.assertFalse(colonise.wants_colony(replace(PRESETS["miner"], expansion=0.0), home(0), 1, rng))
        self.assertFalse(colonise.wants_colony(replace(PRESETS["miner"], expansion=0.25), home(0), 2, rng))

    def test_goals_list_only_what_is_missing(self):
        buildings, research = colonise.goals(1, {"impulse_drive": 3, "espionage_technology": 1})
        self.assertEqual(research, {"espionage_technology": 4, "astrophysics": 1})
        self.assertEqual(buildings["shipyard"], colonise.SHIPYARD_NEEDED)

    def test_the_tables_cover_every_position(self):
        for pos in range(1, 16):
            self.assertIn(pos, colonise.FIELD_RANGE)
            self.assertIn(pos, colonise.TEMPERATURE)


class TargetTests(unittest.TestCase):
    P = PRESETS["balanced"]

    def test_picks_a_free_position_and_never_a_taken_one(self):
        for seed in range(30):
            pick = colonise.pick_target(self.P, random.Random(seed), (1, 100, 8),
                                        [view(100, taken=range(1, 12)), view(101, taken=(8,))], astro=2)
            (g, s, pos), why = pick
            self.assertIn(pos, (12, 13, 14, 15, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11) if s == 101 else (12, 13, 14, 15))
            if s == 101:
                self.assertNotEqual(pos, 8)

    def test_far_positions_need_astrophysics(self):
        taken = range(4, 13)   # leaves 1, 2, 3, 13, 14, 15
        self.assertIsNone(colonise.pick_target(self.P, random.Random(1), (1, 100, 8), [view(100, taken=taken)], astro=1))
        pick = colonise.pick_target(self.P, random.Random(1), (1, 100, 8), [view(100, taken=taken)], astro=8)
        self.assertIsNotNone(pick)

    def test_prefers_a_good_position_over_a_poor_one(self):
        picks = [colonise.pick_target(self.P, random.Random(s), (1, 100, 8), [view(100, taken=set(range(1, 16)) - {8, 15})], astro=8)
                 for s in range(20)]
        self.assertTrue(all(p[0][2] == 8 for p in picks))

    def test_scan_stays_inside_the_universe(self):
        systems = colonise.systems_to_scan((1, 2, 8))
        self.assertEqual(min(s for _, s in systems), 1)
        self.assertEqual(len(colonise.systems_to_scan((1, 250, 8))), 2 * colonise.SEARCH_SYSTEMS + 1)


class MemoryTests(unittest.TestCase):
    def test_colony_state_survives_a_restart(self):
        m = Memory()
        m.expanding, m.colony_sent_at, m.colony_planets_before, m.colony_until = True, 500.0, 2, 900.0
        m2 = Memory.from_dict(m.to_dict())
        self.assertEqual((m2.expanding, m2.colony_sent_at, m2.colony_planets_before, m2.colony_until), (True, 500.0, 2, 900.0))
        self.assertFalse(Memory.from_dict({"expanding": "garbage-ok"}).colony_sent_at)

    def test_existing_agents_keep_their_personalities(self):
        a = make_persona(0, random.Random(5))
        b = make_persona(0, random.Random(5))
        self.assertEqual(a, b)
        self.assertEqual(replace(a, expansion=0.0).aggression, a.aggression)


class AgentInTheFakeGameTests(unittest.TestCase):
    def make(self, persona):
        game = FakeGame()
        game.colony_stuff = True
        game.field_max = 30                  # crowded from the start: a colony is wanted at once
        game.slots_max = 2
        game.levels.update({"research_lab": 3, "shipyard": 4})
        game.tech.update({"impulse_drive": 3, "espionage_technology": 4})
        game.res.update({"metal": 3_000_000, "crystal": 3_000_000, "deuterium": 3_000_000})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(3), client=game)
        agent.events = []
        agent.log = lambda event, **k: agent.events.append((event, k))
        agent.user_id = 1
        return game, agent

    def run_for(self, game, agent, hours):
        elapsed = 0.0
        while elapsed < hours * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay

    def test_an_expansive_agent_researches_builds_a_ship_and_settles(self):
        game, agent = self.make(replace(PRESETS["scientist"], expansion=1.0))
        self.run_for(game, agent, 12)
        names = [e for e, _ in agent.events]
        self.assertIn("expand", names)
        self.assertIn("colony_sent", names)
        self.assertEqual(game.tech["astrophysics"], 1)
        self.assertEqual(len(game.colonies), 1)
        g, s, p = game.colonies[0]
        self.assertNotEqual((g, s, p), (1, 3, 8))
        sent = [b for path, b in game.post_log if path.endswith("/fleet")]
        self.assertEqual(sent[0]["mission"], "colonisation")
        self.assertEqual(sent[0]["ships"], {"colony_ship": 1})
        self.assertEqual(agent.stats["errors"], 0)

    def test_a_persona_without_appetite_never_builds_a_colony_ship(self):
        game, agent = self.make(replace(PRESETS["scientist"], expansion=0.0))
        self.run_for(game, agent, 6)
        self.assertEqual(game.colonies, [])
        self.assertNotIn("expand", [e for e, _ in agent.events])
        self.assertEqual(game.tech["astrophysics"], 0)

    def test_a_planet_with_plenty_of_fields_does_not_expand(self):
        game, agent = self.make(replace(PRESETS["scientist"], expansion=1.0))
        game.field_max = 163
        self.run_for(game, agent, 3)
        self.assertNotIn("expand", [e for e, _ in agent.events])

    def test_a_new_planet_ends_the_goal(self):
        game, agent = self.make(replace(PRESETS["scientist"], expansion=1.0))
        agent.memory.expanding, agent.memory.colony_sent_at, agent.memory.colony_planets_before = True, 10.0, 0
        agent.now, agent.planets_owned = 100.0, 1
        agent._follow_colony({"id": 10})
        self.assertFalse(agent.memory.expanding)
        self.assertEqual(agent.memory.colony_sent_at, 0.0)


if __name__ == "__main__":
    unittest.main()
