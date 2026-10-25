import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import summarize_logs as sl
from swarm import logistics as L
from swarm.agent import Agent, TokenStore
from swarm.military import Memory
from swarm.personas import PRESETS
from tests.fakegame import FakeGame

# No military streak: the agent's own raiding and spying stay out of the way.
QUIET = replace(PRESETS["balanced"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, expansion=0.0, caution=0.0)
QUIET_MINER = replace(PRESETS["miner"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, expansion=0.0, caution=0.0)
STATS = {"small_cargo": {"cargo_capacity": 5000}, "large_cargo": {"cargo_capacity": 25000}, "light_fighter": {"cargo_capacity": 50}}


def planet(pid, res, storage=1_000_000, coord=(1, 3, 8)):
    return {"id": pid, "type": "planet", "resources": dict(res), "storage": {r: storage for r in res},
            "coordinates": {"galaxy": coord[0], "system": coord[1], "position": coord[2]}}


def hostile(id=1, to=10, origin=(1, 1, 5), left=40, kind="attack"):
    return {"id": id, "mission": kind, "is_mine": False, "is_return": False, "arrive_at": left,
            "from": {"galaxy": origin[0], "system": origin[1], "position": origin[2]},
            "to": {"planet_id": to, "galaxy": 1, "system": 3 if to == 10 else 4, "position": 8 if to == 10 else 6}}


class SupplyRuleTests(unittest.TestCase):
    RICH = {"metal": 100_000, "crystal": 50_000, "deuterium": 20_000}

    def test_route_goes_from_the_richest_to_the_starved_colony(self):
        home, poor, ok = planet(1, self.RICH), planet(2, {"metal": 100, "crystal": 0, "deuterium": 0}), planet(3, self.RICH)
        src, dst = L.pick_supply_route([ok, home, poor])
        self.assertIs(dst, poor)
        self.assertIn(src["id"], (1, 3))

    def test_no_route_when_planets_are_alike_poor_or_the_colony_is_full(self):
        self.assertIsNone(L.pick_supply_route([planet(1, self.RICH)]))
        self.assertIsNone(L.pick_supply_route([planet(1, self.RICH), planet(2, {"metal": 90_000, "crystal": 40_000, "deuterium": 10_000})]))
        self.assertIsNone(L.pick_supply_route([planet(1, {"metal": 1000, "crystal": 0, "deuterium": 0}), planet(2, {"metal": 0, "crystal": 0, "deuterium": 0})]))
        full = planet(2, {"metal": 100, "crystal": 0, "deuterium": 0}, storage=100)
        self.assertIsNone(L.pick_supply_route([planet(1, self.RICH), full]))

    def test_moons_are_not_supplied(self):
        moon = dict(planet(2, {"metal": 0, "crystal": 0, "deuterium": 0}), type="moon")
        self.assertIsNone(L.pick_supply_route([planet(1, self.RICH), moon]))

    def test_amounts_keep_a_reserve_and_respect_the_colony_storage(self):
        src, dst = planet(1, self.RICH), planet(2, {"metal": 0, "crystal": 0, "deuterium": 0}, storage=30_000)
        a = L.supply_amounts(src, dst)
        self.assertEqual(a, {"metal": 30_000, "crystal": 25_000, "deuterium": 10_000})
        for r, v in a.items():
            self.assertLessEqual(v, self.RICH[r] * (1 - L.RESERVE_SHARE))

    def test_cargo_uses_the_biggest_ships_and_only_what_exists(self):
        ships, cap = L.pick_cargo({"small_cargo": 10, "large_cargo": 2}, STATS, 40_000)
        self.assertEqual(ships, {"large_cargo": 2})
        self.assertEqual(cap, 50_000)
        ships, cap = L.pick_cargo({"small_cargo": 10, "large_cargo": 1}, STATS, 40_000)
        self.assertEqual(ships, {"large_cargo": 1, "small_cargo": 3})
        ships, cap = L.pick_cargo({"small_cargo": 2}, STATS, 40_000)
        self.assertEqual((ships, cap), ({"small_cargo": 2}, 10_000))
        self.assertEqual(L.pick_cargo({"light_fighter": 9}, STATS, 100), ({}, 0))

    def test_amounts_are_scaled_to_the_hold_and_fuel_must_be_cheap(self):
        fit = L.fit_to_capacity({"metal": 30_000, "crystal": 10_000, "deuterium": 0}, 20_000)
        self.assertLessEqual(sum(fit.values()), 20_000)
        self.assertEqual(fit["metal"], 15_000)
        self.assertTrue(L.fuel_ok({"metal": 100_000, "crystal": 0, "deuterium": 0}, 1_000))      # 3,000 of 100,000
        self.assertFalse(L.fuel_ok({"metal": 100_000, "crystal": 0, "deuterium": 0}, 2_000))     # 6,000 > 5%

    def test_only_miners_and_turtles_build_cargo_and_only_a_few(self):
        units = [{"machine_name": "small_cargo", "requirements_met": True, "max_buildable": 400,
                  "cost_per_unit": {"metal": 2000, "crystal": 2000, "deuterium": 0}}]
        have = {"metal": 1_000_000, "crystal": 1_000_000, "deuterium": 0}
        self.assertEqual(L.choose_cargo_build(PRESETS["miner"], units, have, {}), ("small_cargo", L.BUILD_CARGO_MAX))
        self.assertIsNone(L.choose_cargo_build(PRESETS["raider"], units, have, {}))
        self.assertIsNone(L.choose_cargo_build(PRESETS["miner"], units, have, {"large_cargo": 1}))
        self.assertIsNone(L.choose_cargo_build(PRESETS["miner"], units, {"metal": 20_000, "crystal": 0, "deuterium": 0}, {}))   # 10% buys none
        self.assertEqual(L.choose_cargo_build(PRESETS["miner"], units, {"metal": 100_000, "crystal": 0, "deuterium": 0}, {}), ("small_cargo", 2))


class FleetSavingRuleTests(unittest.TestCase):
    def test_attacks_are_read_with_their_target(self):
        found = L.incoming_attacks([hostile(1), hostile(2, kind="espionage"), hostile(3, kind="missile_attack"),
                                    dict(hostile(4), is_mine=True), dict(hostile(5), is_return=True)])
        self.assertEqual([a.id for a in found], [1])
        self.assertEqual((found[0].planet_id, found[0].origin), (10, (1, 1, 5)))
        home = planet(10, {"metal": 0, "crystal": 0, "deuterium": 0})
        self.assertEqual(L.attacks_on(home, found), found)
        self.assertEqual(L.attacks_on(planet(11, {"metal": 0}), found), [])

    def test_refuge_is_the_planet_farthest_from_the_attacker_that_is_not_attacked(self):
        a, b, c = (planet(1, {"metal": 0}, coord=(1, 10, 8)), planet(2, {"metal": 0}, coord=(1, 12, 8)),
                   planet(3, {"metal": 0}, coord=(1, 200, 8)))
        attacks = L.incoming_attacks([hostile(1, to=1, origin=(1, 190, 5))])
        self.assertEqual(L.pick_refuge(a, [a, b, c], attacks, (1, 190, 5))["id"], 2)
        attacks += L.incoming_attacks([hostile(2, to=2, origin=(1, 190, 5))])
        self.assertEqual(L.pick_refuge(a, [a, b, c], attacks, (1, 190, 5))["id"], 3)
        attacks += L.incoming_attacks([hostile(3, to=3, origin=(1, 190, 5))])
        self.assertIsNone(L.pick_refuge(a, [a, b, c], attacks, (1, 190, 5)))

    def test_unknown_or_strong_attacker_means_run_a_weak_one_means_stay(self):
        ships, defence = {"light_fighter": 20}, {"rocket_launcher": 100}
        rng = random.Random(1)
        self.assertFalse(L.can_win(ships, defence, None, None, rng)[0])
        self.assertFalse(L.can_win(ships, defence, 2_000_000, None, rng)[0])
        self.assertTrue(L.can_win(ships, defence, 4_000, None, rng)[0])

    def test_cargo_taken_along_fits_the_hold_and_leaves_the_fuel(self):
        self.assertEqual(L.flee_cargo({"metal": 800, "crystal": 100, "deuterium": 50}, 10_000, 30),
                         {"metal": 800, "crystal": 100, "deuterium": 20})
        self.assertEqual(sum(L.flee_cargo({"metal": 8000, "crystal": 4000, "deuterium": 0}, 6_000, 0).values()), 6_000)
        self.assertNotIn("deuterium", L.flee_cargo({"metal": 5, "crystal": 5, "deuterium": 500}, 100, None))

    def test_return_waits_for_the_landing_and_for_the_raid_to_end(self):
        entry = {"home_id": 10, "at_id": 11, "danger_until": 1130}
        planets = [planet(10, {"metal": 0}), planet(11, {"metal": 0})]
        raid = L.incoming_attacks([hostile(1, to=10)])
        self.assertFalse(L.ready_to_return(entry, 1000, [], planets))
        self.assertFalse(L.ready_to_return(entry, 1200, raid, planets))
        self.assertTrue(L.ready_to_return(entry, 1200, [], planets))

    def test_memory_round_trip_and_old_files(self):
        m = Memory()
        m.supply_next_at = 55.0
        m.away = [{"home_id": 1, "at_id": 2, "ships": {"light_fighter": 3}}]
        back = Memory.from_dict(m.to_dict())
        self.assertEqual((back.supply_next_at, back.away), (55.0, m.away))
        old = Memory.from_dict({"risk_scale": 0.8})
        self.assertEqual((old.supply_next_at, old.away), (0.0, []))
        junk = Memory.from_dict({"supply_next_at": "x", "away": "nope"})
        self.assertEqual((junk.supply_next_at, junk.away), (0.0, []))


class AgentLogisticsTests(unittest.TestCase):
    def make(self, persona=QUIET):
        game = FakeGame()
        game.logistics_stuff = True
        game.levels["shipyard"] = 1
        game.slots_max = 2
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(3), client=game)
        agent.user_id = 1
        agent.logs = []
        agent.log = lambda event, **f: agent.logs.append((event, f))
        return game, agent

    def events(self, agent, name):
        return [f for e, f in agent.logs if e == name]

    def posted(self, game, mission):
        return [b for p, b in game.post_log if p.endswith("/fleet") and b.get("mission") == mission]

    # -- supply --
    def test_rich_home_feeds_the_starved_colony_and_the_ships_come_home(self):
        game, agent = self.make()
        game.res = {"metal": 200_000.0, "crystal": 100_000.0, "deuterium": 50_000.0}
        game.ships["large_cargo"] = 3
        agent.tick()
        sent = self.events(agent, "supply_sent")
        self.assertEqual(len(sent), 1)
        trip = self.posted(game, "transport")[0]
        self.assertEqual((trip["galaxy"], trip["system"], trip["position"]), (1, 4, 6))
        self.assertGreaterEqual(sum(trip["resources"].values()), 74_990)         # three large cargos are full
        self.assertEqual(sent[0]["fuel"], game.fuel_cost)
        game.advance(125)
        self.assertGreater(game.res2["metal"], 0)
        self.assertEqual(game.ships["large_cargo"], 3)                          # they flew home alone
        self.assertEqual(sum(game.res2.values()), sum(trip["resources"].values()))
        agent.tick()
        self.assertEqual(len(self.posted(game, "transport")), 1)                # cooling down: no second trip at once

    def test_reserve_stays_home(self):
        game, agent = self.make()
        game.res = {"metal": 200_000.0, "crystal": 0.0, "deuterium": 5_000.0}
        game.ships["large_cargo"] = 10
        agent.tick()
        trip = self.posted(game, "transport")[0]
        self.assertLessEqual(trip["resources"]["metal"], 100_000)
        self.assertGreaterEqual(game.res["metal"], 60_000)

    def test_expensive_fuel_skips_the_trip(self):
        game, agent = self.make()
        game.res = {"metal": 100_000.0, "crystal": 0.0, "deuterium": 5_000.0}
        game.ships["small_cargo"] = 5
        game.fuel_cost = 2_000
        agent.tick()
        self.assertEqual(self.posted(game, "transport"), [])
        skipped = self.events(agent, "supply_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertIn("not worth the trip", skipped[0]["why"])

    def test_no_cargo_ships_a_raider_waits_a_miner_orders_a_few(self):
        game, agent = self.make(QUIET)
        game.res = {"metal": 200_000.0, "crystal": 100_000.0, "deuterium": 50_000.0}
        agent.tick()
        self.assertEqual(self.posted(game, "transport"), [])
        self.assertIn("no cargo ships", self.events(agent, "supply_skipped")[0]["why"])
        self.assertEqual(game.ships["small_cargo"], 0)

        game, agent = self.make(QUIET_MINER)
        game.res = {"metal": 200_000.0, "crystal": 100_000.0, "deuterium": 50_000.0}
        agent.tick()
        built = [b for p, b in game.post_log if p == "/planets/10/units"]
        self.assertEqual(built, [{"machine_name": "small_cargo", "amount": 5}])
        game.advance(70)
        agent.tick()
        self.assertEqual(len(self.posted(game, "transport")), 1)

    def test_single_planet_agents_are_untouched(self):
        game, agent = self.make()
        game.logistics_stuff = False
        game.res = {"metal": 200_000.0, "crystal": 100_000.0, "deuterium": 50_000.0}
        game.ships["large_cargo"] = 3
        agent.tick()
        self.assertEqual([e for e, _ in agent.logs if e.startswith(("supply", "fleet"))], [])

    # -- fleet saving --
    def test_fleet_runs_from_a_raid_it_cannot_win_and_comes_home_afterwards(self):
        game, agent = self.make()
        game.res = {"metal": 300.0, "crystal": 100.0, "deuterium": 300.0}
        game.ships["light_fighter"] = 20
        game.hostile = [hostile(left=40)]
        delay = agent.tick()
        self.assertLess(delay, 45)                                              # alert: it keeps looking often
        saved = self.events(agent, "fleet_saved")
        self.assertEqual(len(saved), 1)
        move = self.posted(game, "deployment")[0]
        self.assertEqual((move["system"], move["position"]), (4, 6))
        self.assertEqual(move["ships"], {"light_fighter": 20})
        self.assertEqual(move["resources"]["metal"], 300)
        self.assertEqual(move["resources"]["deuterium"], 300 - game.fuel_cost)
        self.assertEqual(game.ships["light_fighter"], 0)                        # nothing left to rob or shoot
        self.assertEqual(len(agent.memory.away), 1)
        game.advance(65)
        self.assertEqual(game.ships2["light_fighter"], 20)
        agent.tick()                                                            # raid not over: stays away
        self.assertEqual(self.events(agent, "fleet_returned"), [])
        game.hostile = []
        game.advance(100)
        agent.tick()
        back = self.events(agent, "fleet_returned")
        self.assertEqual([b["ok"] for b in back], [True])
        self.assertEqual(agent.memory.away, [])
        game.advance(65)
        self.assertEqual(game.ships["light_fighter"], 20)
        self.assertEqual(game.ships2["light_fighter"], 0)

    def test_return_is_remembered_across_a_restart(self):
        game, agent = self.make()
        game.res = {"metal": 0.0, "crystal": 0.0, "deuterium": 500.0}
        game.ships["light_fighter"] = 20
        game.hostile = [hostile()]
        agent.tick()
        saved = Memory.from_dict(agent.memory.to_dict())
        self.assertEqual(len(saved.away), 1)
        game.hostile = []
        game.advance(200)
        agent2 = Agent("sim", QUIET, "x", agent.store, random.Random(4), client=game)
        agent2.user_id, agent2.memory = 1, saved
        agent2.log = lambda *a, **k: None
        agent2.tick()
        game.advance(65)
        self.assertEqual(game.ships["light_fighter"], 20)

    def test_a_known_weak_attacker_is_fought_not_fled_from(self):
        game, agent = self.make()
        game.ships["light_fighter"] = 20
        game.defense["rocket_launcher"] = 100
        game.res = {"metal": 0.0, "crystal": 0.0, "deuterium": 500.0}
        agent.memory.opponents[(1, 1, 5)] = {"strength": 4_000}
        game.hostile = [hostile()]
        agent.tick()
        self.assertEqual(self.posted(game, "deployment"), [])
        self.assertEqual(len(self.events(agent, "fleet_stays")), 1)
        agent.tick()
        self.assertEqual(len(self.events(agent, "fleet_stays")), 1)             # said once, not every round

    def test_no_safe_planet_no_slot_and_no_fleet_worth_saving(self):
        game, agent = self.make()
        game.ships["light_fighter"] = 20
        game.res = {"metal": 0.0, "crystal": 0.0, "deuterium": 500.0}
        game.hostile = [hostile(1, to=10), hostile(2, to=11)]
        agent.tick()
        self.assertEqual(self.posted(game, "deployment"), [])
        self.assertIn("no other planet", self.events(agent, "fleet_save_failed")[0]["why"])

        game, agent = self.make()
        game.ships["light_fighter"] = 20
        game.res = {"metal": 0.0, "crystal": 0.0, "deuterium": 500.0}
        game.slots_max = 0
        game.hostile = [hostile()]
        agent.tick()
        self.assertIn("slot", self.events(agent, "fleet_save_failed")[0]["why"])

        game, agent = self.make()
        game.ships["espionage_probe"] = 3                                       # not worth a flight
        game.hostile = [hostile()]
        agent.tick()
        self.assertEqual(self.posted(game, "deployment"), [])
        self.assertEqual(self.events(agent, "fleet_save_failed"), [])

    def test_spy_probe_alone_does_not_move_the_fleet(self):
        game, agent = self.make()
        game.ships["light_fighter"] = 20
        game.hostile = [hostile(kind="espionage")]
        agent.tick()
        self.assertEqual(self.posted(game, "deployment"), [])

    def test_fleet_at_the_refuge_moves_on_when_that_one_is_attacked(self):
        game, agent = self.make()
        game.res = {"metal": 0.0, "crystal": 0.0, "deuterium": 500.0}
        game.res2["deuterium"] = 500.0
        game.ships["light_fighter"] = 20
        game.hostile = [hostile(1, to=10, left=40)]
        agent.tick()
        game.advance(65)
        game.hostile = [hostile(1, to=10, left=10), hostile(2, to=11, left=40)]
        agent.tick()                                                            # colony hit too, home also hit: nowhere to go
        self.assertEqual(game.ships2["light_fighter"], 20)
        game.hostile = [hostile(2, to=11, left=40)]
        agent.tick()
        self.assertEqual(len(self.posted(game, "deployment")), 2)               # back to the home planet, which is quiet now
        self.assertEqual(len(agent.memory.away), 1)
        self.assertEqual(agent.memory.away[0]["at_id"], 10)
        self.assertEqual(agent.memory.away[0]["home_id"], 10)


class LogisticsReportTests(unittest.TestCase):
    def test_report_sums_supplies_and_saved_fleets(self):
        t = 1_700_000_000
        ev = lambda dt, agent, event, **f: {"t": t + dt, "agent": agent, "event": event, **f}
        events = [
            ev(0, "a1", "supply_sent", resources={"metal": 30000, "crystal": 10000, "deuterium": 0}, value=45000, fuel=100),
            ev(1, "a1", "supply_sent", resources={"metal": 5000, "crystal": 0, "deuterium": 2000}, value=11000, fuel=50),
            ev(2, "a1", "supply_skipped", why="fuel 900 deuterium for cargo worth 20000: not worth the trip"),
            ev(3, "a2", "fleet_saved", planet=1, to=2, ships={"light_fighter": 20}, resources={"metal": 800}, value=80000, why="attacker unknown"),
            ev(4, "a2", "fleet_returned", ok=True, ships={"light_fighter": 20}),
            ev(5, "a2", "fleet_stays", planet=1, why="simulated"),
            ev(6, "a2", "fleet_save_failed", planet=1, why="no free fleet slot"),
        ]
        text = sl.summarize(events)
        self.assertIn("LOGISTICS", text)
        self.assertIn("2 supply trips sent (56,000 worth: metal 35,000, crystal 10,000, deuterium 2,000; 150 deuterium fuel), 1 skipped", text)
        self.assertIn("1 fleets saved (ships worth 80,000; resources taken along: metal 800), 1 brought home, 0 lost track of, 1 stayed to fight, 1 could not run", text)
        self.assertIn("no free fleet slot", text)

    def test_report_without_logistics_says_none(self):
        text = sl.summarize([{"t": 1_700_000_000, "agent": "a", "event": "build"}])
        self.assertRegex(text, r"LOGISTICS[^\n]*\n  none")


if __name__ == "__main__":
    unittest.main()
