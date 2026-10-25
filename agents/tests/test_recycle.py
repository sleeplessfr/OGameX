import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swarm import recycle
from swarm.agent import Agent, TokenStore
from swarm.military import Memory
from swarm.personas import PRESETS, make_persona
from tests.fakegame import FakeGame

ITEM = {"machine_name": "recycler", "requirements_met": True, "character_class_met": True, "max_buildable": 50,
        "cost_per_unit": {"metal": 10000, "crystal": 6000, "deuterium": 2000}}
RICH = {"metal": 10 ** 6, "crystal": 10 ** 6, "deuterium": 10 ** 6}


class RuleTests(unittest.TestCase):
    def test_recyclers_needed_rounds_up_and_is_capped(self):
        self.assertEqual(recycle.recyclers_needed(20000), 1)
        self.assertEqual(recycle.recyclers_needed(20001), 2)
        self.assertEqual(recycle.recyclers_needed(50000, cargo=25000), 2)
        self.assertEqual(recycle.recyclers_needed(0), 0)
        self.assertEqual(recycle.recyclers_needed(10 ** 9), recycle.MAX_RECYCLERS)

    def test_who_fights(self):
        self.assertTrue(recycle.fights(PRESETS["raider"]))
        self.assertTrue(recycle.fights(PRESETS["balanced"]))
        self.assertFalse(recycle.fights(PRESETS["miner"]))
        self.assertFalse(recycle.fights(PRESETS["turtle"]))
        self.assertFalse(recycle.fights(PRESETS["scientist"]))

    def test_fighters_buy_for_medium_debris_peaceful_only_for_big(self):
        n, _ = recycle.recyclers_to_buy(PRESETS["raider"], ITEM, RICH, 30000, 0)
        self.assertEqual(n, 2)
        n, why = recycle.recyclers_to_buy(PRESETS["miner"], ITEM, RICH, 30000, 0)
        self.assertEqual(n, 0)
        self.assertIn("too small", why)
        n, _ = recycle.recyclers_to_buy(PRESETS["miner"], ITEM, RICH, 90000, 1)
        self.assertEqual(n, 4)     # 5 needed, 1 owned

    def test_no_purchase_when_small_unaffordable_or_not_unlocked(self):
        self.assertEqual(recycle.recyclers_to_buy(PRESETS["raider"], ITEM, RICH, 8000, 0)[0], 0)
        self.assertEqual(recycle.recyclers_to_buy(PRESETS["raider"], {**ITEM, "requirements_met": False}, RICH, 90000, 0)[0], 0)
        self.assertEqual(recycle.recyclers_to_buy(PRESETS["raider"], None, RICH, 90000, 0)[0], 0)
        poor = {"metal": 15000, "crystal": 100000, "deuterium": 100000}      # 40% of 15000 = 6000 < one recycler
        self.assertEqual(recycle.recyclers_to_buy(PRESETS["raider"], ITEM, poor, 90000, 0)[0], 0)

    def test_purchase_never_spends_more_than_its_share(self):
        have = {"metal": 50000, "crystal": 50000, "deuterium": 50000}
        n, _ = recycle.recyclers_to_buy(PRESETS["raider"], ITEM, have, 200000, 0)
        self.assertEqual(n, 2)      # 40% of 50,000 metal = 20,000 = 2 recyclers

    def test_trip_worth_it(self):
        self.assertTrue(recycle.worth_trip(40000, 500, 10000)[0])
        self.assertFalse(recycle.worth_trip(40000, 500, 100)[0])            # no fuel
        ok, why = recycle.worth_trip(6000, 5000, 10 ** 6)                   # fuel costs more than it brings
        self.assertFalse(ok)
        self.assertIn("not worth", why)
        self.assertFalse(recycle.worth_trip(100, 0, 10)[0])

    def test_memory_adds_battles_replaces_with_spy_reports_and_expires(self):
        f: dict = {}
        recycle.remember(f, (1, 2, 3), 10000, 100.0, add=True)
        recycle.remember(f, (1, 2, 3), 5000, 200.0, add=True)
        self.assertEqual(f[(1, 2, 3)]["amount"], 15000)
        recycle.remember(f, (1, 2, 3), 9000, 300.0, add=False)
        self.assertEqual(f[(1, 2, 3)]["amount"], 9000)
        recycle.remember(f, (1, 2, 4), 7000, 300.0, add=True)
        recycle.remember(f, (1, 2, 3), 0, 400.0, add=False)                 # a spy report says the field is empty
        self.assertNotIn((1, 2, 3), f)
        recycle.remember(f, None, 5000, 1.0, add=True)
        recycle.remember(f, (1, None, 3), 5000, 1.0, add=True)
        self.assertEqual(list(f), [(1, 2, 4)])
        recycle.forget_old(f, 300.0 + recycle.DEBRIS_TTL + 1)
        self.assertEqual(f, {})

    def test_total_ignores_deuterium_and_junk(self):
        self.assertEqual(recycle.total_of({"metal": 100, "crystal": 50, "deuterium": 999}), 150)
        self.assertEqual(recycle.total_of(None), 0)
        self.assertEqual(recycle.total_of({"metal": "x"}), 0)

    def test_pick_prefers_big_and_near_and_skips_busy_ones(self):
        f: dict = {}
        recycle.remember(f, (1, 3, 5), 20000, 0.0, add=True)
        recycle.remember(f, (1, 200, 5), 30000, 0.0, add=True)
        recycle.remember(f, (2, 3, 5), 900000, 0.0, add=True)               # another galaxy: too far
        self.assertEqual(recycle.pick_field(f, 10.0, (1, 3, 8)), ((1, 3, 5), 20000))
        f[(1, 3, 5)]["sent_until"] = 500.0
        self.assertEqual(recycle.pick_field(f, 10.0, (1, 3, 8)), ((1, 200, 5), 30000))
        self.assertIsNone(recycle.pick_field(f, recycle.DEBRIS_TTL + 1, (1, 3, 8)))

    def test_after_sending_books_the_haul(self):
        f: dict = {}
        recycle.remember(f, (1, 3, 5), 50000, 0.0, add=True)
        recycle.after_sending(f, (1, 3, 5), 20000, 10.0, 120.0)
        self.assertEqual(f[(1, 3, 5)]["amount"], 30000)
        self.assertGreater(f[(1, 3, 5)]["sent_until"], 130.0)
        recycle.after_sending(f, (1, 3, 5), 28000, 200.0, 120.0)           # the rest is not worth a trip
        self.assertEqual(f, {})

    def test_fleet_for(self):
        self.assertEqual(recycle.fleet_for(50000, 10), 3)
        self.assertEqual(recycle.fleet_for(50000, 2), 2)
        self.assertEqual(recycle.fleet_for(50000, 0), 0)


