import unittest
import galaxy_page as G

class Fleets(unittest.TestCase):
    def test_attack_and_estimates(self):
        ev = [{"t": 1000, "agent": "a1", "event": "attack", "at": "1:5:3", "seconds": 120, "ships": {"light_fighter": 4}},
              {"t": 1000, "agent": "a1", "event": "spy", "at": "1:9:3"},
              {"t": 1000, "agent": "a1", "event": "build"},
              {"t": 1000, "agent": "a2", "event": "attack", "at": "1:5:3"}]
        out = G.fleets_from(ev, lambda a: [1, 2, 4] if a == "a1" else None, 1050)
        self.assertEqual([f["kind"] for f in out], ["attack", "spy"])
        self.assertEqual(out[0]["ships"], 4); self.assertTrue(out[0]["back"])
        self.assertGreater(out[1]["secs"], 30)
    def test_old_fleets_dropped(self):
        ev = [{"t": 0, "agent": "a", "event": "attack", "at": "1:5:3", "seconds": 60}]
        self.assertEqual(G.fleets_from(ev, lambda a: [1, 1, 1], 99999), [])
if __name__ == "__main__": unittest.main()

class PlanetView(unittest.TestCase):
    def test_split_units(self):
        d = {"id": 5, "name": "P", "type": "planet", "fields": {"used": 3, "max": 160}, "resources": {"metal": 1},
             "buildings": {"metal_mine": 4, "crystal_mine": 2}, "units": {"rocket_launcher": 10, "small_cargo": 3},
             "production_per_hour": {"metal": 9}, "building_queue": []}
        v = G.planet_view(d)
        self.assertEqual(v["defence"], {"rocket_launcher": 10}); self.assertEqual(v["ships"], {"small_cargo": 3})
        self.assertFalse(v["public"]); self.assertEqual(list(v["buildings"]), ["crystal_mine", "metal_mine"])

class Detail(unittest.TestCase):
    def test_unknown_owner_is_public(self):
        import tempfile, observatory
        with tempfile.TemporaryDirectory() as d:
            o = observatory.Observatory(d, d + "/s.json")
            out = o.planet_detail(7, "someone", "Home")
            self.assertTrue(out["public"])

class Debris(unittest.TestCase):
    def test_battle_then_recycle(self):
        ev = [{"t": 100, "event": "battle", "at": "1:5:3", "debris": 90000},
              {"t": 200, "event": "recycle_sent", "at": "1:5:3", "debris": 90000, "haul": 30000},
              {"t": 300, "event": "battle", "at": "1:6:1", "debris": 500}]
        out = G.debris_from(ev, 400)
        self.assertEqual([(d["at"], d["amount"]) for d in out], [([1, 5, 3], 60000)])
        self.assertEqual(G.debris_from(ev, 400 + 90000), [])

class Assets(unittest.TestCase):
    def test_bundled_skins(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "assets"
        self.assertEqual(len(list((root / "planets").glob("*.png"))), 70)
        self.assertEqual(len(list((root / "moons").glob("*.gif"))), 5)

class Composition(unittest.TestCase):
    def test_fleet_dicts(self):
        ev = [{"t": 1000, "agent": "a", "event": "attack", "at": "1:5:3", "seconds": 60, "ships": {"light_fighter": 7, "small_cargo": 106}},
              {"t": 1000, "agent": "a", "event": "spy", "at": "1:6:3", "probes": 3},
              {"t": 1000, "agent": "a", "event": "recycle_sent", "at": "1:7:3", "ships": 12, "seconds": 60}]
        out = {f["kind"]: f for f in G.fleets_from(ev, lambda a: [1, 2, 4], 1010)}
        self.assertEqual(out["attack"]["fleet"], {"light_fighter": 7, "small_cargo": 106})
        self.assertEqual(out["spy"]["fleet"], {"espionage_probe": 3}); self.assertEqual(out["recycle_sent"]["fleet"], {"recycler": 12})


class RelationsForMap(unittest.TestCase):
    def test_pact_and_alliance_lines(self):
        import galaxy_page as G
        ev = [{"event": "diplo_step", "agent": "agent0001", "with": "Bob", "to": "pact", "t": 1},
              {"event": "diplo_step", "agent": "agent0002", "with": "Cy", "to": "friendly", "t": 1},
              {"event": "diplo_step", "agent": "agent0003", "with": "Dee", "to": "alliance", "t": 1}]
        main = {"Al": [1, 5, 3], "Bob": [1, 9, 4], "Cy": [1, 2, 2], "Eve": [1, 7, 7], "Dee": [1, 8, 8]}
        users = {"agent0001": "Al", "agent0002": "Eve", "agent0003": "Eve"}
        out = G.relations_for_map(ev, main, users)
        self.assertEqual({(r["step"]) for r in out}, {"pact", "alliance"})
        self.assertEqual(len(out), 2)
    def test_never_raises(self):
        import galaxy_page as G
        self.assertEqual(G.relations_for_map([{"event": 5}], {}, {}), [])
