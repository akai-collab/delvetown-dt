"""A post that is a quiz writes its own tally task.

The tally duty used to be a claim row (retired 2026-10-23), then a sentence in the skill telling the publishing
session to remember. `post --quiz "<what to count>"` is the version that cannot be forgotten: the publish creates
an unassigned task on the delvetown board, which nothing dispatches, which the digest lists and marks stale.

These tests never touch the town: they exercise the helper and the flag, with a fake hermes binary.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")

FAKE = """#!/bin/sh
# fake hermes: record the call, print what the CLI prints on success
echo "kanban $@" >> "$FAKE_CALLS"
echo "Created t_faketally  (triage, assignee=-)"
"""


class QuizFlag(unittest.TestCase):
    def setUp(self):
        self.src = open(DT, encoding="utf-8").read()

    def test_quiz_is_an_allowed_post_flag(self):
        """An undeclared flag is refused before login, so the flag has to exist in the allow-list."""
        block = re.search(r"ALLOWED_FLAGS = \{(.*?)\n\}", self.src, re.S).group(1)
        post_line = [l for l in block.splitlines() if l.strip().startswith('"post"')][0]
        self.assertIn("--quiz", post_line)

    def test_the_tally_is_created_only_for_a_published_post(self):
        block = self.src[self.src.index('elif cmd == "post":'):]
        block = block[: block.index('elif cmd == "reply":')]
        self.assertIn('if "--quiz" in sys.argv and status == 200 and not DRY_RUN', block)
        self.assertIn("tally_task(", block)

    def test_the_task_carries_the_quiz_uri_and_says_what_to_count(self):
        import importlib.util
        tmp = tempfile.mkdtemp()
        binp = os.path.join(tmp, "hermes")
        open(binp, "w").write(FAKE)
        os.chmod(binp, 0o755)
        calls = os.path.join(tmp, "calls.log")
        os.environ.update({"HERMES_BIN": binp, "FAKE_CALLS": calls})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("HERMES_BIN", "FAKE_CALLS")])
        spec = importlib.util.spec_from_file_location("dt_quiz", DT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        uri = "at://did:plc:me/town.delve.feed.post/3quiz"
        r = m.tally_task(uri, note="answers naming the seat", words="Quiz: which door?")
        self.assertEqual(r["status"], 200)
        called = open(calls).read()
        self.assertIn("--board delvetown create", called)
        self.assertIn("Tally: Quiz: which door?", called)
        self.assertIn(uri, called, "the body must name the quiz so a later run knows what it is tallying")
        self.assertIn("answers naming the seat", called)

    def test_an_unavailable_hermes_is_reported_not_swallowed(self):
        import importlib.util
        os.environ["HERMES_BIN"] = "/nonexistent/hermes"
        self.addCleanup(lambda: os.environ.pop("HERMES_BIN", None))
        spec = importlib.util.spec_from_file_location("dt_quiz2", DT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        r = m.tally_task("at://did:plc:me/town.delve.feed.post/3quiz")
        self.assertEqual(r["status"], 0)
        self.assertTrue(r["error"])


if __name__ == "__main__":
    unittest.main()
