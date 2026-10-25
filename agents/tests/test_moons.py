import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import summarize_logs as sl
from swarm import moons as M
from swarm.agent import Agent, TokenStore
from swarm.military import Memory
from swarm.personas import PRESETS
from tests.fakegame import FakeGame

# A fighter-minded agent that leaves raiding and spying alone, so only the moon logic acts.
GENERAL = replace(PRESETS["balanced"], aggression=0.0, scout_interest=0.0, army_share=0.0, probes_wanted=0, expansion=0.0,
                  caution=0.5, character_class="general", expedition_interest=0.0)
PEACEFUL = replace(GENERAL, character_class="collector", caution=0.0)
HOME, VICTIM = (1, 3, 8), (1, 1, 5)
RICH = {"metal": 600_000.0, "crystal": 600_000.0, "deuterium": 300_000.0}


def row(name, level=0, met=True, cost=(20000, 40000, 20000), afford=True):
    return {"machine_name": name, "level": level, "next_level": level + 1, "requirements_met": met, "can_afford": afford,
            "cost": dict(zip(("metal", "crystal", "deuterium"), cost))}


def attack_on_home(left=300, now=0, id=77):
    """What a scan of VICTIM shows when its fleet is out attacking us: the predicted return flight (origin = our planet)."""
    return {"mission_id": id, "mission_type": 1, "mission": "Attack", "direction": "Enemy fleet", "hostile": True, "is_return_trip": True,
            "owner_id": 2, "origin": {"galaxy": 1, "system": 3, "position": 8}, "destination": {"galaxy": 1, "system": 1, "position": 5},
            "ships": {"light_fighter": 30}, "ship_count": 30, "speed": 12500, "departure_time": now + left, "arrival_time": now + 2 * left,
            "seconds_until_arrival": 2 * left}


