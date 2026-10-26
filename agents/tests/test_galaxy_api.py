import tempfile, unittest, json
import observatory

class GalaxyApi(unittest.TestCase):
    def test_galaxy_data_callable(self):
        with tempfile.TemporaryDirectory() as d:
            o = observatory.Observatory(d, d + "/state.json")
            out = o.galaxy_data()
            self.assertIn("fleets", out); json.dumps(out)
if __name__ == "__main__": unittest.main()
