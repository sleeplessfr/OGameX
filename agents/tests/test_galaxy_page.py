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