class MoonRuleTests(unittest.TestCase):
    def test_who_wants_a_phalanx(self):
        self.assertEqual(M.wanted_level(PRESETS["raider"], 0), M.MILITARY_LEVEL)
        self.assertEqual(M.wanted_level(PRESETS["balanced"], 0), M.MILITARY_LEVEL)          # general class
        self.assertEqual(M.wanted_level(PRESETS["miner"], 10_000), 0)                        # peaceful and poor
        self.assertEqual(M.wanted_level(PRESETS["miner"], M.RICH_VALUE), M.CIVIL_LEVEL)      # peaceful but rich

    def test_build_choice_lunar_base_first_then_phalanx(self):
        blocked = [row("lunar_base"), row("sensor_phalanx", met=False)]
        self.assertEqual(M.choose_moon_build(GENERAL, blocked, 0)["machine_name"], "lunar_base")
        open_ = [row("lunar_base", 1), row("sensor_phalanx")]
        self.assertEqual(M.choose_moon_build(GENERAL, open_, 0)["machine_name"], "sensor_phalanx")
        done = [row("lunar_base", 1), row("sensor_phalanx", M.MILITARY_LEVEL)]
        self.assertIsNone(M.choose_moon_build(GENERAL, done, 0))
        self.assertIsNone(M.choose_moon_build(PEACEFUL, open_, 0))                           # poor and peaceful
        self.assertIsNone(M.choose_moon_build(GENERAL, [row("lunar_base")], 0))              # a game without the phalanx

    def test_shortage_and_feed(self):
        item = row("sensor_phalanx")
        need = M.shortage(item, {"metal": 25000, "crystal": 0, "deuterium": 5000})
        self.assertEqual(need, {"crystal": 40000, "deuterium": 15000})
        src = {"metal": 100_000, "crystal": 100_000, "deuterium": 50_000}
        self.assertEqual(M.feed_amounts(src, need, 10, 100_000), need)
        self.assertIsNone(M.feed_amounts(src, need, 10, 50_000))                             # ships too small
        self.assertIsNone(M.feed_amounts({**src, "crystal": 50_000}, need, 10, 100_000))     # would break the 30% reserve
        self.assertIsNone(M.feed_amounts({**src, "deuterium": 20_000}, {"deuterium": 15_000}, 10, 100_000))
        self.assertEqual(M.scan_shortage({"deuterium": 1000}), {"deuterium": 9000})
        self.assertEqual(M.scan_shortage({"deuterium": 6000}), {})

    def test_range_wraps_around_the_galaxy(self):
        self.assertEqual([M.scan_range(n) for n in (1, 2, 3, 4)], [0, 3, 8, 15])
        self.assertEqual(M.system_distance(2, 498), 3)
        self.assertTrue(M.in_range((1, 3, 8), 2, (1, 499, 4)))       # 3 systems away across the edge
        self.assertFalse(M.in_range((1, 3, 8), 2, (2, 3, 4)))        # other galaxy
        self.assertFalse(M.in_range((1, 3, 8), 0, (1, 3, 4)))        # no phalanx
        self.assertTrue(M.in_range((1, 3, 8), 3, (1, 10, 4), 1.0))
        self.assertFalse(M.in_range((1, 3, 8), 2, (1, 7, 4), 1.0))
        self.assertTrue(M.in_range((1, 3, 8), 2, (1, 7, 4), 1.4))    # discoverer bonus

    def test_scan_budget(self):
        st = M.new_state()
        self.assertTrue(M.may_scan(GENERAL, st, 1000, 6000)[0])
        self.assertFalse(M.may_scan(GENERAL, st, 1000, 4000)[0])
        for i in range(M.hourly_cap(GENERAL)):
            M.record_scan(st, 1000 + i * 200, VICTIM)
        ok, why = M.may_scan(GENERAL, st, 3000, 50_000)
        self.assertFalse(ok)
        self.assertIn("cap", why)
        self.assertTrue(M.may_scan(GENERAL, st, 1000 + 3700, 50_000)[0])                    # an hour later
        self.assertFalse(M.may_scan(GENERAL, M.new_state() | {"scan_next": 5000.0}, 1000, 50_000)[0])
        self.assertLess(M.hourly_cap(PEACEFUL), M.hourly_cap(GENERAL))
        self.assertLess(M.scan_gap(True), M.scan_gap(False))

    def test_who_gets_scanned_first(self):
        st = M.new_state()
        players = [{"is_me": False, "main_planet": {"galaxy": 1, "system": 2, "position": 4}},
                   {"is_me": True, "main_planet": {"galaxy": 1, "system": 3, "position": 8}},
                   {"is_me": False, "main_planet": {"galaxy": 1, "system": 300, "position": 4}}]       # far away
        args = dict(moon=HOME, level=3, own={HOME}, players=players)
        self.assertEqual(M.pick_scan_target(GENERAL, st, 1000, aggressors={}, targets={}, **args), ((1, 2, 4), "other"))
        # a raider's target beats a stranger, a known aggressor beats both
        self.assertEqual(M.pick_scan_target(GENERAL, st, 1000, aggressors={}, targets={(1, 5, 5): 9000}, **args), ((1, 5, 5), "target"))
        self.assertEqual(M.pick_scan_target(GENERAL, st, 1000, aggressors={(1, 6, 6): 900}, targets={(1, 5, 5): 9000}, **args), ((1, 6, 6), "aggressor"))
        # a peaceful agent does not plan raids
        self.assertEqual(M.pick_scan_target(PEACEFUL, st, 1000, aggressors={}, targets={(1, 5, 5): 9000}, **args), ((1, 2, 4), "other"))
        # out of range, own planets, refused planets and recent scans are left out
        self.assertIsNone(M.pick_scan_target(GENERAL, st, 1000, aggressors={}, targets={(1, 400, 5): 9000}, **{**args, "players": []}))
        self.assertIsNone(M.pick_scan_target(GENERAL, st, 1000, aggressors={}, targets={HOME: 9000}, **{**args, "players": []}))
        M.mark_nogo(st, (1, 5, 5), 1000)
        self.assertIsNone(M.pick_scan_target(GENERAL, st, 1000, aggressors={}, targets={(1, 5, 5): 9000}, **{**args, "players": []}))
        M.record_scan(st, 1000, (1, 6, 6))
        self.assertIsNone(M.pick_scan_target(GENERAL, st, 1100, aggressors={(1, 6, 6): 900}, targets={}, **{**args, "players": []}))
        self.assertIsNotNone(M.pick_scan_target(GENERAL, st, 1000 + M.RESCAN_AFTER["aggressor"] + 1, aggressors={(1, 6, 6): 1000}, targets={}, **{**args, "players": []}))

    def test_reading_a_scan(self):
        own = {HOME}
        away_trip = {**attack_on_home(), "mission_id": 5, "hostile": False, "is_return_trip": True, "origin": {"galaxy": 1, "system": 9, "position": 9},
                     "destination": {"galaxy": 1, "system": 1, "position": 5}, "arrival_time": 900, "ships": {"cruiser": 4}}
        warnings, away = M.read_scan([attack_on_home(300)], own, VICTIM, 0)
        self.assertEqual([(w["id"], w["lands"], w["to"], w["origin"]) for w in warnings], [(77, 300.0, HOME, VICTIM)])
        self.assertEqual(away["ships"], {"light_fighter": 30})
        # already landed, or not hostile, or aimed at somebody else: no warning
        self.assertEqual(M.read_scan([attack_on_home(-10)], own, VICTIM, 0)[0], [])
        self.assertEqual(M.read_scan([{**attack_on_home(), "hostile": False}], own, VICTIM, 0)[0], [])
        self.assertEqual(M.read_scan([away_trip], own, VICTIM, 0)[0], [])
        self.assertEqual(M.read_scan([away_trip], own, VICTIM, 0)[1], {"until": 900.0, "ships": {"cruiser": 4}})
        self.assertEqual(M.read_scan([], own, VICTIM, 0), ([], None))
        self.assertEqual(M.read_scan([{"junk": 1}], own, VICTIM, 0), ([], None))

    def test_warnings_are_new_once_and_expire(self):
        st = M.new_state()
        w, away = M.read_scan([attack_on_home(300)], {HOME}, VICTIM, 0)
        self.assertEqual(len(M.remember_scan(st, 0, VICTIM, w, away)), 1)
        self.assertEqual(len(M.remember_scan(st, 60, VICTIM, w, away)), 0)
        active = M.active_warnings(st, 100)
        self.assertEqual((active[0]["arrives_in"], active[0]["origin"], active[0]["to"]), (200.0, VICTIM, HOME))
        M.prune(st, 700)
        self.assertEqual((st["warn"], st["away"]), ({}, {}))
        self.assertEqual(M.active_warnings(st, 300), [])

    def test_a_fleet_that_is_away_makes_the_raid_a_little_easier(self):
        st = M.new_state()
        self.assertEqual(M.away_factor(st, VICTIM, 0), 1.0)
        M.remember_scan(st, 0, VICTIM, [], {"until": 600.0, "ships": {"light_fighter": 30}})
        f = M.away_factor(st, VICTIM, 0)
        self.assertTrue(1.0 < f <= 1.0 + M.AWAY_MAX_SHARE)
        self.assertGreater(M.away_factor(st, VICTIM, 0, strength=1000), 1.0)
        self.assertLessEqual(M.away_factor(st, VICTIM, 0, strength=1000), 1.0 + M.AWAY_MAX_SHARE)
        self.assertEqual(M.away_factor(st, VICTIM, 580), 1.0)           # back within a minute: no help
        self.assertEqual(M.away_factor(None, VICTIM, 0), 1.0)

    def test_memory_round_trip_and_old_files(self):
        m = Memory()
        w, away = M.read_scan([attack_on_home(300)], {HOME}, VICTIM, 0)
        M.remember_scan(m.moons, 0, VICTIM, w, away)
        m.moons["levels"]["12"] = 2
        back = Memory.from_dict(m.to_dict())
        self.assertEqual(back.moons["levels"], {"12": 2})
        self.assertEqual(len(M.active_warnings(back.moons, 0)), 1)
        self.assertEqual(Memory.from_dict({"risk_scale": 0.8}).moons, M.new_state())
        self.assertEqual(Memory.from_dict({"moons": "junk"}).moons, M.new_state())
        self.assertEqual(Memory.from_dict({"moons": {"levels": []}}).moons["levels"], {})


