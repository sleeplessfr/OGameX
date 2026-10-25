"""The bounty parts of the observatory (Board tab, newspaper) and of the board's own page: read only, fail soft, text only."""
import json
import tempfile
import unittest
from pathlib import Path

import board_page
import observatory as ob
from tests.test_observatory import T, line


def post(id, kind, agent, data, t=T, text="x"):
    return {"id": id, "t": t, "expires": t + 3600, "agent": agent, "kind": kind, "text": text, "data": data}


BOUNTY = post(1, "BOUNTY", "agent0003", {"target_player_id": 2, "target_at": "1:1:5", "reward": {"metal": 4000}, "expires": T + 6 * 3600,
                                          "poster_id": 3, "poster_at": "1:5:2"}, text="WANTED: player 2 at 1:1:5")
CLAIM = post(2, "CLAIM", "agent0001", {"bounty_id": 1, "report": 77, "at": "1:1:5", "claimant_id": 1, "claimant_at": "1:3:8", "loot": 9000}, t=T + 60)
YES = post(3, "VERDICT", "referee", {"bounty_id": 1, "claim_id": 2, "ok": True, "why": "confirmed", "claimant": "agent0001"}, t=T + 90)
NO = post(3, "VERDICT", "referee", {"bounty_id": 1, "claim_id": 2, "ok": False, "why": "no loot", "claimant": "agent0001"}, t=T + 90)


class WantedTests(unittest.TestCase):
    def status(self, posts, events=(), now=T + 100):
        (w,) = ob.wanted_notices(posts, list(events), now)
        return w["status"]

    def test_the_life_of_a_bounty(self):
        self.assertEqual(self.status([BOUNTY]), "open")
        self.assertEqual(self.status([BOUNTY], now=T + 7 * 3600), "expired")
        self.assertEqual(self.status([BOUNTY, CLAIM]), "claimed, waiting for the referee")
        self.assertEqual(self.status([BOUNTY, CLAIM, YES]), "confirmed, payment due")
        self.assertEqual(self.status([BOUNTY, CLAIM, NO]), "claim rejected")
        paid = {"event": "bounty_paid", "bounty": "1", "side": "poster"}
        unpaid = {"event": "bounty_unpaid", "bounty": 1, "side": "claimant"}
        self.assertEqual(self.status([BOUNTY, CLAIM, YES], [paid]), "paid")
        self.assertEqual(self.status([BOUNTY, CLAIM, YES], [unpaid]), "unpaid")

    def test_the_notice_says_who_what_and_how_much(self):
        (w,) = ob.wanted_notices([BOUNTY, CLAIM], [], T + 100)
        self.assertEqual((w["poster"], w["target_at"], w["target_id"], w["reward"], w["claimant"]),
                         ("agent0003", "1:1:5", 2, "4,000 metal", "agent0001"))

    def test_a_verdict_from_somebody_else_is_not_a_verdict(self):
        fake = dict(YES, agent="agent0001")
        self.assertEqual(self.status([BOUNTY, CLAIM, fake]), "claimed, waiting for the referee")

    def test_garbage_is_ignored(self):
        junk = [post(5, "BOUNTY", "x", {}), post(6, "BOUNTY", "x", {"reward": "lots", "expires": "soon"}), post(7, "CLAIM", "x", {"bounty_id": [1]}),
                {"id": 8, "kind": "BOUNTY", "data": None, "agent": "x"}]
        got = ob.wanted_notices(junk + [BOUNTY], [{"event": "bounty_paid"}], T + 100)
        self.assertEqual(len(got), 4)
        self.assertIn(1, [w["id"] for w in got])   # (malformed ones are shown as plain unknowns, never an exception)


class ObservatoryBountyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "agent0003.jsonl").write_text(
            line(t=T, agent="agent0003", event="registered", player="Poster", persona="miner")
            + line(t=T + 1, agent="agent0003", event="bounty_post", bounty=1, target="1:1:5", target_id=2, reward={"metal": 4000}, value=4000, expires=T + 6 * 3600)
            + line(t=T + 200, agent="agent0003", event="bounty_paid", side="poster", bounty="1", to="agent0001", resources={"metal": 4000}, value=4000))
        (self.dir / "agent0001.jsonl").write_text(
            line(t=T, agent="agent0001", event="registered", player="Hunter", persona="raider")
            + line(t=T + 10, agent="agent0001", event="revenge_join", post=9, victim="agent0003", raider_at="1:1:5", raider_id=2, until=T + 7200, replied=True)
            + line(t=T + 60, agent="agent0001", event="bounty_claim", bounty=1, claim=2, poster="agent0003", at="1:1:5", loot=9000, report=77, reward={"metal": 4000})
            + line(t=T + 90, agent="agent0001", event="bounty_verdict", bounty=1, claim=2, ok=True, why="confirmed", role="claimant")
            + line(t=T + 250, agent="agent0001", event="bounty_paid", side="claimant", bounty="1", poster="agent0003", resources={"metal": 4000}, value=4000)
            + line(t=T + 300, agent="agent0001", event="bounty_unpaid", side="claimant", bounty="2", poster="agent0009", promised={"metal": 100}))
        self.posts = [BOUNTY, CLAIM, YES, post(4, "REVENGE", "agent0001", {"reply_to": 9, "raider_at": "1:1:5"}, t=T + 11, text="Joining agent0003")]

    def state(self, posts=None):
        reader = ob.BoardReader("http://localhost:1", fetch=lambda p: {"now": T + 400, "last_id": 9, "posts": posts or self.posts}, clock=lambda: 10 ** 9, ttl=0)
        o = ob.Observatory(self.dir, self.dir / "none.json", None, board=reader)
        o.scan()
        s = o.state()
        json.dumps(s)
        return s

    def test_the_board_tab_marks_bounties_and_joined_revenges(self):
        s = self.state()
        rows = {p["id"]: p for p in s["board"]["posts"]}
        self.assertEqual(rows[1]["status"], "paid")
        self.assertTrue(rows[4]["reply"])
        self.assertTrue(rows[4]["text"].startswith("joins the call for revenge"))
        self.assertFalse(rows[2]["reply"])
        self.assertEqual(s["board"]["counts"], {"BOUNTY": 1, "CLAIM": 1, "VERDICT": 1, "REVENGE": 1})

    def test_the_newspaper_has_wanted_notices_and_the_board_section_shows_the_new_kinds(self):
        n = self.state()["newspaper"]
        (w,) = n["wanted"]
        self.assertEqual((w["status"], w["reward"], w["target_at"]), ("paid", "4,000 metal", "1:1:5"))
        kinds = [x["kind"] for x in n["board"]]
        self.assertEqual(kinds, ["REVENGE", "BOUNTY", "CLAIM", "VERDICT"])
        titles = [n["headline"]["title"]] + [a["title"] for a in n["articles"]]
        self.assertTrue(any("never paid" in t or "pays a bounty" in t or "price on a raider" in t or "joins a revenge" in t for t in titles), titles)

    def test_the_feed_describes_the_new_events(self):
        texts = [f["text"] for f in self.state()["feed"]]
        for part in ("joined agent0003's call for revenge against 1:1:5", "put a bounty of 4,000 metal on the raider at 1:1:5",
                     "claimed bounty 1 after a raid on 1:1:5", "the referee confirmed the claim on bounty 1", "paid the bounty 4,000 metal to agent0001",
                     "was paid the bounty 4,000 metal by agent0003", "was never paid bounty 2 by agent0009; shunned"):
            self.assertTrue(any(part in t for t in texts), part)

    def test_without_a_board_there_are_no_wanted_notices(self):
        o = ob.Observatory(self.dir, self.dir / "none.json", None, board=None)
        o.scan()
        s = o.state()
        self.assertEqual(s["newspaper"]["wanted"], [])
        self.assertEqual(s["board"]["posts"], [])

    def test_a_dead_board_changes_nothing_else(self):
        reader = ob.BoardReader("http://localhost:1", fetch=lambda p: (_ for _ in ()).throw(OSError("down")), clock=lambda: 10 ** 9, ttl=0)
        o = ob.Observatory(self.dir, self.dir / "none.json", None, board=reader)
        o.scan()
        s = o.state()
        self.assertEqual(s["newspaper"]["wanted"], [])
        self.assertTrue(s["feed"])

    def test_the_pages_know_every_kind_use_the_same_colours_and_never_markup(self):
        for kind in ("BOUNTY", "CLAIM", "VERDICT", "REVENGE"):
            self.assertIn(kind + ":", ob.PAGE.split("const COLORS")[0])             # observatory colour table
            self.assertIn(kind + ":", board_page.PAGE)
        for hexcode in ("#f9c74f", "#ff9f43", "#43d9ad", "#ff4d6d"):               # the same hues in both pages
            self.assertIn(hexcode, ob.PAGE)
            self.assertIn(hexcode, board_page.PAGE)
        self.assertNotIn("innerHTML", ob.PAGE)
        self.assertNotIn("innerHTML", board_page.PAGE)
        self.assertNotIn("insertAdjacentHTML", ob.PAGE + board_page.PAGE)
        self.assertNotIn("document.write", ob.PAGE + board_page.PAGE)


if __name__ == "__main__":
    unittest.main()


class RichBoardStateTests(unittest.TestCase):
    def test_posts_carry_their_data_and_bounties_their_status(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.jsonl").write_text(line(t=T, agent="agent0003", event="registered", player="P", persona="miner"))
        reader = ob.BoardReader("http://x", fetch=lambda p: {"now": T + 100, "posts": [BOUNTY, CLAIM, YES, post(9, "OFFER", "a", {"give": {"metal": 5}, "want": {"crystal": 2}, "junk": [1]})]},
                                clock=lambda: 10 ** 9, ttl=0)
        o = ob.Observatory(d, d / "none.json", None, board=reader)
        o.scan()
        b = o.state()["board"]
        self.assertEqual(b["now"], T + 100)
        by = {p["id"]: p for p in b["posts"]}
        self.assertEqual(by[1]["data"]["reward"], {"metal": 4000})
        self.assertEqual(by[1]["status"], "confirmed, payment due")
        self.assertEqual(by[9]["data"]["give"], {"metal": 5})
        self.assertIsNone(by[9]["data"]["junk"])
        self.assertEqual(by[2]["data"]["bounty_id"], 1)
        json.dumps(b)

    def test_the_page_only_uses_textcontent(self):
        self.assertNotIn("innerHTML", ob.PAGE)
        self.assertIn("prefers-color-scheme", ob.PAGE)
