import json
import time
import unittest

import referee as R
from swarm import bounty as B
from swarm import social as S
from swarm.boardclient import BoardClient
from tests.test_bounty import Story, bounty_post
from tests.test_social import AGENTS_DIR


# ---- the referee ------------------------------------------------------------------------------------------------------------------------
def ev(agent, event, t, **kw):
    return {"t": t, "agent": agent, "event": event, **kw}


class FakeBoard:
    """What the referee needs of a board client, with posts we hand it."""
    def __init__(self, posts):
        self.posts, self.sent = list(posts), []

    def read(self, since=0, kind=None, limit=50):
        posts = [p for p in self.posts if p["id"] > since and (not kind or p["kind"] in kind)]
        return {"ok": True, "now": 2000.0, "last_id": max([p["id"] for p in self.posts] + [since]), "posts": posts}

    def post(self, kind, text, data=None, ttl=None):
        p = {"id": 1000 + len(self.sent), "t": 2000.0, "agent": R.NAME, "kind": kind, "text": text, "data": data}
        self.sent.append(p)
        self.posts.append(p)
        return p


class RefereeBase(unittest.TestCase):
    BOUNTY_T = 1000.0

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        self.bounty = B.parse_bounty(bounty_post(t=self.BOUNTY_T, expires=1000 + 6 * 3600))
        self.claim = R.parse_claim({"id": 31, "t": 1500.0, "agent": "agent0001", "kind": "CLAIM",
                                    "data": {"bounty_id": 21, "report": 77, "at": "1:1:5", "time": 1500, "claimant_id": 1,
                                             "claimant_at": "1:3:8", "loot": 9000}})
        self.good = [ev("agent0001", "attack", 1200, at="1:1:5"),
                     ev("agent0001", "battle", 1400, at="1:1:5", role="attacker", loot=9000, their_id=2, report=77)]

    def index(self, events):
        with (self.dir / "agent0001.jsonl").open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        idx = R.LogIndex(self.dir)
        idx.poll()
        return idx

    def judge(self, events, claim=None, bounty="default", paid=None, now=1600.0):
        return R.judge(claim or self.claim, self.bounty if bounty == "default" else bounty, self.index(events), paid or {}, now)


