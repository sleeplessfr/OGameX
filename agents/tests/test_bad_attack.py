"""Regression: the 12:41 raid (160 LF + 1 HF + 104 SC against 547 rocket launchers) approved from an ally's count-only intel."""
import random
import unittest
from dataclasses import replace

from swarm.combat import harmless_fleet
from swarm.defense import find_threats
from swarm.logistics import incoming_attacks
from swarm.military import ALLY_INTEL_MAX_AGE, INTEL_MAX_AGE, Intel, Memory, assess_attack, choose_strike, pick_attack
from swarm.moons import read_scan
from swarm.personas import PRESETS

RAIDER = replace(PRESETS["raider"], randomness=0.0, aggression=1.0)
STATS = {"light_fighter": {"cargo_capacity": 50}, "heavy_fighter": {"cargo_capacity": 100}, "small_cargo": {"cargo_capacity": 5000}}
HOME = {"light_fighter": 1210, "heavy_fighter": 40, "small_cargo": 200}
NOW = 100_000.0


def ally_entry(age=60, defenders=743, loot=526_599):
    return {"at": NOW - age, "loot": loot, "defenders": defenders, "owner_id": 7, "ships": None, "defense": None, "research": None, "from_ally": 3}


class BadAttackTests(unittest.TestCase):
    def test_exact_case_sends_nothing(self):
        # the real garrison was 547 rocket launchers + 1 probe; the ally only said "743 defenders"
        intel = Intel(526_599, 743, 7, at=NOW - 60, ally=3)
        sent = {"light_fighter": 160, "heavy_fighter": 1, "small_cargo": 104}
        strike, go, why, kind = choose_strike(RAIDER, random.Random(1), sent, STATS, intel, (0, 0, 0))
        self.assertFalse(go, why)
        self.assertTrue(why.startswith("simulated fight"), why)
        self.assertIn("rocket launchers", why)
        self.assertFalse(choose_strike(RAIDER, random.Random(1), HOME, STATS, intel, (0, 0, 0))[1])

    def test_pick_then_strike_in_the_agent_flow_never_goes(self):
        for seed in range(5):
            mem = Memory(intel={(1, 9, 6): ally_entry()})
            pick = pick_attack(RAIDER, random.Random(seed), mem, NOW, HOME)
            if pick is None:
                continue
            _, intel, _ = pick
            info = {}
            _, go, _, _ = choose_strike(RAIDER, random.Random(seed), HOME, STATS, intel, (0, 0, 0), info=info)
            self.assertFalse(go)
            self.assertEqual(info["win_chance"], 0.0)

    def test_count_compared_is_the_strike_force_not_everything_at_home(self):
        # 743 defenders vs 1210 fighters at home looked fine for a raider; against the force that really fights it does not
        persona = replace(RAIDER, risk_ratio=1.0, strike_fraction=0.1)
        mem = Memory(intel={(1, 9, 6): ally_entry(defenders=743)})
        self.assertIsNone(pick_attack(persona, random.Random(1), mem, NOW, {"light_fighter": 1210, "small_cargo": 900}))

    def test_ally_intel_needs_a_fresh_own_spy(self):
        intel = Intel(50_000, 0, 7, at=NOW - (ALLY_INTEL_MAX_AGE + 10), ally=3)
        self.assertFalse(assess_attack(RAIDER, random.Random(1), intel, 50, now=NOW)[0])
        own = Intel(50_000, 0, 7, at=NOW - (ALLY_INTEL_MAX_AGE + 10), ally=None)
        self.assertTrue(assess_attack(RAIDER, random.Random(1), own, 50, now=NOW)[0])

    def test_old_own_report_is_refused(self):
        old = Intel(50_000, 0, 7, at=NOW - INTEL_MAX_AGE - 5)
        self.assertFalse(assess_attack(RAIDER, random.Random(1), old, 50, now=NOW)[0])

    def test_a_good_attack_is_unchanged(self):
        intel = Intel(40_000, 4, 3, ships={}, defense={"rocket_launcher": 4}, at=NOW - 30)
        _, go, why, _ = choose_strike(RAIDER, random.Random(1), {"light_fighter": 100, "small_cargo": 20}, {"light_fighter": {"cargo_capacity": 50}, "small_cargo": {"cargo_capacity": 5000}}, intel, (0, 0, 0))
        self.assertTrue(go, why)
        small = Intel(30_000, 10, 3, at=NOW - 30, ally=3)   # tiny unknown garrison: old rule still decides
        self.assertTrue(assess_attack(RAIDER, random.Random(1), small, 50, now=NOW)[0])


def hostile(ships=None, kind="attack"):
    m = {"id": 1, "mission": kind, "is_mine": False, "is_return": False, "seconds_remaining": 60,
         "from": {"galaxy": 1, "system": 1, "position": 5}, "to": {"planet_id": 10, "galaxy": 1, "system": 3, "position": 8}}
    if ships is not None:
        m["ships"] = ships
    return m


class HarmlessFleetTests(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(harmless_fleet({"recycler": 3}))
        self.assertTrue(harmless_fleet({"recycler": 3, "espionage_probe": 1, "small_cargo": 0, "colony_ship": 1}))
        self.assertFalse(harmless_fleet({"recycler": 3, "cruiser": 1}))
        self.assertFalse(harmless_fleet({"recycler": 0}))
        self.assertFalse(harmless_fleet(None))
        self.assertFalse(harmless_fleet({}))

    def test_harmless_attack_is_no_threat_and_no_fleet_save(self):
        self.assertEqual(incoming_attacks([hostile({"recycler": 3})]), [])
        self.assertEqual(find_threats([hostile({"recycler": 3, "large_cargo": 2})]), [])

    def test_any_warship_or_no_data_still_counts(self):
        self.assertEqual(len(incoming_attacks([hostile({"recycler": 3, "light_fighter": 1})])), 1)
        self.assertEqual(len(incoming_attacks([hostile()])), 1)
        self.assertEqual(len(find_threats([hostile({"cruiser": 2})])), 1)
        self.assertEqual(len(find_threats([hostile()])), 1)

    def test_espionage_is_still_a_threat_for_logging(self):
        t = find_threats([hostile({"espionage_probe": 3}, kind="espionage")])
        self.assertEqual([x.kind for x in t], ["espionage"])

    def test_phalanx_ignores_harmless_return_flights(self):
        own = {(1, 3, 8)}
        fleet = lambda ships: {"hostile": True, "is_return_trip": True, "origin": {"galaxy": 1, "system": 3, "position": 8},
                               "destination": {"galaxy": 1, "system": 9, "position": 6}, "departure_time": NOW + 100,
                               "arrival_time": NOW + 200, "mission_id": 4, "ship_count": 3, "ships": ships}
        self.assertEqual(read_scan([fleet({"recycler": 3})], own, (1, 9, 6), NOW)[0], [])
        self.assertEqual(len(read_scan([fleet({"cruiser": 3})], own, (1, 9, 6), NOW)[0]), 1)


if __name__ == "__main__":
    unittest.main()
