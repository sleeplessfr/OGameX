import random, unittest
from swarm import military as M

class GambleCargo(unittest.TestCase):
    def test_cargo_counts_as_risk(self):
        self.assertIn("small_cargo", M.CARGOS)
        fleet = {"light_fighter": 7, "small_cargo": 106}
        risk = sum(M.unit_value(n) * fleet.get(n, 0) for n in M.FIGHTERS + M.CARGOS)
        self.assertGreater(risk, 100000)
if __name__ == "__main__": unittest.main()
