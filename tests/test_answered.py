"""What has been answered: the publish log is the record. dt.py's reply path reads it (and the repo) before
it sends, the monitor drops what it closes, and an ask answered twice wakes the account once.

The cases are the 2026-10-23 failures, each as the smallest input that produced it, plus the invariants
`dt.py audit` checks. Offline: the repo read is a dict handed in, the PDS unreachable.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
DT = os.path.join(ROOT, "dt.py")

ME = "did:plc:pupeplyyuw54mc6ngxavghv2"
SELENE = "did:plc:seleneseleneselenesele"
ASK = "at://%s/town.delve.feed.post/3myk42g4b2o27" % SELENE
OTHER = "at://%s/town.delve.feed.post/3myk42g4b2o28" % SELENE
CID = "bafyreih7fp6wxtpq2m32ndbuiqp24vv7helurjxi4vptepchmt45ehgxka"


def mine(rkey):
    return "at://%s/town.delve.feed.post/%s" % (ME, rkey)


def load(path, name, env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def item(uri, at="2026-10-23T11:00:00.000Z"):
    return {"reason": "reply", "author": "selene.delve.town", "author_did": SELENE, "uri": uri,
            "text": "a question", "indexed_at": at}


def reply(rkey, parent=ASK, t=0, **kw):
    return dict({"ts": iso(t), "kind": "reply", "uri": mine(rkey), "rkey": rkey, "parent_uri": parent}, **kw)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "publish-log.jsonl")
        self.dt = load(DT, "dt_answered", {"DT_PUBLISH_LOG": self.log, "DT_DRY_RUN": "1",
                                            "DT_CLAIMS_FILE": os.path.join(self.tmp, "claims.json"),
                                            "AT_MCP_WAKE_STATE": os.path.join(self.tmp, "wake.json")})
        self.dt.DRY_RUN = False          # the log is written; nothing below reaches the network
        self.m = load(MONITOR, "mon_answered", {})
        self.m.PUBLISH_LOG = self.log
        self.m.SETTLE_S, self.m.MAX_WAIT_S = 600, 2700

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_log(self, entries):
        with open(self.log, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def lines(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def tick(self, items, state, now, **kw):
        kw.setdefault("answered", self.m.answered_asks(self.log))
        return self.m.wake_output(items, state, now, quiet=set(), replies=[], **kw)


class TheLog(Fixture):
    """One file says what is answered, and both programs read it the same way."""

    def test_a_reply_answers_its_parent_and_a_decline_closes_its_subject(self):
        self.write_log([reply("r1"), {"ts": iso(1), "kind": "decline", "subject": OTHER, "why": "spam"}])
        self.assertEqual(self.dt.ask_state(ASK)[0], "reply")
        self.assertEqual(self.dt.ask_state(OTHER)[0], "decline")
        self.assertEqual(self.m.answered_asks(self.log), {ASK: "reply", OTHER: "decline"})

    def test_a_deleted_reply_answers_nothing(self):
        """The 14:06Z repair: published, deleted 49 seconds later, republished. The delete reopens the ask
        (so the republish is not refused), and the republish closes it again (so it is not a double)."""
        self.write_log([reply("slip")])
        self.write_log([{"ts": iso(49), "kind": "delete", "uri": mine("slip")}])
        self.assertIsNone(self.dt.ask_state(ASK)[0])
        self.assertEqual(self.m.answered_asks(self.log), {})
        self.write_log([reply("real", t=60)])
        self.assertEqual(self.dt.ask_state(ASK)[0], "reply")
        self.assertEqual(self.dt.double_answers(), [])
        self.assertEqual(self.m.double_answers(self.log), [])

    def test_lines_from_before_parent_uri_match_by_rkey(self):
        """Lines before 2026-10-15 kept only the parent's rkey: migration is reading them, not rewriting."""
        self.write_log([{"ts": iso(0), "kind": "reply", "parent": "3myk42g4b2o27"}])
        self.assertEqual(self.dt.ask_state(ASK)[0], "reply")
        out, _ = self.tick([item(ASK)], {}, 10 ** 4)
        self.assertEqual(out, "wake=none")

    def test_both_programs_find_the_same_doubles(self):
        self.write_log([reply("a"), reply("b", t=1), reply("c", t=2, again=True), reply("d", OTHER, t=3)])
        self.assertEqual(self.dt.double_answers(), self.m.double_answers(self.log))
        self.assertEqual([(d["first"], d["second"]) for d in self.dt.double_answers()], [(mine("a"), mine("b"))])

    def test_bookkeeping_lines_are_not_writes(self):
        now = datetime.now(timezone.utc)
        t = now.timestamp() - 30
        self.write_log([{"ts": iso(t), "kind": "decline", "subject": ASK}]
                       + [reply("x%d" % i, t=t, adopted=True) for i in range(20)])
        self.assertEqual(self.dt.write_pressure(now=now), (0, 0))
        self.assertIsNone(self.m.held_reason(now.timestamp(), os.path.join(self.tmp, "none"), self.log))
        self.assertEqual(self.m.own_replies(now.timestamp(), self.log), [])


