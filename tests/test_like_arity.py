"""A cid given to `like` is the subject's cid, not a second target.

2026-10-24: `dt.py like <post> <cid>` — the shape every other write takes — liked the post (verified true), then
took the cid for another target and printed a 400 RecordNotFound underneath. The like had landed; the output read
as a failure. Cids are now skipped in the target list.
"""
import inspect
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dt.py")


class LikeArity(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding="utf-8").read()

    def test_like_filters_cids_out_of_its_targets(self):
        block = self.src[self.src.index('elif cmd == "like":'):]
        block = block[: block.index('elif cmd == "unfollow":')]
        self.assertIn("cid_shaped(a)", block, "the target loop must skip cids")
        self.assertNotIn("for arg in sys.argv[2:]:", block, "the unfiltered loop is the bug")

    def test_the_filter_uses_the_same_cid_rule_as_reply(self):
        """One rule, not two: a value reply treats as a misplaced cid must also leave like's targets."""
        self.assertIn("def cid_shaped(", self.src)
        self.assertIn("CID_RE.match(value)", self.src)


if __name__ == "__main__":
    unittest.main()