class RefereeJudgeTests(RefereeBase):
    def test_a_good_claim_is_confirmed(self):
        ok, why = self.judge(self.good)
        self.assertTrue(ok, why)
        self.assertIn("confirmed", why)

    def test_the_claimant_must_not_be_the_poster(self):
        c = dict(self.claim, agent="agent0003")
        self.assertEqual(self.judge(self.good, claim=c)[0], False)
        self.assertIn("poster", self.judge(self.good, claim=dict(self.claim, claimant_id=3))[1])

    def test_unknown_bounty_and_a_second_claim(self):
        self.assertIn("no such bounty", self.judge(self.good, bounty=None)[1])
        ok, why = self.judge(self.good, paid={21: 29})
        self.assertFalse(ok)
        self.assertIn("already claimed", why)
        self.assertTrue(self.judge(self.good, paid={21: 31})[0])        # (judging the same claim again gives the same answer)

    def test_the_target_must_be_the_bounty_target(self):
        other = [ev("agent0001", "attack", 1200, at="1:9:9"),
                 ev("agent0001", "battle", 1400, at="1:9:9", role="attacker", loot=9000, their_id=7, report=77)]
        ok, why = self.judge(other, now=1600.0)
        self.assertFalse(ok)
        self.assertIn("against the bounty's target", why)

    def test_the_planet_changed_owner_but_the_player_is_the_same(self):
        moved = [ev("agent0001", "attack", 1200, at="1:7:7"),
                 ev("agent0001", "battle", 1400, at="1:7:7", role="attacker", loot=9000, their_id=2, report=77)]
        ok, why = self.judge(moved, claim=dict(self.claim, at="1:7:7"))
        self.assertTrue(ok, why)

    def test_the_battle_must_come_after_the_bounty_and_before_it_expires(self):
        early = [ev("agent0001", "attack", 800, at="1:1:5"), ev("agent0001", "battle", 900, at="1:1:5", role="attacker", loot=9000, their_id=2, report=77)]
        self.assertIn("before the bounty", self.judge(early)[1])
        late = [ev("agent0001", "attack", 1200, at="1:1:5"),
                ev("agent0001", "battle", 1000 + 7 * 3600, at="1:1:5", role="attacker", loot=9000, their_id=2, report=77)]
        self.assertIn("expired", self.judge(late)[1])

    def test_the_attack_must_be_sent_after_the_bounty(self):
        sent_early = [ev("agent0001", "attack", 900, at="1:1:5"),
                      ev("agent0001", "battle", 1400, at="1:1:5", role="attacker", loot=9000, their_id=2, report=77)]
        ok, why = self.judge(sent_early)
        self.assertFalse(ok)
        self.assertIn("before the bounty", why)
        self.assertIn("no attack", self.judge([self.good[1]])[1])

    def test_a_battle_without_loot_is_no_win_and_an_inflated_claim_is_refused(self):
        lost = [self.good[0], dict(self.good[1], loot=0)]
        self.assertIn("no loot", self.judge(lost)[1])
        self.assertIn("more loot", self.judge(self.good, claim=dict(self.claim, loot=50_000))[1])

    def test_defender_battles_do_not_count(self):
        self.assertFalse(self.judge([self.good[0], dict(self.good[1], role="defender")])[0])

    def test_missing_evidence_waits_a_minute_and_then_says_no(self):
        self.assertIsNone(self.judge([], now=1500.0 + 10))
        ok, why = self.judge([], now=1500.0 + R.GRACE + 1)
        self.assertFalse(ok)
        self.assertIn("no battle", why)

    def test_the_log_is_read_incrementally_and_half_lines_wait(self):
        path = self.dir / "agent0001.jsonl"
        idx = R.LogIndex(self.dir)
        path.write_text(json.dumps(self.good[0]) + "\n" + json.dumps(self.good[1])[:30])
        idx.poll()
        self.assertEqual(len(idx.of("agent0001", "attack")), 1)
        self.assertEqual(idx.of("agent0001", "battle"), [])
        path.write_text(json.dumps(self.good[0]) + "\n" + json.dumps(self.good[1]) + "\nnot json\n"
                        + json.dumps(ev("agent0001", "build", 5)) + "\n")
        idx.poll()
        self.assertEqual((len(idx.of("agent0001", "attack")), len(idx.of("agent0001", "battle"))), (1, 1))
        idx.poll()
        self.assertEqual(len(idx.of("agent0001", "attack")), 1)
        path.write_text("")                                             # replaced: forgotten
        idx.poll()
        self.assertEqual(idx.of("agent0001", "attack"), [])
        self.assertEqual(R.LogIndex(self.dir / "nowhere").of("x", "attack"), [])