class Reply(Fixture):
    """dt.py reply: one answer per ask, whichever writer gets there first, claimed or not."""

    def test_an_answered_ask_is_refused_and_again_is_the_way_past(self):
        self.write_log([reply("r1", mode="session")])
        refused = self.dt.answered_refusal(ASK, repo={})
        self.assertIn("already answered", refused)
        self.assertIn("session", refused)
        self.assertIsNone(self.dt.answered_refusal(ASK, again=True, repo={}))

    def test_a_declined_ask_is_refused(self):
        self.write_log([{"ts": iso(0), "kind": "decline", "subject": ASK, "why": "a loop"}])
        refused = self.dt.answered_refusal(ASK, repo={})
        self.assertIn("declined", refused)
        self.assertIn("a loop", refused)

    def test_a_reply_the_log_lacks_is_found_in_the_repo_and_adopted(self):
        """A run died between createRecord and the log line, or a session answered through another client.
        The repo holds the reply; the next attempt logs it instead of sending a second."""
        refused = self.dt.answered_refusal(ASK, repo={ASK: mine("elsewhere")})
        self.assertIn("already holds a reply", refused)
        last = self.lines()[-1]
        self.assertEqual((last["kind"], last["parent_uri"], last["uri"], last["adopted"]),
                         ("reply", ASK, mine("elsewhere"), True))
        self.assertIn("already answered", self.dt.answered_refusal(ASK, repo={}), "now a lookup")

    def test_a_second_reply_under_our_own_post_is_not_a_double(self):
        """A post of this account's own is a thread, not an ask. The picture goes up, then the street list
        replies under it, then the correction; all three are one post being built, not one ask answered
        twice. Before this, the follow-up was refused and needed --again to be written at all."""
        self.write_log([reply("r1", parent=mine("3myownpost"))])
        self.dt.DID = ME          # the fixture's account is skillissue; the log's "us" is ME
        self.assertIsNone(self.dt.answered_refusal(mine("3myownpost"), repo={}))

    def test_the_boundary_is_the_parents_repo_not_a_substring(self):
        """A post of ours is one in our repo, whoever quoted it; a post elsewhere that merely names our DID
        in its rkey slot or path is not."""
        self.dt.DID = ME
        self.write_log([reply("r1", parent="at://%s/town.delve.feed.post/%s" % (SELENE, ME.split(":")[-1]))])
        self.assertIn("already answered", self.dt.answered_refusal(
            "at://%s/town.delve.feed.post/%s" % (SELENE, ME.split(":")[-1]), repo={}))

    def test_follow_ups_under_our_own_post_are_not_doubles_anywhere(self):
        """Five of the twelve same-parent pairs in the live log were the picture, the street list and the
        correction under one post of ours. audit and the gate must agree with reply that this is a thread."""
        own = mine("3myownpost")
        self.write_log([reply("list", parent=own), reply("fix", parent=own, t=1), reply("a"), reply("b", t=2)])
        self.assertEqual([d["ask"] for d in self.dt.double_answers()], [ASK])
        self.assertEqual([d["ask"] for d in self.m.double_answers(self.log)], [ASK])

    def test_an_ask_from_someone_else_is_still_refused(self):
        self.write_log([reply("r1")])
        self.assertIn("already answered", self.dt.answered_refusal(ASK, repo={}))

    def test_an_unanswered_ask_goes_through(self):
        self.write_log([reply("r1", OTHER)])
        self.assertIsNone(self.dt.answered_refusal(ASK, repo={}))

    def run_dt(self, *args):
        env = dict(os.environ, DT_PUBLISH_LOG=self.log, DT_DRY_RUN="1", HOME=self.tmp,
                   DT_CLAIMS_FILE=os.path.join(self.tmp, "claims.json"),
                   AT_MCP_WAKE_STATE=os.path.join(self.tmp, "wake.json"),
                   DT_PDS="http://127.0.0.1:9", DT_APPVIEW="http://127.0.0.1:9")
        return subprocess.run([sys.executable, DT, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_the_second_answer_is_refused_from_the_command_line(self):
        """The 14:30Z case, whatever slot slipped: the real answer is logged, and a second reply to the
        same post — `31`, a fragment, a repeat — is refused before it is sent."""
        self.write_log([reply("handoff")])
        r = self.run_dt("reply", ASK, CID, "31")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('"refused": "already answered', r.stdout)
        r = self.run_dt("reply", ASK, CID, "a correction, on purpose", "--again")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_decline_wants_a_whole_uri(self):
        r = self.run_dt("decline", "3myk42g4b2o27")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bare rkey", r.stdout)


class Abstain(Fixture):
    """2026-10-24: four repo-lane items a wake may not answer were re-offered, because a run that looked and
    correctly wrote nothing closed nothing. `decline` would stop the churn but also refuse the session that
    should answer them. `abstain` closes an ask for the gate and leaves it answerable."""

    def abstain(self, subject=ASK, t=5, **kw):
        return dict({"ts": iso(t), "kind": "abstain", "subject": subject}, **kw)

    def test_an_abstained_ask_is_closed_for_the_gate_and_open_to_a_session(self):
        self.write_log([self.abstain(why="repo lane")])
        self.assertEqual(self.m.answered_asks(self.log), {ASK: "abstain"})
        self.assertEqual(self.dt.ask_state(ASK)[0], "abstain")
        self.assertIsNone(self.dt.answered_refusal(ASK, repo={}), "a session may still answer it")
        out, st = self.tick([item(ASK)], {}, 0)
        self.assertEqual((out, st["pending"]), ("wake=none", {}))

    def test_an_abstain_never_reopens_an_answer_or_a_decline(self):
        self.write_log([reply("r1"), self.abstain(), {"ts": iso(6), "kind": "decline", "subject": OTHER},
                        self.abstain(OTHER, t=7)])
        self.assertEqual(self.m.answered_asks(self.log), {ASK: "reply", OTHER: "decline"})
        self.assertEqual({k: v[0] for k, v in self.dt.ask_states().items()}, {ASK: "reply", OTHER: "decline"})

    def test_abstain_on_purpose_turns_a_decline_back(self):
        """Today's repo-lane declines refuse the session; `dt.py abstain` on them writes replaces_decline."""
        self.write_log([{"ts": iso(1), "kind": "decline", "subject": ASK}, self.abstain(replaces_decline=True)])
        self.assertEqual(self.m.answered_asks(self.log), {ASK: "abstain"})
        self.assertIsNone(self.dt.answered_refusal(ASK, repo={}))

    def test_a_later_reply_closes_it_for_good(self):
        self.write_log([self.abstain(), reply("r1", t=9)])
        self.assertEqual(self.dt.ask_state(ASK)[0], "reply")
        self.assertIn("already answered", self.dt.answered_refusal(ASK, repo={}))

    def test_an_abstained_batch_is_not_reoffered_and_is_counted_as_such(self):
        _, st = self.tick([item(ASK), item(OTHER)], {}, 0)
        first, st = self.tick([item(ASK), item(OTHER)], st, 600)
        self.write_log([self.abstain(t=700), self.abstain(OTHER, t=701)])
        out, st = self.tick([], st, 600 + self.m.REOFFER_AFTER_S)
        self.assertEqual(out, first, "looked and abstained is not a dead run")
        third = "at://%s/town.delve.feed.post/3myk42g4b2o29" % SELENE
        _, st = self.tick([item(third)], st, 4000)
        out, st = self.tick([item(third)], st, 4600)
        self.assertIn("previous batch of 2: 0 answered, 0 declined, 2 abstained, 0 untouched", out)

    def run_dt(self, *args):
        env = dict(os.environ, DT_PUBLISH_LOG=self.log, DT_DRY_RUN="0", HOME=self.tmp, HERMES_REAL_HOME=self.tmp,
                   DT_PDS="http://127.0.0.1:9", DT_APPVIEW="http://127.0.0.1:9")
        return subprocess.run([sys.executable, DT, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_the_command_downgrades_a_decline_and_refuses_a_bare_rkey(self):
        self.write_log([{"ts": iso(1), "kind": "decline", "subject": ASK}])
        r = self.run_dt("abstain", ASK, "repo lane: the session answers these")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["replaced_decline"])
        self.assertEqual(self.lines()[-1]["kind"], "abstain")
        self.assertIsNone(self.dt.answered_refusal(ASK, repo={}))
        self.assertEqual(self.run_dt("abstain", "3myk42g4b2o27").returncode, 1)

    def test_a_decline_may_harden_an_abstention(self):
        self.write_log([self.abstain()])
        r = self.run_dt("decline", ASK, "nobody should answer this after all")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.m.answered_asks(self.log), {ASK: "decline"})
        self.assertIn("declined", self.dt.answered_refusal(ASK, repo={}))


class Unanswered(Fixture):
    """`unanswered` gives two counts, never one sum: nobody has looked, and looked-and-left-for-a-session."""

    def test_untouched_and_abstained_are_counted_apart(self):
        import contextlib, io
        self.write_log([reply("r1", OTHER), {"ts": iso(1), "kind": "abstain", "subject": ASK}])
        third = "at://%s/town.delve.feed.post/3myk42g4b2o29" % SELENE

        def note(uri):
            return {"uri": uri, "reason": "reply", "indexedAt": "2026-10-24T09:00:00Z",
                    "author": {"handle": "selene.delve.town", "did": SELENE}, "record": {"text": "a question"}}

        def fake(method, url, body=None, token=None, params=None, headers_out=None):
            if url.endswith("getServiceAuth"):
                return 200, {"token": "t"}
            return 200, {"notifications": [note(ASK), note(OTHER), note(third)]}

        saved = (self.dt.req, self.dt.session, sys.argv)
        self.dt.req, sys.argv = fake, ["dt.py", "unanswered", "10"]
        self.dt.session = lambda *a, **k: {"accessJwt": "t", "did": self.dt.DID}   # no vault offline
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.dt.main()
        finally:
            self.dt.req, self.dt.session, sys.argv = saved
        out = json.loads(buf.getvalue())
        self.assertEqual((out["untouched"], out["abstained"]), (1, 1))
        self.assertNotIn("unanswered", out, "no single sum to misread as debt")
        self.assertEqual([a["uri"] for a in out["asks"]], [third, ASK], "untouched first")


class Gate(Fixture):
    """The monitor: an ask the log closes never wakes, whoever closed it; a double wakes once."""

    def test_a_session_answer_takes_the_ask_out_of_the_queue(self):
        """The session answered at 11:12Z while the gate held the ask; the gate woke a run at 11:57Z that
        answered it again."""
        _, st = self.tick([item(ASK)], {}, 0, held="rate-limited")
        self.write_log([reply("r1", t=100, mode="session")])
        out, st = self.tick([item(ASK)], st, 2700)
        self.assertEqual(out, "wake=none")
        self.assertEqual(st["pending"], {})
        # and the ask, still in the notification list, is not re-queued a tick later: the failure the
        # removed mark_handled existed for (skillissue, 11:12 answered in a session, 11:57 again)
        out, st = self.tick([item(ASK)], st, 3000)
        self.assertEqual((out, st["pending"]), ("wake=none", {}))

    def test_an_answered_ask_stays_answered_when_fired_forgets_it(self):
        """`fired` expires and state files get lost; the log does neither."""
        _, st = self.tick([item(ASK)], {}, 0)
        fired, st = self.tick([item(ASK)], st, 600)
        self.assertTrue(fired.startswith("wake=1"))
        self.write_log([reply("r1", t=700)])
        st["fired"] = {}
        out, st = self.tick([item(ASK)], st, 10 ** 5)
        self.assertEqual(out, fired, "no new output, so no wake")

    def test_a_failed_run_that_answered_everything_is_not_sent_again(self):
        _, st = self.tick([item(ASK)], {}, 0)
        fired, st = self.tick([item(ASK)], st, 600)
        self.write_log([reply("r1", t=650)])
        out, _ = self.tick([item(ASK)], dict(st), 2000, failed_at=700)
        self.assertEqual(out, fired, "the run wrote its answer before it died: no retry")
        out, _ = self.tick([item(ASK)], dict(st), 2000, failed_at=700, answered=set())
        self.assertIn("retry 1 of", out, "and without the answer the retry still happens")

    def test_a_double_answer_wakes_once(self):
        """The routine that was missing: nobody's check would have found the 14:30Z `31` until someone
        opened the thread."""
        self.write_log([reply("handoff"), reply("slip", t=1)])
        doubles = self.m.double_answers(self.log)
        out, st = self.tick([], {}, 100, doubles=doubles)
        self.assertTrue(out.startswith("wake=double n=1 id="), out)
        self.assertIn(mine("handoff"), out)
        self.assertIn(mine("slip"), out)
        again, st = self.tick([], st, 400, doubles=doubles)
        self.assertEqual(again, out, "reported once")

    def test_a_double_is_reported_even_while_the_town_does_not_answer(self):
        self.write_log([reply("a"), reply("b", t=1)])
        out, _ = self.tick(None, {}, 100, doubles=self.m.double_answers(self.log))
        self.assertTrue(out.startswith("wake=double"), out)

    def test_main_wakes_only_on_a_recent_double(self):
        """The first tick after a deploy must not wake the account to read the log's whole past."""
        self.write_log([reply("old1", t=0), reply("old2", t=1)])
        recent = [d for d in self.m.double_answers(self.log)
                  if 10 ** 9 - self.m._epoch(d.get("at")) < self.m.DOUBLE_WINDOW_S]
        self.assertEqual(recent, [])

    def test_an_unreadable_log_is_the_old_behaviour(self):
        self.assertEqual(self.m.answered_asks(os.path.join(self.tmp, "absent.jsonl")), {})
        self.assertEqual(self.m.double_answers(os.path.join(self.tmp, "absent.jsonl")), [])


class Audit(Fixture):
    """`dt.py audit` checks the invariants instead of asserting them."""

    def run_dt(self, *args):
        env = dict(os.environ, DT_PUBLISH_LOG=self.log, DT_DRY_RUN="1", HOME=self.tmp,
                   DT_PDS="http://127.0.0.1:9", DT_APPVIEW="http://127.0.0.1:9")
        return subprocess.run([sys.executable, DT, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_a_clean_log_passes(self):
        self.write_log([reply("a"), reply("b", t=1, again=True), reply("c", OTHER, t=2)])
        r = self.run_dt("audit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_a_double_answer_fails_and_a_deleted_one_does_not(self):
        self.write_log([reply("a"), reply("b", t=1)])
        r = self.run_dt("audit")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["doubles"][0]["second"], mine("b"))
        self.write_log([{"ts": iso(2), "kind": "delete", "uri": mine("b")}])
        r = self.run_dt("audit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
