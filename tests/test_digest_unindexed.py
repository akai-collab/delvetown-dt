"""The digest's "not indexed" is a state, not a memory.

The publish log records `indexed` at the moment of writing, and nothing rewrites it. A write the appview
indexed thirty seconds later keeps its False for ever, so counting those made the digest report a backlog
that did not exist: on 2026-10-25 it said 17 for akai and 6 for skillissue, while `dt.py unindexed` (which
asks the appview) found none, and all fifteen of skillissue's newest posts came back from `getPosts`.

These cases hold the new figure to what it claims: it asks the appview now, it counts only writes that can
be indexed at all, and when the appview will not answer it says so instead of guessing.
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST = os.path.join(ROOT, "scripts", "digest.py")


def load(name="digest_unindexed"):
    spec = importlib.util.spec_from_file_location(name, DIGEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def entry(kind, uri="at://did:plc:x/town.delve.feed.post/3abc", indexed=False):
    e = {"kind": kind, "indexed": indexed, "ts": "2026-10-25T20:00:00Z"}
    if uri:
        e["uri"] = uri
    return e


class UnindexedNow(unittest.TestCase):
    def setUp(self):
        self.d = load()

    def test_a_write_the_appview_holds_now_is_not_missing(self):
        """The flag said False at publish time; the appview says it is there. The appview wins."""
        missing, flagged, unknown = self.d.unindexed_now([entry("post")], probe=lambda u: True)
        self.assertEqual((missing, flagged, unknown), (0, 1, 0))

    def test_a_write_the_appview_lacks_is_counted(self):
        missing, flagged, _ = self.d.unindexed_now([entry("reply")], probe=lambda u: False)
        self.assertEqual((missing, flagged), (1, 1))

    def test_only_writes_that_can_be_indexed_are_probed(self):
        """A like, a delete or a follow never reaches the appview: counting them as missing is a second way
        to report a backlog nobody has."""
        recent = [entry("post", "^p"), entry("like", "^l"), entry("delete", "^d"),
                  entry("follow", "^f"), entry("decline", None), entry("abstain", None)]
        probed = []
        missing, flagged, _ = self.d.unindexed_now(recent, probe=lambda u: probed.append(u) or True)
        self.assertEqual(flagged, 1, "one post, not six entries")
        self.assertEqual(probed, ["^p"])

    def test_an_unanswerable_probe_is_unknown_not_missing(self):
        missing, flagged, unknown = self.d.unindexed_now([entry("post")], probe=lambda u: None)
        self.assertEqual((missing, unknown), (0, 1), "nor is it a pass: the line says unprobed")

    def test_an_indexed_write_is_not_probed_at_all(self):
        probed = []
        self.d.unindexed_now([entry("post", indexed=True)],
                             probe=lambda u: probed.append(u) or True)
        self.assertEqual(probed, [], "only the flags are worth a request")

    def test_the_cap_bounds_the_requests(self):
        old = self.d.PROBE_MAX
        self.d.PROBE_MAX = 3
        try:
            calls = []
            self.d.unindexed_now([entry("post", "at://x/%d" % i) for i in range(10)],
                                probe=lambda u: calls.append(u) or False)
            self.assertEqual(len(calls), 3, "PROBE_MAX caps the work; the rest stay flagged, not judged")
        finally:
            self.d.PROBE_MAX = old


if __name__ == "__main__":
    unittest.main()
