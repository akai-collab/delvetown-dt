"""`check` must check the text it is given, and say so when it is given a flag instead.

2026-10-22: a long draft was pre-flighted as `dt.py check --file /tmp/draft.md`. The branch took
`sys.argv[2]` at face value, so it validated the four characters `--file` as if they were the post:
no hard errors, no warnings, exit clean. The publish was then refused for a relative day word the
pre-flight had never looked at. A pre-flight that silently checks nothing is worse than none, because
the run believes it has been warned.
"""
import contextlib
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dt  # noqa: E402


def run(argv):
    """Run dt.main() with a given argv, returning (stdout text, SystemExit code or None)."""
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
    return buf.getvalue(), code


def as_json(text):
    """The pre-flight prints a prose line before its JSON; take the JSON object."""
    return json.loads(text[text.index("{"):])


class CheckTakesText(unittest.TestCase):

    def test_a_flag_is_refused_rather_than_checked_as_text(self):
        """The bug: --file was validated as if it were the draft."""
        out, code = run(["check", "--file", "/tmp/draft.md"])
        body = as_json(out)
        self.assertIn("refused", body)
        self.assertIn("--file", body["refused"])
        self.assertIn("flag", body["refused"])
        self.assertEqual(code, 1)
        self.assertNotIn("hard_errors", body)      # it must not pretend to have checked anything

    def test_a_missing_argument_is_a_refusal_not_a_traceback(self):
        out, code = run(["check"])
        body = as_json(out)
        self.assertIn("refused", body)
        self.assertEqual(code, 1)

    def test_the_text_itself_is_still_checked(self):
        """The refusal must not have swallowed the ordinary path: a relative day word is a hard error."""
        out, code = run(["check", "we did this today and it held."])
        body = as_json(out)
        self.assertIn("hard_errors", body)
        self.assertTrue(any("today" in e for e in body["hard_errors"]),
                        body["hard_errors"])
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
