import importlib, os, unittest

class TrustSpeed(unittest.TestCase):
    def test_default_and_fast(self):
        from swarm import diplomacy as D
        rel = D.new_rel(0); rel["peace_at"] = 0.0; rel["last_hit_at"] = rel["betrayed_at"] = -1e9
        slow = D.peace_tick(rel, 3600 * 3)
        os.environ["OGAMEX_TRUST_SPEED"] = "20"
        try:
            importlib.reload(D)
            rel2 = D.new_rel(0); rel2["peace_at"] = 0.0; rel2["last_hit_at"] = rel2["betrayed_at"] = -1e9
            fast = D.peace_tick(rel2, 3600 * 3)
        finally:
            del os.environ["OGAMEX_TRUST_SPEED"]; importlib.reload(D)
        self.assertEqual(slow, 3.0); self.assertEqual(fast, 60.0)
if __name__ == "__main__": unittest.main()
