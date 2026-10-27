import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import evolve as E, genome as G, targetlearn
from swarm.agent import Agent
from swarm.military import Memory
from swarm.personas import PRESETS

EPOCH = 12 * 3600.0


def agent_names(control: bool, n: int) -> list[str]:
    out, i = [], 0
    while len(out) < n:
        i += 1
        name = f"agent{i:04d}"
        if targetlearn.is_control(name) == control:
            out.append(name)
    return out


class Select(unittest.TestCase):
    def records(self, n=12):
        return [{"agent": f"a{i:02d}", "growth": i / 10.0, "genome": {**G.neutral(), "metal_mine": 1.0 + i / 20.0}, "control": False} for i in range(n)]

    def test_bottom_quarter_copies_a_top_quarter_genome(self):
        recs = self.records(12)           # quarter = 3: a00..a02 are the weakest, a09..a11 the best
        r = E.select(recs, "a00", random.Random(1))
        self.assertEqual(r["action"], "replaced")
        self.assertIn(r["parent"], {"a09", "a10", "a11"})
        self.assertEqual(r["genome"], G.clamp(r["genome"]))
        self.assertNotEqual(r["genome"], G.neutral())

    def test_the_middle_and_the_best_are_kept(self):
        for name in ("a03", "a06", "a11"):
            self.assertEqual(E.select(self.records(12), name, random.Random(1))["action"], "kept")

    def test_too_small_a_population_changes_nothing(self):
        self.assertEqual(E.select(self.records(E.MIN_POP - 1), "a00", random.Random(1))["action"], "none")

    def test_controls_are_ignored_and_never_replaced(self):
        recs = self.records(12) + [{"agent": "c1", "growth": -1.0, "genome": G.neutral(), "control": True}]
        self.assertEqual(E.select(recs, "c1", random.Random(1))["action"], "none")
        self.assertEqual(E.select(recs, "a00", random.Random(1))["pop"], 12)

    def test_unknown_agent_changes_nothing(self):
        self.assertEqual(E.select(self.records(12), "ghost", random.Random(1))["action"], "none")


class Windows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"OGAMEX_EVOLVE": "on", "OGAMEX_EVOLVE_HOURS": "12", "OGAMEX_GENOMES": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def run_agent(self, name, growths, control=False, start_window=10):
        """Feed one agent a metric that grows by the given factor per window; returns the events step() produced."""
        st = E.new_state()
        st["genome"] = G.neutral()
        events = []
        value = 100.0
        for i, g in enumerate(growths):
            now = (start_window + i) * EPOCH + 60
            r = E.step(st, name, "miner", self.tmp, now, value, control)
            if r:
                events.append(r)
            value *= (1 + g)
        r = E.step(st, name, "miner", self.tmp, (start_window + len(growths)) * EPOCH + 60, value, control)
        if r:
            events.append(r)
        return st, events

    def test_first_call_only_starts_the_window(self):
        st = E.new_state()
        self.assertIsNone(E.step(st, "a", "miner", self.tmp, 10 * EPOCH + 60, 100.0, False))
        self.assertEqual(st["cur"]["k"], 10)

    def test_a_record_is_filed_at_the_window_change(self):
        st, events = self.run_agent("agent9", [0.5, 0.5])
        recs = E.read_records(self.tmp, "miner", 10)
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(recs[0]["growth"], 0.5, places=3)
        self.assertEqual(events[0]["action"], "recorded")

    def test_an_agent_that_joined_late_in_the_window_files_nothing(self):
        st = E.new_state()
        E.step(st, "a", "miner", self.tmp, 10 * EPOCH + 0.7 * EPOCH, 100.0, False)
        E.step(st, "a", "miner", self.tmp, 11 * EPOCH + 60, 150.0, False)
        self.assertEqual(E.read_records(self.tmp, "miner", 10), [])

    def test_a_skipped_window_is_not_a_record(self):
        st = E.new_state()
        E.step(st, "a", "miner", self.tmp, 10 * EPOCH + 60, 100.0, False)
        E.step(st, "a", "miner", self.tmp, 13 * EPOCH + 60, 900.0, False)       # offline for three windows
        self.assertEqual(E.read_records(self.tmp, "miner", 10), [])

    def test_the_weak_adopt_better_genomes_and_controls_never_change(self):
        learners = agent_names(False, 12)
        controls = agent_names(True, 3)
        states = {}
        for window_growth in range(3):             # three windows of records
            for i, name in enumerate(learners + controls):
                st = states.setdefault(name, E.new_state())
                st["genome"] = st["genome"] or ({**G.neutral(), "metal_mine": 1.0 + i / 20.0} if name in learners else G.neutral())
                value = 100.0 * (1 + (i / 10.0)) ** window_growth     # rank follows the index: later names grow faster
                E.step(st, name, "miner", self.tmp, (10 + window_growth) * EPOCH + 60, 100.0 * (1 + i / 10.0) ** window_growth, name in controls)
        for name in learners + controls:      # the window change that closes window 12 and decides on window 11
            E.step(states[name], name, "miner", self.tmp, 13 * EPOCH + 60, 100.0 * (1 + int((learners + controls).index(name)) / 10.0) ** 3, name in controls)
        replaced = [n for n in learners if states[n]["gen"] > 0]
        self.assertTrue(replaced)
        self.assertLessEqual(len(replaced), len(learners) // 4 + 1)
        for n in controls:
            self.assertEqual(states[n]["genome"], G.neutral())
            self.assertEqual(states[n]["gen"], 0)

    def test_damaged_record_files_are_skipped(self):
        d = self.tmp / "miner" / "5"
        d.mkdir(parents=True)
        (d / "bad.json").write_text("{not json")
        (d / "worse.json").write_text(json.dumps({"agent": "x"}))
        self.assertEqual(E.read_records(self.tmp, "miner", 5), [])


class Start(unittest.TestCase):
    def test_controls_start_neutral_and_learners_get_noise(self):
        with mock.patch.dict(os.environ, {"OGAMEX_EVOLVE": "on", "OGAMEX_GENOMES": ""}):
            c = agent_names(True, 1)[0]
            a, b = agent_names(False, 2)
            self.assertEqual(E.initial_genome(c, "miner"), G.neutral())
            ga, gb = E.initial_genome(a, "miner"), E.initial_genome(b, "miner")
            self.assertNotEqual(ga, gb)
            self.assertEqual(ga, E.initial_genome(a, "miner"))        # stable per agent
            self.assertLess(G.distance(ga, G.neutral()), 0.12)

    def test_trained_genome_is_the_starting_point(self):
        d = tempfile.mkdtemp()
        trained = {**G.neutral(), "metal_mine": 2.0}
        (Path(d) / "miner.json").write_text(json.dumps({"persona": "miner", "genome": trained}))
        with mock.patch.dict(os.environ, {"OGAMEX_GENOMES": d}):
            name = agent_names(False, 1)[0]
            g = E.initial_genome(name, "miner")
            self.assertGreater(g["metal_mine"], 1.5)
            self.assertEqual(E.initial_genome(name, "scientist"), E.initial_genome(name, "scientist"))
            self.assertLess(E.initial_genome(name, "scientist")["metal_mine"], 1.3)       # no file for this personality: only noise

    def test_damaged_state_is_cleaned(self):
        self.assertEqual(E.clean_state("junk"), E.new_state())
        st = E.clean_state({"genome": {"metal_mine": 99}, "gen": "x"})
        self.assertEqual(st, E.new_state())
        st = E.clean_state({"genome": {"metal_mine": 99}, "gen": 2, "cur": {"k": 3, "t": 1, "start": 5}})
        self.assertEqual(st["genome"]["metal_mine"], G.MULT_RANGE[1])
        self.assertEqual(st["cur"]["k"], 3)

    def test_memory_roundtrip(self):
        m = Memory()
        m.evo["genome"] = {**G.neutral(), "solar_plant": 1.4}
        m.evo["gen"] = 2
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.evo["genome"]["solar_plant"], 1.4)
        self.assertEqual(back.evo["gen"], 2)


