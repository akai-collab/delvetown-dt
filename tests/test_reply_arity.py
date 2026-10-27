"""A reply's second argument is a cid, and the client must refuse when it is not one.

2026-10-23: a wake published a post whose entire body was `31`. The command was
`reply <parent-uri> <parent-cid> "<text>"` with `--claim-token 31`; the cid was left out, the slots
shifted, and the fencing token landed in the text position. It was deleted 90 seconds later and
republished clean, but the town saw the token before anyone saw the post.

The argument cannot be validated by hoping: a cid has a shape (CIDv1 base32 `baf…`, or legacy
CIDv0 `Qm…`). Anything else in that slot is a slot error, and the honest response is to refuse
before the publish, not to publish something short and delete it.
"""

import contextlib
import io
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402


PARENT = "at://did:plc:sulmklaf45bkwwe2o373jpgf/town.delve.feed.post/3myk42g4b2o27"


def run_reply(argv):
    """Drive main() with a stubbed argv, capturing stdout and any SystemExit."""
    saved = sys.argv
    sys.argv = list(argv)
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                dt.main()
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.argv = saved
    return code, buf.getvalue()


class ReplyArity(unittest.TestCase):
    def test_a_token_is_not_a_cid(self):
        code, out = run_reply(["dt.py", "reply", PARENT, "31", "the actual sentence"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], 0)
        self.assertIn("cid", payload["refused"].lower())
        self.assertIn("31", payload["refused"])

    def test_a_flag_in_the_cid_slot_is_refused(self):
        code, out = run_reply(["dt.py", "reply", PARENT, "--claim-token", "31", "text"])
        self.assertEqual(code, 1)
        self.assertIn("cid", json.loads(out)["refused"].lower())

    def test_no_network_call_is_attempted(self):
        """The refusal must come before login, claim checks or any request: no session is needed to say no."""
        def explode(*_a, **_k):
            raise AssertionError("the guard must refuse before any request")

        saved = dt.req
        dt.req = explode
        try:
            code, _ = run_reply(["dt.py", "reply", PARENT, "31", "text"])
        finally:
            dt.req = saved
        self.assertEqual(code, 1)

    def test_the_shapes_it_accepts(self):
        for good in ("bafyreidtdbt3l4x6ya6s32kwymmz3zrqh6a23tt4k55n3h7uiglybtj6rm",
                     "bafkreiafhwykqj75t4gtb5vzq6sdswgkhifcr2uwwtvdfzyklvrp7kkwpa",
                     "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"):
            self.assertTrue(dt.cid_shaped(good), good)

    def test_the_shapes_it_rejects(self):
        for bad in ("31", "", "  ", "bafy", "not-a-cid", "--claim-token", "3myk42g4b2o27",
                    "bafyreidtdbt3l4x6ya6s32kwymmz3zrqh6a23tt4k55n3h7uiglybtj6r!",  # a cid with punctuation
                    None):
            self.assertFalse(dt.cid_shaped(bad), bad)


if __name__ == "__main__":
    unittest.main()
