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

The first is a claim keyed by the uri, with a live owner and an expiry. The second lived here as a durable
obligation row until 2026-10-24, when what is owed moved to the Hermes board (`delvetown`): one place for
it instead of a JSON file nobody read.
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

    def test_a_fresh_claim_is_live_even_though_its_pid_is_dead(self):
        """The field bug of 2026-10-23: the recorded pid is the one-shot dt.py process.

        It exits a second after writing the claim, so pid-based liveness read every fresh claim as
        stale and let a second run take it over instantly — the guard manufacturing the collision.
        """
        self.seed(self.uri, 2 ** 22 + 7, age_s=30, ttl_s=1200)
        out, code = run(["claim", "take", self.uri])
        self.assertIn("refused", out)
        self.assertEqual(code, 1)

    def test_an_expired_claim_is_taken_over_and_says_why(self):
        self.seed(self.uri, os.getpid() + 1, age_s=5000, ttl_s=60)
        out, code = run(["claim", "take", self.uri])
        self.assertIn("claimed", out)
        self.assertIn("expired", out.get("note", "").lower())

    def test_release_frees_it_for_the_next_run(self):
        tok = run(["claim", "take", self.uri])[0]["token"]
        out, _ = run(["claim", "release", self.uri, "--claim-token", str(tok)])
        self.assertIn("released", out)
        self.assertEqual(dt.claims_data()["claims"], {})

    def test_release_needs_the_token_the_holder_was_given(self):
        """The holder is a process that has exited: releasing it must not need --force.

        2026-10-22: `claim take` in one invocation and `reply` in another are different pids, so every
        completed answer left a claim its own run could not release, and the item stayed claimed for
        the full twenty minutes.
        """
        out, _ = run(["claim", "take", self.uri])
        refused, code = run(["claim", "release", self.uri])
        self.assertIn("refused", refused)
        self.assertEqual(code, 1)
        ok, _ = run(["claim", "release", self.uri, "--claim-token", str(out["token"])])
        self.assertIn("released", ok)
        self.assertEqual(dt.claims_data()["claims"], {})

    def test_force_releases_a_claim_whose_run_died(self):
        self.seed(self.uri, 2 ** 22 + 7, age_s=10)
        out, _ = run(["claim", "release", self.uri, "--force"])
        self.assertIn("released", out)

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


class Obligations(ClaimsBase):

    def test_owing_moved_to_the_board(self):
        """claim owe/owed/done were retired on 2026-10-24: a stale prompt is refused and pointed at the board."""
        out, code = run(["claim", "owe", self.uri, "--due", "+12h"])
        self.assertEqual(code, 1)
        self.assertIn("kanban", out["hint"])


if __name__ == "__main__":
    unittest.main()

class FencingTokens(ClaimsBase):
    """A claim that has been taken over must make the old holder's write impossible, not discouraged.

    pid+ttl alone leaves a suspended run able to publish after its claim was taken over: nothing
    invalidates the claim it carries. A monotonic token does, because `reply` refuses a token older
    than the one last issued for that item, even if the claim has since been released.
    """

    def test_taking_a_claim_returns_a_token(self):
        out, _ = run(["claim", "take", self.uri])
        self.assertEqual(out["token"], 1)

    def test_a_takeover_issues_a_higher_token(self):
        first, _ = run(["claim", "take", self.uri])
        self.seed(self.uri, os.getpid() + 1, age_s=5000, ttl_s=60)   # expired holder
        second, _ = run(["claim", "take", self.uri])
        self.assertGreater(second["token"], first["token"])

    def test_the_old_holder_cannot_publish_after_a_takeover(self):
        first, _ = run(["claim", "take", self.uri])
        self.seed(self.uri, os.getpid() + 1, age_s=5000, ttl_s=60)
        run(["claim", "take", self.uri])
        note = dt.claim_gate(self.uri, first["token"])
        self.assertTrue(note)
        self.assertIn("holds a claim", note.lower())

    def test_the_old_holder_still_cannot_publish_after_the_new_holder_releases(self):
        """The gap a claim alone leaves: once released, nothing is holding the item any more."""
        first, _ = run(["claim", "take", self.uri])
        self.seed(self.uri, os.getpid() + 1, age_s=5000, ttl_s=60)
        run(["claim", "take", self.uri])
        run(["claim", "release", self.uri, "--force"])
        note = dt.claim_gate(self.uri, first["token"])
        self.assertTrue(note)

    def test_the_current_holder_passes_the_gate(self):
        out, _ = run(["claim", "take", self.uri])
        self.assertIsNone(dt.claim_gate(self.uri, out["token"]))

    def test_a_reply_to_a_never_claimed_item_needs_no_token(self):
        self.assertIsNone(dt.claim_gate("at://did:plc:other/town.delve.feed.post/3mxfresh", None))

    def test_a_fenced_item_needs_a_token(self):
        tok = run(["claim", "take", self.uri])[0]["token"]
        run(["claim", "release", self.uri, "--claim-token", str(tok)])
        note = dt.claim_gate(self.uri, None)
        self.assertTrue(note)
        self.assertIn("token", note.lower())