class AgentMoonTests(unittest.TestCase):
    def make(self, persona=GENERAL, moon=True):
        game = FakeGame()
        game.moon_stuff = moon
        game.logistics_stuff = True
        game.levels.update(shipyard=1, metal_store=6, crystal_store=6, deuterium_store=6)
        game.bq = {"name": "metal_mine", "level": 1, "end": 10 ** 9, "dur": 10 ** 9}   # the home planet stays busy: its stock is for the moon
        game.slots_max = 3
        game.res = dict(RICH)
        store = TokenStore(str(Path(tempfile.mkdtemp()) / "t.json"))
        agent = Agent("sim", persona, "x", store, random.Random(3), client=game)
        agent.user_id = 1
        agent.logs = []
        agent.log = lambda event, **f: agent.logs.append((event, f))
        return game, agent

    def events(self, agent, name):
        return [f for e, f in agent.logs if e == name]

    def posted(self, game, suffix):
        return [(p, b) for p, b in game.post_log if p.endswith(suffix)]

    def phalanx_ready(self, game, level=2, deuterium=50_000.0):
        game.moon_levels.update(lunar_base=1, sensor_phalanx=level)
        game.res3["deuterium"] = deuterium

    def test_no_moon_nothing_changes(self):
        game, agent = self.make(moon=False)
        game.ships["large_cargo"] = 3
        for _ in range(3):
            agent.tick()
            game.advance(100)
        self.assertEqual([e for e, _ in agent.logs if e.startswith(("moon", "phalanx"))], [])
        self.assertEqual(self.posted(game, "/planets/12/buildings"), [])
        self.assertEqual(agent.memory.moons, M.new_state())

    def test_moon_is_seen_fed_and_gets_its_lunar_base_and_phalanx(self):
        game, agent = self.make()
        game.ships["large_cargo"] = 6
        for _ in range(40):
            agent.tick()
            game.advance(130)
            if game.moon_levels["sensor_phalanx"] >= 1:
                agent.tick()
                break
        self.assertEqual(len(self.events(agent, "moon_seen")), 1)
        self.assertGreaterEqual(len(self.events(agent, "moon_fed")), 1)
        transport = [b for p, b in self.posted(game, "/planets/10/fleet") if b.get("type") == "moon"]
        self.assertTrue(transport and all(b["mission"] == "transport" for b in transport))
        built = [b["machine_name"] for _, b in self.posted(game, "/planets/12/buildings")]
        self.assertEqual(built[:2], ["lunar_base", "sensor_phalanx"])
        self.assertGreaterEqual(game.moon_levels["sensor_phalanx"], 1)
        got = self.events(agent, "phalanx_built")
        self.assertTrue(got and got[0]["level"] == 1)
        self.assertEqual(game.ships["large_cargo"], 6)                    # the cargo ships flew home

    def test_no_cargo_ships_means_no_delivery_and_one_quiet_line(self):
        game, agent = self.make()
        for _ in range(3):
            agent.tick()
            game.advance(130)
        self.assertEqual(self.events(agent, "moon_fed"), [])
        skipped = self.events(agent, "phalanx_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertIn("no cargo ships", skipped[0]["why"])

    def test_a_peaceful_poor_agent_leaves_the_moon_alone(self):
        game, agent = self.make(PEACEFUL)
        game.res = {"metal": 5000.0, "crystal": 5000.0, "deuterium": 5000.0}
        game.ships["large_cargo"] = 3
        for _ in range(3):
            agent.tick()
            game.advance(130)
        self.assertEqual(len(self.events(agent, "moon_seen")), 1)
        self.assertEqual(self.posted(game, "/planets/12/buildings"), [])
        self.assertEqual(self.events(agent, "moon_fed"), [])

    def test_scan_logs_and_costs_deuterium_and_respects_the_gap(self):
        game, agent = self.make()
        self.phalanx_ready(game, level=3)
        agent.tick()
        scans = self.events(agent, "phalanx_scan")
        self.assertEqual(len(scans), 1)
        self.assertEqual((scans[0]["at"], scans[0]["fleets"], scans[0]["hostile"]), ("1:1:5", 0, 0))
        self.assertEqual(game.res3["deuterium"], 45_000)
        agent.tick()                                                       # same moment: not due again
        self.assertEqual(len(game.scans), 1)
        game.advance(M.SCAN_GAP + 10)
        agent.tick()                                                       # due, but the only planet in range was scanned a moment ago
        self.assertEqual(len(game.scans), 1)

    def test_scan_budget_is_capped_per_hour(self):
        game, agent = self.make()
        self.phalanx_ready(game, level=3, deuterium=400_000.0)
        game.others += [{"user_id": 10 + i, "username": f"P{i}", "coord": (1, 2 + i, 3), "score": 0,
                         "resources": {"metal": 0, "crystal": 0, "deuterium": 0}, "ships": {}, "defense": {}} for i in range(12)]
        for _ in range(30):
            agent.tick()
            game.advance(M.SCAN_GAP + 5)
            if game.now > 3500:
                break
        self.assertLessEqual(len(game.scans), M.hourly_cap(GENERAL))
        self.assertGreaterEqual(len(game.scans), 3)

    def test_phalanx_warning_triggers_defence_and_fleet_saving_before_the_fleet_shows(self):
        game, agent = self.make()
        self.phalanx_ready(game)
        game.ships["light_fighter"] = 20
        game.phalanx_fleets[VICTIM] = [attack_on_home(left=300, now=0)]
        self.assertEqual(game.hostile, [])                                  # /fleet knows nothing yet
        agent.tick()
        warn = self.events(agent, "phalanx_warning")
        self.assertEqual(len(warn), 1)
        self.assertEqual((warn[0]["attacker"], warn[0]["planet"], warn[0]["ships"]), ("1:1:5", "1:3:8", 30))
        self.assertAlmostEqual(warn[0]["lands_in"], 300, delta=5)
        self.assertEqual(len(self.events(agent, "threat")), 0)               # not a /fleet threat
        self.assertGreater(agent.alert_until, agent.now)
        self.assertIn(VICTIM, agent.memory.aggressors)
        self.assertTrue(self.events(agent, "defend"), "the defence order should have followed")
        saved = self.events(agent, "fleet_saved")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["attacker"], "1:1:5")

    def test_a_threat_that_fleet_already_shows_is_not_counted_twice(self):
        game, agent = self.make()
        self.phalanx_ready(game)
        game.phalanx_fleets[VICTIM] = [attack_on_home(left=300, now=0)]
        game.hostile = [{"id": 5, "mission": "attack", "is_mine": False, "is_return": False, "arrive_at": 300,
                         "from": {"galaxy": 1, "system": 1, "position": 5}, "to": {"planet_id": 10, "galaxy": 1, "system": 3, "position": 8}}]
        agent.tick()
        self.assertEqual(len(self.events(agent, "threat")), 1)
        self.assertEqual(agent._phalanx_threats(agent._watch()), [])
        self.assertEqual(agent._phalanx_attacks([{"id": 10, "coordinates": {"galaxy": 1, "system": 3, "position": 8}}],
                                                [__import__("swarm.logistics", fromlist=["x"]).Attack(5, 10, HOME, VICTIM, 300.0)]), [])

    def test_a_landed_warning_goes_away(self):
        game, agent = self.make()
        self.phalanx_ready(game)
        game.phalanx_fleets[VICTIM] = [attack_on_home(left=100, now=0)]
        agent.tick()
        self.assertEqual(len(agent._phalanx_threats([])), 1)
        game.advance(100 + M.WARN_KEEP + 5)
        agent.now = game.now
        M.prune(agent.memory.moons, agent.now)
        self.assertEqual(agent._phalanx_threats([]), [])

    def test_fleets_away_are_remembered_for_raiding(self):
        game, agent = self.make()
        self.phalanx_ready(game)
        trip = {**attack_on_home(), "hostile": False, "origin": {"galaxy": 1, "system": 9, "position": 9}}
        game.phalanx_fleets[VICTIM] = [trip]
        agent.tick()
        scan = self.events(agent, "phalanx_scan")[0]
        self.assertTrue(scan["away"])
        self.assertEqual(self.events(agent, "phalanx_warning"), [])
        self.assertGreater(M.away_factor(agent.memory.moons, VICTIM, agent.now), 1.0)

    def test_out_of_range_and_vanished_planets_are_noted_not_repeated(self):
        game, agent = self.make()
        self.phalanx_ready(game, level=2)
        agent.memory.aggressors[(1, 3, 20)] = 0.0                           # looks reachable to the agent, and the game has nobody there
        agent.now = 0.0
        agent.tick()
        agent.tick()
        skipped = [f for f in self.events(agent, "phalanx_skipped") if f.get("code")]
        self.assertEqual([f["code"] for f in skipped], ["no_planet_at_target"])
        self.assertIn("1:3:20", agent.memory.moons["nogo"])

    def test_a_scanning_agent_with_an_empty_moon_gets_deuterium_brought(self):
        game, agent = self.make()
        self.phalanx_ready(game, level=3, deuterium=0.0)
        game.ships["large_cargo"] = 2
        agent.tick()
        fed = self.events(agent, "moon_fed")
        self.assertEqual(len(fed), 1)
        self.assertEqual(fed[0]["resources"], {"deuterium": 2 * M.SCAN_COST})
        game.advance(70)
        agent.tick()
        self.assertEqual(len(self.events(agent, "phalanx_scan")), 1)

    def test_a_game_without_moon_endpoints_is_left_alone(self):
        game, agent = self.make()
        game.no_moon_endpoints = True
        agent.tick()
        agent.tick()
        self.assertTrue(agent.memory.moons["off"])
        self.assertEqual(len([f for f in self.events(agent, "phalanx_skipped") if "no moon endpoints" in f["why"]]), 1)
        self.assertEqual(self.posted(game, "/planets/12/buildings"), [])

    def test_a_server_error_on_the_moon_does_not_stop_the_round(self):
        game, agent = self.make()
        self.phalanx_ready(game, level=3)
        real = game.post

        def broken(path, body=None):
            if path.endswith("/phalanx"):
                from swarm.client import ApiError
                raise ApiError(500, "http_error", "boom")
            return real(path, body)
        game.post = broken
        agent.tick()                                                        # must not raise
        self.assertEqual(len(self.events(agent, "phalanx_skipped")), 1)
        self.assertGreater(agent.memory.moons["scan_next"], agent.now)


