import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm import expedition as ex
from swarm.agent import Agent, TokenStore
from swarm.military import Memory
from swarm.personas import PRESETS, make_persona
from tests.fakegame import FakeGame

CARGO = {"small_cargo": 5000, "large_cargo": 25000, "pathfinder": 10000, "light_fighter": 50}
DISC = PRESETS["scientist"]


class PureTests(unittest.TestCase):
    def test_find_ceiling_follows_the_best_players_points(self):
        self.assertEqual(ex.find_ceiling(0), 40_000)
        self.assertEqual(ex.find_ceiling(50_000), 500_000)
        self.assertEqual(ex.find_ceiling(10 ** 9), 5_000_000)

    def test_target_capacity_is_capped_and_scaled(self):
        self.assertEqual(ex.target_capacity(0, False), 20_000)
        self.assertEqual(ex.target_capacity(0, True), 30_000)
        self.assertEqual(ex.target_capacity(0, False, 0.5), 10_000)
        self.assertEqual(ex.target_capacity(10 ** 9, True), ex.MAX_CAPACITY_TARGET)

    def test_fleet_is_cargo_first_and_limited_to_a_share_of_what_is_home(self):
        fleet = ex.choose_fleet({"small_cargo": 10, "light_fighter": 10}, CARGO, 20_000, discoverer=False)
        self.assertEqual(fleet["small_cargo"], 4)          # 40% of 10
        self.assertEqual(fleet["light_fighter"], 2)        # escort: half as many as cargo ships, within 30% of 10 = 3
        fleet = ex.choose_fleet({"large_cargo": 100, "small_cargo": 100}, CARGO, 30_000, discoverer=True)
        self.assertEqual(fleet, {"large_cargo": 2})

    def test_discoverer_prefers_pathfinders_but_others_do_not_use_them(self):
        ships = {"pathfinder": 10, "small_cargo": 10}
        self.assertEqual(ex.choose_fleet(ships, CARGO, 30_000, True), {"pathfinder": 3})
        self.assertEqual(ex.choose_fleet(ships, CARGO, 20_000, False), {"small_cargo": 4})

    def test_too_small_a_fleet_stays_home(self):
        self.assertEqual(ex.choose_fleet({"light_fighter": 50}, CARGO, 20_000, True), {})
        self.assertEqual(ex.choose_fleet({"small_cargo": 1}, CARGO, 20_000, False), {})   # 40% of one ship rounds to none
        self.assertEqual(ex.choose_fleet({"small_cargo": 1}, CARGO, 20_000, True), {"small_cargo": 1})

    def consider(self, persona=DISC, **kw):
        args = dict(now=1000.0, state=ex.new_state(), astro=1, fleet_slots={"used": 0, "max": 3, "expedition_used": 0, "expedition_max": 1},
                    on_guard=False, alert=False)
        args.update(kw)
        return ex.can_consider(persona, **args)

    def test_gates(self):
        self.assertIsNone(self.consider())
        self.assertIn("astrophysics", self.consider(astro=0))
        self.assertIn("astrophysics", self.consider(PRESETS["balanced"], astro=2))
        self.assertIsNone(self.consider(PRESETS["balanced"], astro=3))
        self.assertIn("expedition slot", self.consider(fleet_slots={"used": 0, "max": 3, "expedition_used": 1, "expedition_max": 1}))
        self.assertIn("fleet slot", self.consider(fleet_slots={"used": 3, "max": 3, "expedition_used": 0, "expedition_max": 1}))
        self.assertIn("no expedition slots", self.consider(fleet_slots={"used": 0, "max": 3}))
        self.assertIsNone(self.consider(on_guard=True))      # a peaceful agent does not stay home because of a warning
        self.assertIn("raiders", self.consider(PRESETS["raider"], astro=3, on_guard=True))
        self.assertIn("raiders", self.consider(alert=True))
        self.assertIn("cooling", self.consider(state={**ex.new_state(), "next_at": 2000.0}))
        self.assertIn("last slot", self.consider(PRESETS["raider"], astro=3, fleet_slots={"used": 2, "max": 3, "expedition_used": 0, "expedition_max": 1}))
        self.assertIn("not interested", self.consider(replace(PRESETS["raider"], expedition_interest=0.0)))

    def test_only_curious_personas_roll_the_dice(self):
        ships = {"small_cargo": 10}
        sent = sum(bool(ex.decide(PRESETS["balanced"], random.Random(i), ships_here=ships, cargo_of=CARGO, top_score=0,
                                  state=ex.new_state()).ships) for i in range(400))
        self.assertTrue(40 < sent < 160)    # interest 0.25
        d = ex.decide(DISC, random.Random(1), ships_here=ships, cargo_of=CARGO, top_score=0, state=ex.new_state())
        self.assertTrue(d.ships)
        self.assertEqual(d.holding_hours, 1)

    def test_discoverers_only_by_class_the_others_are_rare(self):
        rates = {n: PRESETS[n].expedition_interest for n in PRESETS}
        self.assertEqual(rates["scientist"], 1.0)
        self.assertTrue(all(v <= 0.25 for n, v in rates.items() if n != "scientist"))
        for i in range(20):
            p = make_persona(i, random.Random(i))
            self.assertTrue(0.0 <= p.expedition_interest <= 1.0)


