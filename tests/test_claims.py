"""Claims: one run composes an item at a time, and a quiz's tally is owed once.

Two collisions happened before this existed (2026-10-22):

1. **Arrivals.** skillissue's gate wake opened 17:27 and its floor job opened 17:30 on schedule; the
   floor drew at :30 while the gate was still composing, and posted 48 seconds after it without
   re-checking. Two runs, one item, neither saw the other. A prompt-level "re-read before publishing"
   narrows that window but cannot close it: both can read before either writes, and the appview lags
   (the same day, `getPosts` returned 400 for a freshly published uri the author feed already held).
2. **Tally obligations.** A quiz published at 14:00 owes a tally hours later. A scheduled job may fire
   inside a wake's fifteen minutes, and a wake may decide the quiz is due — the obligation outlives any
   single run, so a short-lived lock cannot express it.

Both are the same file format: a claim keyed by the uri, with a live owner and an expiry, plus a durable
obligation row for anything that must be discharged once and can be checked by whichever run comes next.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dt  # noqa: E402


def as_json(text):
    return json.loads(text[text.index("{"):])


def run(argv):
    old = sys.argv
    sys.argv = ["dt.py"] + argv
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            dt.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv = old
    return as_json(buf.getvalue()), code


class ClaimsBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = dt.CLAIMS_FILE
        dt.CLAIMS_FILE = os.path.join(self.tmp, "claims-akai.json")
        self.uri = "at://did:plc:other/town.delve.feed.post/3mxclaim"

    def tearDown(self):
        dt.CLAIMS_FILE = self._old

    def seed(self, uri, pid, age_s=0.0, ttl_s=1200, kind="arrival"):
        data = dt.claims_data()
        data["claims"][uri] = {"pid": pid, "started": dt.now_iso(age_s), "ttl_s": ttl_s, "kind": kind}
        dt.save_claims(data)


class ArrivalClaims(ClaimsBase):

    def test_a_second_owner_is_refused_while_the_first_is_live(self):
        """The 17:27/17:30 race: the floor must not compose an item the gate already holds."""
        out, code = run(["claim", "take", self.uri])
        self.assertIn("claimed", out)
        # a different process now tries the same item
        me = os.getpid()
        self.seed(self.uri, me + 1, age_s=5)
        out2, code2 = run(["claim", "take", self.uri])
        self.assertIn("refused", out2)
        self.assertIn("held", out2["refused"])
        self.assertEqual(code2, 1)

    def test_a_dead_owner_is_taken_over(self):
        self.seed(self.uri, 2 ** 22 + 7, age_s=30)      # no such pid
        out, code = run(["claim", "take", self.uri])
        self.assertIn("claimed", out)
        self.assertIsNone(code)

    def test_an_expired_claim_is_taken_over_and_says_why(self):
        self.seed(self.uri, os.getpid() + 1, age_s=5000, ttl_s=60)
        out, code = run(["claim", "take", self.uri])
        self.assertIn("claimed", out)
        self.assertIn("expired", out.get("note", "").lower())

    def test_release_frees_it_for_the_next_run(self):
        run(["claim", "take", self.uri])
        out, _ = run(["claim", "release", self.uri])
        self.assertIn("released", out)
        self.assertEqual(dt.claims_data()["claims"], {})

    def test_a_claim_left_by_a_dead_run_can_be_released(self):
        """The holder is a process that has exited: releasing it must not need --force.

        2026-10-22: `claim take` in one invocation and `reply` in another are different pids, so every
        completed answer left a claim its own run could not release, and the item stayed claimed for
        the full twenty minutes.
        """
        self.seed(self.uri, 2 ** 22 + 7, age_s=10)
        out, code = run(["claim", "release", self.uri])
        self.assertIn("released", out)
        self.assertIsNone(code)
        self.assertEqual(dt.claims_data()["claims"], {})

    def test_list_shows_the_live_claims_and_their_age(self):
        run(["claim", "take", self.uri])
        out, _ = run(["claim"])
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["claims"][0]["uri"], self.uri)
        self.assertIn("age_s", out["claims"][0])

    def test_a_reply_is_refused_when_another_run_holds_the_parent(self):
        """The mechanical half: it does not depend on a prompt being obeyed."""
        self.seed(self.uri, os.getpid() + 1, age_s=5)
        note = dt.claim_conflict(self.uri)
        self.assertTrue(note)
        self.assertIn("holds a claim", note)

    def test_a_claim_on_a_different_item_does_not_block(self):
        self.seed(self.uri, os.getpid() + 1, age_s=5)
        self.assertIsNone(dt.claim_conflict("at://did:plc:other/town.delve.feed.post/3mxother"))


class TallyObligations(ClaimsBase):

    def test_a_quiz_owed_once_is_visible_to_the_next_run(self):
        out, _ = run(["claim", "owe", self.uri, "--due", "+12h", "--kind", "tally"])
        self.assertIn("owed", out)
        owed, _ = run(["claim", "owed"])
        self.assertEqual(owed["count"], 1)
        self.assertEqual(owed["obligations"][0]["kind"], "tally")
        self.assertTrue(owed["obligations"][0]["due"])

    def test_discharging_closes_it_and_a_second_discharge_is_refused(self):
        run(["claim", "owe", self.uri, "--due", "+12h", "--kind", "tally"])
        out, _ = run(["claim", "done", self.uri, "at://did:plc:zhcehhukgwhdt2mnokes2tui/town.delve.feed.post/3mxtally"])
        self.assertIn("done", out)
        again, code = run(["claim", "done", self.uri, "at://did:plc:zhcehhukgwhdt2mnokes2tui/town.delve.feed.post/3mxagain"])
        self.assertIn("refused", again)
        self.assertEqual(code, 1)
        self.assertEqual(run(["claim", "owed"])[0]["count"], 0)

    def test_an_obligation_survives_a_stale_claim(self):
        """A tally can be hours late: the row must not expire with the lock."""
        run(["claim", "owe", self.uri, "--due", "+1h", "--kind", "tally"])
        run(["claim", "take", self.uri, "--ttl", "1", "--kind", "tally"])
        data = dt.claims_data()
        data["claims"][self.uri]["started"] = dt.now_iso(9999)
        dt.save_claims(data)
        out, _ = run(["claim", "owed"])
        self.assertEqual(out["count"], 1)


if __name__ == "__main__":
    unittest.main()