class AgentHook(unittest.TestCase):
    def stub(self, name, persona_name="miner"):
        s = mock.Mock(spec=Agent)
        s.name = name
        s.base_persona = PRESETS[persona_name]
        s.persona = PRESETS[persona_name]
        s.memory = Memory()
        s.now = 10 * EPOCH + 60
        s._evo_dir = Path(tempfile.mkdtemp())
        s.logged = []
        s.log = lambda event, **kw: s.logged.append((event, kw))
        return s

    def test_nothing_changes_when_evolution_is_off(self):
        with mock.patch.dict(os.environ, {"OGAMEX_EVOLVE": "off", "OGAMEX_GENOMES": ""}):
            s = self.stub(agent_names(False, 1)[0])
            Agent._init_genome(s)
            self.assertIs(s.persona, s.base_persona)
            self.assertIsNone(s.memory.evo["genome"])

    def test_a_learner_gets_a_genome_a_control_does_not(self):
        with mock.patch.dict(os.environ, {"OGAMEX_EVOLVE": "on", "OGAMEX_GENOMES": ""}):
            s = self.stub(agent_names(False, 1)[0])
            Agent._init_genome(s)
            self.assertIsNotNone(s.memory.evo["genome"])
            self.assertNotEqual(s.persona.building_weights, s.base_persona.building_weights)
            self.assertEqual(s.persona.aggression, s.base_persona.aggression)
            c = self.stub(agent_names(True, 1)[0])
            Agent._init_genome(c)
            self.assertIs(c.persona, c.base_persona)

    def test_evolve_step_logs_the_window_and_applies_a_new_genome(self):
        with mock.patch.dict(os.environ, {"OGAMEX_EVOLVE": "on", "OGAMEX_EVOLVE_HOURS": "12", "OGAMEX_GENOMES": ""}):
            s = self.stub(agent_names(False, 1)[0])
            Agent._init_genome(s)
            status = {"research": {"levels": {}}}
            planets = [{"production_per_hour": {"metal": 100, "crystal": 0, "deuterium": 0}}]
            Agent._evolve_step(s, status, planets)
            s.now += EPOCH
            planets = [{"production_per_hour": {"metal": 200, "crystal": 0, "deuterium": 0}}]
            Agent._evolve_step(s, status, planets)
            ev = [kw for e, kw in s.logged if e == "evolve"]
            self.assertEqual(len(ev), 1)
            self.assertAlmostEqual(ev[0]["growth"], 1.0, places=2)
            self.assertEqual(ev[0]["group"], "evolving")


if __name__ == "__main__":
    unittest.main()