class RefereeBoardTests(RefereeBase):
    def referee(self, events, extra=()):
        posts = [bounty_post(t=self.BOUNTY_T, expires=1000 + 6 * 3600),
                 {"id": 31, "t": 1500.0, "agent": "agent0001", "kind": "CLAIM", "text": "x", "data": self.claim_data()}, *extra]
        board = FakeBoard(posts)
        return R.Referee(board, self.index(events)), board

    def claim_data(self):
        return {"bounty_id": 21, "report": 77, "at": "1:1:5", "time": 1500, "claimant_id": 1, "claimant_at": "1:3:8", "loot": 9000}

    def test_a_verdict_is_posted_once_with_everything_the_poster_needs(self):
        ref, board = self.referee(self.good)
        ref.step()
        ref.step()
        (v,) = board.sent
        self.assertEqual(v["kind"], "VERDICT")
        d = v["data"]
        self.assertEqual((d["bounty_id"], d["claim_id"], d["ok"], d["claimant"], d["claimant_id"], d["claimant_at"]),
                         (21, 31, True, "agent0001", 1, "1:3:8"))
        self.assertLessEqual(len(json.dumps(d)), 600)
        self.assertLessEqual(len(v["text"]), 200)

    def test_one_claim_per_bounty_the_second_loses(self):
        second = {"id": 32, "t": 1501.0, "agent": "agent0002", "kind": "CLAIM", "text": "x", "data": dict(self.claim_data(), claimant_id=4)}
        ref, board = self.referee(self.good, extra=[second])
        with (self.dir / "agent0002.jsonl").open("w") as f:
            for e in self.good:
                f.write(json.dumps(dict(e, agent="agent0002")) + "\n")
        ref.step()
        self.assertEqual([(p["data"]["claim_id"], p["data"]["ok"]) for p in board.sent], [(31, True), (32, False)])
        self.assertIn("already claimed", board.sent[1]["data"]["why"])

    def test_a_restarted_referee_does_not_judge_again_and_remembers_who_was_paid(self):
        ref, board = self.referee(self.good)
        ref.step()
        again = R.Referee(board, self.index(self.good))
        again.step()
        self.assertEqual(len(board.sent), 1)
        self.assertEqual(again.paid, {21: 31})

    def test_a_board_that_is_away_costs_nothing_and_the_claim_is_judged_later(self):
        ref, board = self.referee(self.good)
        board.post = lambda *a, **k: None
        self.assertEqual(ref.step(), [])
        self.assertEqual(list(ref.pending), [31])

    def test_garbage_posts_are_ignored(self):
        ref, board = self.referee(self.good, extra=[{"id": 50, "t": 1.0, "agent": "x", "kind": "CLAIM", "text": "x", "data": {"bounty_id": "z"}},
                                                    {"id": 51, "t": 1.0, "agent": "x", "kind": "BOUNTY", "text": "x", "data": {}}])
        ref.step()
        self.assertEqual(len(board.sent), 1)