class ParseTests(unittest.TestCase):
    def test_resources(self):
        r = ex.parse_message("expedition_gain_resources", "Text.\n\nCrystal 1,234,567 have been captured.")
        self.assertEqual((r.outcome, r.crystal, r.metal), ("resources", 1234567, 0))

    def test_ships_and_dark_matter(self):
        r = ex.parse_message("expedition_gain_ships", "Found.\n\nThe following ships are now part of the fleet:\nSmall Cargo: 3\nBattleship: 1,200\nEspionage Probe: 2")
        self.assertEqual(r.ships, {"small_cargo": 3, "battle_ship": 1200, "espionage_probe": 2})
        r = ex.parse_message("expedition_gain_dark_matter", "Text\n\n(1,500 Dark Matter)")
        self.assertEqual((r.outcome, r.dark_matter), ("dark_matter", 1500))

    def test_the_rest_is_named_by_key(self):
        for key, name in (("expedition_failed", "nothing"), ("expedition_loss_of_fleet", "loss"), ("expedition_battle_pirates", "pirates"),
                          ("expedition_failed_and_delay", "delay"), ("expedition_merchant_found", "merchant"), ("something_else", "other")):
            self.assertEqual(ex.parse_message(key, "").outcome, name)

    def test_garbage_does_not_raise(self):
        self.assertEqual(ex.parse_message("expedition_gain_resources", None).metal, 0)
        self.assertEqual(ex.parse_message(None, None).outcome, "other")


class MemoryTests(unittest.TestCase):
    def test_results_are_counted_and_a_loss_is_charged_to_the_oldest_launch(self):
        st = ex.new_state()
        ex.record_sent(st, 100.0, {"small_cargo": 4}, 4000, 900)
        ex.record_sent(st, 200.0, {"large_cargo": 1}, 3000, 900)
        ex.record_result(st, 300.0, ex.Result("resources", metal=500))
        f = ex.record_result(st, 400.0, ex.Result("loss"))
        self.assertEqual(f["lost"], {"large_cargo": 1})
        self.assertEqual((st["sent"], st["metal"], st["lost_value"], st["ships_lost"]), (2, 500, 3000, {"large_cargo": 1}))
        self.assertEqual(st["outcomes"], {"resources": 1, "loss": 1})
        self.assertLess(st["scale"], 1.0)
        self.assertGreaterEqual(st["next_at"], 400.0 + ex.LOSS_PAUSE)

    def test_a_result_without_a_launch_is_still_counted(self):
        st = ex.new_state()
        ex.record_result(st, 5.0, ex.Result("ships", ships={"small_cargo": 2}))
        self.assertEqual(st["ships_found"], {"small_cargo": 2})

    def test_memory_round_trips_and_old_files_still_load(self):
        m = Memory()
        ex.record_sent(m.expedition, 10.0, {"small_cargo": 1}, 1000, 900)
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.expedition, m.expedition)
        old = Memory.from_dict({"risk_scale": 0.8})                 # file from before expeditions
        self.assertEqual(old.expedition, ex.new_state())
        broken = Memory.from_dict({"expedition": {"sent": "x", "scale": 99, "inflight": "no", "outcomes": []}})
        self.assertEqual(broken.expedition["sent"], 0)
        self.assertEqual(broken.expedition["scale"], ex.SCALE_MAX)
        self.assertEqual(broken.expedition["inflight"], [])


