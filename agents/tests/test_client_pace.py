import time
import unittest

from swarm.client import ApiClient


class SharedPaceTests(unittest.TestCase):
    def test_clients_share_one_pace(self):
        old = ApiClient.shared_interval
        ApiClient.shared_interval = 0.05
        ApiClient._shared_next = 0.0
        try:
            waits = [ApiClient._shared_pause() for _ in range(5)]
        finally:
            ApiClient.shared_interval = old
            ApiClient._shared_next = 0.0
        # five clients asking at the same instant are spread out, not served together
        self.assertAlmostEqual(waits[4] - waits[0], 0.2, delta=0.03)
        self.assertEqual(sorted(waits), waits)


if __name__ == "__main__":
    unittest.main()
