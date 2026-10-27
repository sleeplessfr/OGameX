import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import validate_econsim as V
from swarm import econsim as E
from swarm.agent import Agent

ROOT = Path(__file__).resolve().parent.parent


class Formulas(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(E.price("metal_mine", 1), {"metal": 60, "crystal": 15, "deuterium": 0})
        self.assertEqual(E.price("metal_mine", 3), {"metal": 135, "crystal": 33, "deuterium": 0})   # 60*1.5^2, 15*1.5^2 floored
        self.assertEqual(E.price("astrophysics", 3), {"metal": 12200, "crystal": 24500, "deuterium": 12200})   # rounded to 100
        self.assertEqual(E.price("metal_mine", 0), {"metal": 0, "crystal": 0, "deuterium": 0})

    def test_build_time_matches_the_php_formula(self):
        # (60+15)/(2500*max(4-0.5,1)*(1+0)*1*1) h = 0.00857 h = 30 s at speed 1
        self.assertEqual(E.build_seconds("metal_mine", 1, 0, 0, 1), 30)
        self.assertEqual(E.build_seconds("metal_mine", 1, 0, 0, 100), 1)
        # robot factory 2 and nano 1 cut the time to 1/(3*2)
        self.assertEqual(E.build_seconds("metal_mine", 5, 2, 1, 1), int((E.price("metal_mine", 5)["metal"] + E.price("metal_mine", 5)["crystal"]) / (2500 * 1.5 * 3 * 2) * 3600))
        a, b = E.build_seconds("nano_factory", 2, 5, 0, 1), E.build_seconds("nano_factory", 2, 5, 1, 1)
        self.assertGreater(a, b)

    def test_research_time(self):
        p = E.price("energy_technology", 1)
        self.assertEqual(E.research_seconds("energy_technology", 1, 1, 1), int((p["metal"] + p["crystal"]) / (1000 * 2) * 3600))
        self.assertEqual(E.research_seconds("energy_technology", 1, 1, 1, discoverer=True), int(int((p["metal"] + p["crystal"]) / 2000 * 3600) * 0.75))

    def test_storage_and_fields(self):
        self.assertEqual(E.storage_cap(0), 10000)
        self.assertEqual(E.fields_max(163, 3), 163 + 15 + 1)

    def test_production_basics(self):
        # speed 1, no mines: only the basic income, boosted by the position
        p = E.production({}, 8, 30, 1)
        self.assertAlmostEqual(p["metal"], 30 * 1.35)
        self.assertAlmostEqual(p["crystal"], 15)
        # a metal mine 10 with enough power at position 8
        lv = {"metal_mine": 10, "solar_plant": 10}
        raw = 30 * 10 * 1.1 ** 10
        p = E.production(lv, 8, 30, 1)
        self.assertAlmostEqual(p["metal"], 30 * 1.35 + raw + int(raw * 0.35), delta=1.0)

    def test_power_shortage_cuts_the_mines_not_the_basic_income(self):
        lv = {"metal_mine": 20}
        p = E.production(lv, 8, 30, 1)
        self.assertAlmostEqual(p["metal"], 30 * 1.35, places=6)

    def test_plasma_and_collector(self):
        lv = {"metal_mine": 10, "solar_plant": 20}
        base = E.production(lv, 5, 30, 1)["metal"]
        self.assertGreater(E.production(lv, 5, 30, 1, plasma=5)["metal"], base)
        self.assertGreater(E.production(lv, 5, 30, 1, collector=True)["metal"], base * 1.1)

    def test_fusion_burns_deuterium(self):
        lv = {"deuterium_synthesizer": 5, "solar_plant": 15, "fusion_plant": 3}
        self.assertLess(E.production(lv, 8, 30, 1)["deuterium"], E.production({**lv, "fusion_plant": 0}, 8, 30, 1)["deuterium"])


class DataMatchesTheGame(unittest.TestCase):
    def test_econdata_is_generated_from_the_php(self):
        if not (ROOT.parent / "app" / "GameObjects").is_dir():
            self.skipTest("game source not next to the agents folder")
        r = subprocess.run([sys.executable, str(ROOT / "gen_econdata.py"), "--check"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def snapshot_line(agent, t, levels, research, pos=8, temp=30.0, klass="general", speed=100.0, noise=1.0):
    prod = E.production(levels, pos, temp, speed, research.get("energy_technology", 0), research.get("plasma_technology", 0), klass == "collector")
    make, need = E.energy(levels, research.get("energy_technology", 0), klass == "collector")
    row = {"id": 5, "pos": pos, "fields": 160, "levels": levels, "prod": {k: int(v * noise) for k, v in prod.items()},
           "storage": E.storage(levels), "res": {}, "energy": int(make - need)}
    return json.dumps({"t": t, "agent": agent, "event": "econ_snapshot", "klass": klass, "research": research, "planets": [row]})


class Validator(unittest.TestCase):
    def logs(self, noise):
        d = tempfile.mkdtemp()
        lv = {"metal_mine": 18, "crystal_mine": 15, "deuterium_synthesizer": 12, "solar_plant": 22, "robot_factory": 5, "metal_store": 2}
        lines = [snapshot_line("agent0001", 1000 + i * 1800, lv, {"energy_technology": 5, "plasma_technology": 2}, noise=noise) for i in range(3)]
        lines.append(json.dumps({"t": 1100, "agent": "agent0001", "event": "build", "planet": 5, "what": "metal_mine", "level": 19,
                                 "seconds": E.build_seconds("metal_mine", 19, 5, 0, 100), "why": "x"}))
        lines.append(json.dumps({"t": 1200, "agent": "agent0001", "event": "research", "planet": 5, "what": "energy_technology", "level": 6,
                                 "seconds": E.research_seconds("energy_technology", 6, 0, 100), "why": "x"}))
        (Path(d) / "agent0001.jsonl").write_text("\n".join(lines) + "\n")
        return Path(d)

    def run_check(self, noise):
        events = V.read_events(self.logs(noise), None)
        return V.report(V.check_snapshots(events, 100.0), V.check_durations(events, 100.0))

    def test_a_faithful_log_passes(self):
        text, ok = self.run_check(1.0)
        self.assertTrue(ok, text)
        self.assertIn("the simulator matches", text)

    def test_a_log_from_a_different_game_fails(self):
        text, ok = self.run_check(1.6)
        self.assertFalse(ok, text)
        self.assertIn("NOT yet", text)


class Snapshot(unittest.TestCase):
    def stub(self, now, last=None):
        s = mock.Mock(spec=Agent)
        s.now = now
        s.ECON_SNAPSHOT_EVERY = Agent.ECON_SNAPSHOT_EVERY
        s.persona = mock.Mock(character_class="collector")
        s.logged = []
        s.log = lambda event, **kw: s.logged.append((event, kw))
        s.client = mock.Mock()
        s.client.get.return_value = {"buildings": [{"machine_name": "metal_mine", "level": 7}, {"machine_name": "shipyard", "level": 0}]}
        if last is not None:
            s._econ_last = last
        return s

    def test_logs_levels_production_and_research_every_half_hour(self):
        s = self.stub(10_000.0)
        planets = [{"id": 5, "type": "planet", "coordinates": {"position": 8}, "fields": {"max": 160}, "production_per_hour": {"metal": 9},
                    "storage": {"metal": 10000}, "resources": {"metal": 1}, "energy": 3},
                   {"id": 6, "type": "moon", "coordinates": {"position": 8}}]
        Agent._econ_snapshot(s, {"research": {"levels": {"energy_technology": 4}}}, planets)
        self.assertEqual(len(s.logged), 1)
        ev, kw = s.logged[0]
        self.assertEqual(ev, "econ_snapshot")
        self.assertEqual(kw["klass"], "collector")
        self.assertEqual(kw["planets"][0]["levels"], {"metal_mine": 7})     # zero levels are left out, moons are skipped
        self.assertEqual(len(kw["planets"]), 1)
        s._econ_last = s.now
        s.now += 600
        Agent._econ_snapshot(s, {"research": {"levels": {}}}, planets)
        self.assertEqual(len(s.logged), 1)


if __name__ == "__main__":
    unittest.main()