class AgentTests(unittest.TestCase):
    def make(self, persona=DISC, outcomes=("resources",), astro=1, ships=None, discoverer=True):
        game = FakeGame()
        game.expedition_stuff = True
        game.is_discoverer = discoverer
        game.slots_max = 3
        game.tech["astrophysics"] = astro
        game.ships.update(ships or {"small_cargo": 10})
        game.exp_outcomes = list(outcomes)
        game.levels["research_lab"] = 1
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

    def names(self, agent):
        return [e for e, _ in agent.events]

    def test_a_discoverer_sends_reads_the_report_and_remembers(self):
        game, agent = self.make()
        self.run_for(game, agent, 2)
        self.assertIn("expedition_sent", self.names(agent))
        sent = [b for p, b in game.post_log if p.endswith("/fleet")][0]
        self.assertEqual((sent["mission"], sent["position"], sent["holding_hours"]), ("expedition", 16, 1))
        self.assertEqual((sent["galaxy"], sent["system"]), (1, 3))
        self.assertIn("small_cargo", sent["ships"])
        results = [k for e, k in agent.events if e == "expedition_result"]
        self.assertEqual(results[0]["outcome"], "resources")
        self.assertEqual(results[0]["metal"], 12345)
        st = agent.memory.expedition
        self.assertGreaterEqual(st["sent"], 1)
        self.assertEqual(st["metal"], 12345 * len(results))
        self.assertEqual(agent.stats["errors"], 0)

    def test_all_outcomes_are_read(self):
        game, agent = self.make(outcomes=("ships", "dark_matter", "nothing", "delay", "loss"), ships={"small_cargo": 40})
        self.run_for(game, agent, 40)
        outs = agent.memory.expedition["outcomes"]
        self.assertIn("ships", outs)
        self.assertIn("dark_matter", outs)
        self.assertEqual(agent.memory.expedition["ships_found"]["small_cargo"] % 3, 0)
        self.assertEqual(agent.stats["errors"], 0)

    def test_a_lost_fleet_is_counted_and_followed_by_a_pause(self):
        game, agent = self.make(outcomes=("loss",))
        self.run_for(game, agent, 2)
        st = agent.memory.expedition
        self.assertEqual(st["outcomes"].get("loss"), 1)
        self.assertGreater(st["lost_value"], 0)
        self.assertEqual(st["sent"], 1)       # no second launch inside the pause
        self.assertLess(st["scale"], 1.0)

    def test_no_astrophysics_no_expedition(self):
        game, agent = self.make(astro=0)
        self.run_for(game, agent, 2)
        self.assertNotIn("expedition_sent", self.names(agent))
        self.assertEqual([b for p, b in game.post_log if p.endswith("/fleet")], [])

    def test_a_raided_peaceful_agent_still_goes_but_an_aggressive_one_stays(self):
        game, agent = self.make()
        agent.memory.aggressors[(1, 1, 5)] = 0.0     # robbed a moment ago
        agent.tick()
        self.assertIn("expedition_sent", self.names(agent))     # a scientist does not hide for 3 hours
        game2, agent2 = self.make()
        agent2.persona = replace(agent2.persona, aggression=0.5)
        agent2.memory.aggressors[(1, 1, 5)] = 0.0
        agent2.tick()
        self.assertNotIn("expedition_sent", self.names(agent2))

    def test_incoming_hostile_fleet_keeps_it_home(self):
        game, agent = self.make()
        game.hostile = [{"id": 5, "mission": "attack", "is_mine": False, "is_return": False, "from": {"coordinates": {"galaxy": 1, "system": 1, "position": 5}},
                         "arrive_at": 500.0}]
        agent.alert_until = 10 ** 9
        agent.tick()
        self.assertNotIn("expedition_sent", self.names(agent))

    def test_a_refusal_is_logged_and_backs_off(self):
        game, agent = self.make()
        orig = game.post
        def refuse(path, body=None):
            if path.endswith("/fleet"):
                from swarm.client import ApiError
                raise ApiError(422, "mission_rejected", "not enough deuterium")
            return orig(path, body)
        game.post = refuse
        agent.tick()
        self.assertIn("expedition_refused", self.names(agent))
        self.assertGreater(agent.memory.expedition["next_at"], agent.now)

    def test_a_non_discoverer_without_interest_never_goes(self):
        game, agent = self.make(persona=replace(PRESETS["balanced"], expedition_interest=0.0), astro=4, discoverer=False)
        self.run_for(game, agent, 3)
        self.assertNotIn("expedition_sent", self.names(agent))

    def test_a_curious_non_discoverer_goes_rarely(self):
        game, agent = self.make(persona=replace(PRESETS["balanced"], expedition_interest=1.0), astro=4, discoverer=False)
        self.run_for(game, agent, 3)
        self.assertEqual(agent.memory.expedition["sent"], 1)    # 6 h pause after the first launch


if __name__ == "__main__":
    unittest.main()
