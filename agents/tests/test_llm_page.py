import json
import tempfile
import unittest
from pathlib import Path

import llm_page


def w(path, rows):
    path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n", encoding="utf-8")


class LlmPageTest(unittest.TestCase):
    def test_missing_file_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as d:
            v = llm_page.llm_view(d)
            self.assertFalse(v["available"])
            self.assertEqual(v["calls"], [])

    def test_calls_newest_first_with_used_flag(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            w(d / "llm.jsonl", [
                {"t": 1, "kind": "llm_call", "agent": "agent0001", "purpose": "diary", "prompt": "p1", "answer": "Big day.", "latency": 2.0, "ok": True, "tokens": 30},
                {"t": 2, "kind": "llm_call", "agent": "agent0002", "purpose": "rewrite:OFFER", "prompt": "p2", "answer": "nope", "latency": 1.0, "ok": True},
                {"t": 3, "kind": "llm_call", "agent": "agent0003", "purpose": "diary", "prompt": "p3", "answer": None, "latency": 9.0, "ok": False, "error": "timeout"},
                "garbage {",
            ])
            w(d / "agent0001.jsonl", [{"agent": "agent0001", "event": "diary", "text": "Big day."}])
            v = llm_page.llm_view(d)
            self.assertEqual([c["t"] for c in v["calls"]], [3, 2, 1])
            self.assertEqual([c["used"] for c in v["calls"]], [False, False, True])
            self.assertEqual(v["totals"]["failed"], 1)
            self.assertEqual(v["totals"]["used"], 1)
            self.assertEqual(v["agents"], ["agent0001", "agent0002", "agent0003"])

    def test_limit_and_page_is_text_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            w(d / "llm.jsonl", [{"t": i, "kind": "llm_call", "agent": "a", "purpose": "x", "prompt": "", "answer": "y", "ok": True} for i in range(10)])
            self.assertEqual(len(llm_page.llm_view(d, 3)["calls"]), 3)
        self.assertNotIn("innerHTML", llm_page.PAGE)


if __name__ == "__main__":
    unittest.main()