class BountyStory(Story):
    def run_until(self, agents, cond, ticks=40, dt=100.0):
        self.play(agents, ticks, dt=dt, until=cond)
        self.assertTrue(cond(), "the story did not get there")

    def set_up(self, with_cargo=True):
        p, gp = self.poster(ships={"small_cargo": 5} if with_cargo else {})
        self.rob(p, gp)
        gp.others.append({"user_id": 1, "username": "Hunter", "coord": (1, 3, 8), "score": 0,
                          "resources": {"metal": 0, "crystal": 0, "deuterium": 0}, "ships": {}, "defense": {}})
        h, gh = self.hunter()
        gp.peers[gh.home] = gh
        gh.peers[gp.home] = gp
        self.store.put("agent0001", {"email": "h@x", "password": "p", "token": "t", "persona": "raider", "user_id": 1})
        return p, gp, h, gh

    def referee(self, h):
        return R.Referee(BoardClient(self.url, R.NAME), R.LogIndex(h.log_path.parent))

    def claim_it(self, h, gh):
        self.run_until([(h, gh)], lambda: bool(self.events(h, "bounty_claim")))
        return self.events(h, "bounty_claim")[0]

    def test_the_whole_story_raid_claim_verdict_payment(self):
        p, gp, h, gh = self.set_up()
        (bounty,) = self.posts("BOUNTY")
        claim_event = self.claim_it(h, gh)
        # the hunter went for the wanted raider, won, and claimed: the claim names the report and the place
        (claim,) = self.posts("CLAIM")
        d = claim["data"]
        self.assertEqual((d["bounty_id"], d["at"], d["claimant_id"], d["claimant_at"]), (bounty["id"], "1:1:5", 1, "1:3:8"))
        self.assertGreater(d["loot"], 0)
        self.assertEqual(claim_event["reward"], {"metal": 4000})
        # the referee checks the logs and confirms
        self.referee(h).step()
        (verdict,) = self.posts("VERDICT")
        self.assertTrue(verdict["data"]["ok"], verdict["data"]["why"])
        self.assertEqual((verdict["agent"], verdict["data"]["claim_id"], verdict["data"]["claimant"]), ("referee", claim["id"], "agent0001"))
        # nothing has been paid yet: the reward comes AFTER the verdict
        self.assertEqual(gp.sent_to_peers, [])
        gp.advance(1500)                                              # (the poster stayed on guard for a quarter of an hour after the raid)
        p.memory.social["read_at"] = 0.0
        p.tick()
        (sent,) = gp.sent_to_peers
        self.assertEqual((sent["to"], sent["resources"]), ((1, 3, 8), {"metal": 4000}))
        self.assertLessEqual(sent["resources"]["metal"], 0.10 * self.P_STOCK["metal"])
        (paid,) = self.events(p, "bounty_paid")
        self.assertEqual((paid["side"], paid["to"], paid["resources"], paid["bounty"]), ("poster", "agent0001", {"metal": 4000}, str(bounty["id"])))
        self.assertEqual(self.events(p, "bounty_verdict")[0]["role"], "poster")
        self.assertTrue(p.memory.social["bounty_out"][str(bounty["id"])]["paid"])
        # it arrives; the claimant sees it and trusts the poster more
        before = gh.res["metal"]
        self.run_until([(h, gh), (p, gp)], lambda: bool([e for e in self.events(h, "bounty_paid")]), ticks=10, dt=100)
        (got,) = self.events(h, "bounty_paid")
        self.assertEqual((got["side"], got["poster"], got["resources"]), ("claimant", "agent0003", {"metal": 4000}))
        self.assertGreaterEqual(gh.res["metal"], before + 4000)
        self.assertEqual(h.memory.social["rep"]["agent0003"]["ok"], 1)
        self.assertEqual(h.memory.social["owed"], {})
        verdicts = self.events(h, "bounty_verdict")
        self.assertEqual((verdicts[0]["role"], verdicts[0]["ok"]), ("claimant", True))
        # one payment only, even if time goes on
        self.play([(h, gh), (p, gp)], 15)
        self.assertEqual(len(gp.sent_to_peers), 1)

    def test_a_poster_that_does_not_pay_is_shunned(self):
        p, gp, h, gh = self.set_up()
        self.claim_it(h, gh)
        self.referee(h).step()
        self.play([(h, gh)], 3)                                      # the poster never wakes up again
        self.assertEqual(len(h.memory.social["owed"]), 1)
        gh.advance(B.PAY_WAIT + 100)
        h.memory.social["pay_mail_at"] = 0.0
        h.tick()
        (e,) = self.events(h, "bounty_unpaid")
        self.assertEqual((e["side"], e["poster"]), ("claimant", "agent0003"))
        self.assertEqual(h.memory.social["owed"], {})
        self.assertTrue(S.is_shunned(h.memory.social["rep"], "agent0003", gh.now))
        # its bounties no longer count for this hunter
        B.remember_bounties(h.memory.social, [bounty_post(id=99, agent="agent0003", t=time.time())], "agent0001", time.time(), gh.now)
        self.assertEqual(h.memory.social["bounties"].get("99"), None)
        self.assertIsNone(h._bounty_focus())

    def test_a_poster_without_cargo_ships_builds_a_few_to_pay(self):
        p, gp, h, gh = self.set_up(with_cargo=False)
        gp.levels["shipyard"] = 1
        self.claim_it(h, gh)
        self.referee(h).step()
        for _ in range(8):
            gp.advance(200)
            p.memory.social["read_at"] = 0.0
            p.tick()
            if gp.sent_to_peers:
                break
        self.assertTrue([e for e in self.events(p, "units") if e["what"] == "small_cargo"])
        self.assertTrue(gp.sent_to_peers)

    def test_a_rejected_claim_is_never_paid(self):
        p, gp, h, gh = self.set_up()
        bad = self.board.post("agent0001", "CLAIM", "I did it, honest", {"bounty_id": self.posts("BOUNTY")[0]["id"], "report": 5, "at": "1:1:5",
                                                                           "time": int(time.time()), "claimant_id": 1, "claimant_at": "1:3:8", "loot": 9999})
        h.memory.social["claims"][str(bad["data"]["bounty_id"])] = {"bounty": bad["data"]["bounty_id"], "claim": bad["id"], "poster": "agent0003",
                                                                     "poster_id": 3, "poster_at": "1:5:2", "reward": {"metal": 4000}, "at": 0.0}
        ref = self.referee(h)
        ref.step()
        self.assertEqual(self.posts("VERDICT"), [])                      # young claim, no evidence yet: it waits
        ref.clock = lambda: time.time() + 3600
        ref.index.poll()
        verdict_now = R.judge(ref.pending[bad["id"]], ref.bounties[bad["data"]["bounty_id"]], ref.index, {}, time.time() + 3600)
        self.assertFalse(verdict_now[0])
        self.board.post(R.NAME, "VERDICT", "REJECTED", {"bounty_id": bad["data"]["bounty_id"], "claim_id": bad["id"], "ok": False, "why": "no log",
                                                        "claimant": "agent0001", "claimant_id": 1, "claimant_at": "1:3:8"})
        h.memory.social["read_at"] = 0.0
        p.memory.social["read_at"] = 0.0
        self.play([(h, gh), (p, gp)], 4)
        self.assertEqual(gp.sent_to_peers, [])
        self.assertEqual(h.memory.social["owed"], {})
        self.assertFalse(self.events(h, "bounty_verdict")[0]["ok"])
        self.assertFalse(p.memory.social["bounty_out"][str(bad["data"]["bounty_id"])]["paid"])

    def test_verdicts_from_anybody_but_the_referee_are_ignored(self):
        p, gp, h, gh = self.set_up()
        bid = self.posts("BOUNTY")[0]["id"]
        self.board.post("agent0001", "VERDICT", "pay me", {"bounty_id": bid, "claim_id": 1, "ok": True, "why": "trust me",
                                                           "claimant": "agent0001", "claimant_id": 1, "claimant_at": "1:3:8"})
        gp.advance(100)
        p.memory.social["read_at"] = 0.0
        p.tick()
        p.tick()
        self.assertEqual(gp.sent_to_peers, [])
        self.assertEqual(p.memory.social["pay"], {})

    def test_the_poster_only_pays_a_claimant_that_really_lives_there(self):
        p, gp, h, gh = self.set_up()
        bid = self.posts("BOUNTY")[0]["id"]
        self.board.post(R.NAME, "VERDICT", "ok", {"bounty_id": bid, "claim_id": 5, "ok": True, "why": "x", "claimant": "agent0001",
                                                  "claimant_id": 1, "claimant_at": "1:9:9"})
        gp.advance(100)
        p.memory.social["read_at"] = 0.0
        for _ in range(3):
            p.tick()
        self.assertEqual(gp.sent_to_peers, [])
        gp.advance(B.PAY_WINDOW + 100)
        p.tick()
        (e,) = self.events(p, "bounty_unpaid")
        self.assertEqual(e["side"], "poster")

    def test_a_dead_board_or_a_bug_in_the_bounty_code_never_costs_a_round(self):
        p, gp, h, gh = self.set_up()
        h._revenge_join = lambda *a: 1 / 0
        h.memory.social["read_at"] = 0.0
        self.board.post("agent0003", "REVENGE", "again", {"raider_at": "1:1:5", "raider_id": 2, "victim_at": "1:5:2", "loot": 9000})
        h.tick()
        self.assertEqual(self.events(h, "board_error")[0]["type"], "ZeroDivisionError")
        self.assertEqual(self.events(h, "api_error"), [])


class WiringTests(unittest.TestCase):
    def test_referee_refuses_a_remote_board(self):
        import subprocess
        import sys
        from tests.test_social import AGENTS_DIR
        r = subprocess.run([sys.executable, "referee.py", "--board", "http://example.com:8765"], cwd=AGENTS_DIR, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, "referee.py", "--help"], cwd=AGENTS_DIR, capture_output=True, text=True, timeout=30)
        self.assertIn("--logs", r.stdout)


if __name__ == "__main__":
    unittest.main()