class MemoryTests(unittest.TestCase):
    def test_debris_survives_a_save_and_old_files_still_load(self):
        m = Memory()
        recycle.remember(m.debris, (1, 3, 5), 12345, 50.0, add=True)
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.debris[(1, 3, 5)]["amount"], 12345)
        old = m.to_dict()
        del old["debris"]
        self.assertEqual(Memory.from_dict(old).debris, {})
        self.assertEqual(Memory.from_dict({"debris": {"bad": 1, "1:2:3": {"amount": 1}}}).debris, {(1, 2, 3): {"amount": 1}})
        recycle.forget_old(Memory.from_dict({"debris": {"1:2:3": "junk"}}).debris, 1.0)   # junk is tolerated

    def test_personas_are_unchanged_by_this_feature(self):
        self.assertEqual(make_persona(3, random.Random(5)), make_persona(3, random.Random(5)))


class AgentInTheFakeGameTests(unittest.TestCase):
    def make(self, persona, debris=60000):
        game = FakeGame()
        game.recycle_stuff = True
        game.slots_max = 3
        game.levels.update({"shipyard": 4, "metal_store": 6, "crystal_store": 6})
        game.res.update({"metal": 500_000, "crystal": 500_000, "deuterium": 5_000})
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(3), client=game)
        agent.events = []
        agent.log = lambda event, **k: agent.events.append((event, k))
        agent.user_id = 1
        if debris:
            game.debris[(1, 1, 5)] = debris
            recycle.remember(agent.memory.debris, (1, 1, 5), debris, 0.0, add=True)
        return game, agent

    def plan(self, game, agent):
        status = game.get("/status")
        agent.now = float(status["server_time"])
        home = status["planets"][0]
        return agent._plan_recycle(status, home, dict(home["resources"]))

    def names(self, agent):
        return [e for e, _ in agent.events]

    def test_a_raider_buys_recyclers_sends_them_and_keeps_the_haul(self):
        game, agent = self.make(PRESETS["raider"])
        self.plan(game, agent)
        self.assertEqual(game.ships["recycler"], 3)
        self.assertIn(("units", {"what": "recycler", "amount": 3, "why": "60,000 debris remembered"}), agent.events)
        metal_before = game.res["metal"]
        agent.now += 61
        game.advance(61)
        self.plan(game, agent)
        self.assertIn("recycle_sent", self.names(agent))
        sent = [b for p, b in game.post_log if p == "/planets/10/fleet"][0]
        self.assertEqual((sent["mission"], sent["type"], sent["ships"]), ("recycle", "debris_field", {"recycler": 3}))
        game.advance(300)
        self.assertEqual(game.recycled, 60000)
        self.assertEqual(game.ships["recycler"], 3)          # they came home
        self.assertGreater(game.res["metal"], metal_before)
        self.assertEqual(game.debris[(1, 1, 5)], 0)

    def test_no_second_fleet_goes_to_a_field_that_is_being_emptied(self):
        game, agent = self.make(PRESETS["raider"])
        game.ships["recycler"] = 3
        self.plan(game, agent)
        agent.now += 61
        self.plan(game, agent)
        self.assertEqual(self.names(agent).count("recycle_sent"), 1)
        self.assertNotIn((1, 1, 5), agent.memory.debris)    # all of it is on the way: nothing left to remember

    def test_a_miner_ignores_small_debris_but_buys_for_big_debris(self):
        game, agent = self.make(PRESETS["miner"], debris=30000)
        self.plan(game, agent)
        self.assertEqual(game.ships["recycler"], 0)
        game, agent = self.make(PRESETS["miner"], debris=100000)
        self.plan(game, agent)
        self.assertEqual(game.ships["recycler"], 5)

    def test_nothing_happens_without_debris_and_no_requests_are_made(self):
        game, agent = self.make(PRESETS["raider"], debris=0)
        self.plan(game, agent)
        self.assertEqual(game.post_log, [])

    def test_recyclers_not_unlocked_yet_means_no_order(self):
        game, agent = self.make(PRESETS["raider"])
        game.levels["shipyard"] = 3
        self.plan(game, agent)
        self.assertEqual(game.post_log, [])

    def test_too_expensive_fuel_is_skipped_and_not_retried_every_round(self):
        game, agent = self.make(PRESETS["raider"], debris=30000)
        game.ships["recycler"] = 2
        game.res["deuterium"] = 50_000
        game.recycle_fuel = 6_000          # 30,000 haul < 3 x 2 x 6,000
        self.plan(game, agent)
        self.assertEqual(self.names(agent), ["recycle_skipped"])
        self.assertEqual([p for p, _ in game.post_log if p == "/planets/10/fleet"], [])
        agent.now += 61
        n = len(game.post_log)
        self.plan(game, agent)
        self.assertEqual(len(game.post_log), n)

    def test_a_field_the_game_does_not_know_is_forgotten(self):
        game, agent = self.make(PRESETS["raider"])
        game.ships["recycler"] = 3
        game.debris.clear()
        self.plan(game, agent)
        self.assertEqual(agent.memory.debris, {})
        self.assertEqual(self.names(agent), ["recycle_skipped"])

    def test_no_free_fleet_slot_waits(self):
        game, agent = self.make(PRESETS["raider"])
        game.ships["recycler"] = 3
        game.slots_max = 0
        self.plan(game, agent)
        self.assertNotIn("recycle_sent", self.names(agent))

    def test_a_battle_report_creates_a_field_and_a_spy_report_corrects_it(self):
        game, agent = self.make(PRESETS["raider"], debris=0)
        game._add_message("combat_report", {
            "target": {"galaxy": 1, "system": 1, "position": 5, "type": 1, "owner_id": 2},
            "attacker": {"player_id": 1, "resource_loss": 80000, "units": {"light_fighter": 100}},
            "defender": {"player_id": 2, "resource_loss": 1000}, "loot": {}, "debris": {"metal": 30000, "crystal": 10000, "deuterium": 0},
            "rounds": 4})
        agent.now = 100.0
        agent._read_battles(100.0)
        self.assertEqual(agent.memory.debris[(1, 1, 5)]["amount"], 40000)
        game._add_message("espionage_report", {
            "target": {"galaxy": 1, "system": 1, "position": 5, "type": 1, "owner_id": 2},
            "resources": {}, "ships": {}, "defense": {}, "debris": {"metal": 5000, "crystal": 2000}, "research": {}})
        agent.memory.pending[(1, 1, 5)] = 100.0
        agent._read_intel(200.0)
        self.assertEqual(agent.memory.debris[(1, 1, 5)]["amount"], 7000)

    def test_a_full_tick_loop_with_a_raider_loses_nothing_to_errors(self):
        game, agent = self.make(PRESETS["raider"])
        elapsed = 0.0
        while elapsed < 3 * 3600:
            delay = agent.tick()
            game.advance(delay)
            elapsed += delay
        self.assertGreater(game.recycled, 0)
        self.assertEqual(agent.stats["errors"], 0)
        self.assertEqual(agent.stats["refused"], 0)


if __name__ == "__main__":
    unittest.main()