class MoonSummaryTests(unittest.TestCase):
    def test_summary_has_a_moons_section(self):
        t = 1_700_000_000
        events = [
            {"t": t, "agent": "a1", "event": "moon_seen", "moon": 12, "at": "1:3:8"},
            {"t": t + 1, "agent": "a1", "event": "moon_fed", "moon": 12, "resources": {"metal": 20000, "crystal": 40000, "deuterium": 20000}, "fuel": 10},
            {"t": t + 2, "agent": "a1", "event": "phalanx_built", "moon": 12, "level": 2, "range": 3},
            {"t": t + 3, "agent": "a1", "event": "phalanx_scan", "moon": 12, "at": "1:1:5", "fleets": 1, "hostile": 1, "away": True, "cost": 5000, "why": "aggressor"},
            {"t": t + 4, "agent": "a1", "event": "phalanx_scan", "moon": 12, "at": "1:2:5", "fleets": 0, "hostile": 0, "away": False, "cost": 5000, "why": "other"},
            {"t": t + 5, "agent": "a1", "event": "phalanx_warning", "moon": 12, "attacker": "1:1:5", "planet": "1:3:8", "ships": 30, "lands_in": 300},
            {"t": t + 6, "agent": "a1", "event": "phalanx_skipped", "moon": 12, "why": "cannot bring for sensor_phalanx level 3: no cargo ships"},
        ]
        text = "\n".join(sl.moons_section(events)) if hasattr(sl, "moons_section") else ""
        self.assertIn("MOONS", text)
        self.assertIn("1 moons seen", text)
        self.assertIn("2 scans", text)
        self.assertIn("10,000 deuterium", text)
        self.assertIn("1 warnings", text)
        self.assertIn("1:1:5", text)
        self.assertIn("no cargo ships", text)

    def test_empty_section_says_none(self):
        self.assertIn("none", "\n".join(sl.moons_section([])))


if __name__ == "__main__":
    unittest.main()
