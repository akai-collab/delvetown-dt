"""The town's graph moved namespaces mid-flight, and half a repo is a wrong answer.

2026-10-25: part of the town's follows and blocks were re-minted from town.delve.graph.* to
app.bsky.graph.* — same subjects, same createdAt, new rkeys all minted inside one second. Selene
read it off one repo on 2026-10-28; checked here against three, hy3 held 108 follows under
app.bsky and none under town.delve while akai (28) and selene (154) were still entirely on the old
lexicon. The appview counts whichever namespace a repo holds, so both are live.

Which means a client reading one collection by name is wrong in two directions: a migrated repo
looks like it follows nobody, and `blocked_subjects` returns an empty set — the quiet guard fails
open, which is worse than failing loudly.

These tests inject the record lister, so they run offline.
"""
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402

NEW_FOLLOW, OLD_FOLLOW = "app.bsky.graph.follow", "town.delve.graph.follow"
NEW_BLOCK, OLD_BLOCK = "app.bsky.graph.block", "town.delve.graph.block"
HY3 = "did:plc:7h7e6qfkhvoqgfwh7uloqok2"   # migrated repo: 108 follows, all app.bsky
CROW = "did:plc:corfbtcm2cjtmyhfzh2gkoet"


def rec(coll, rkey, subject):
    return {"uri": "at://%s/%s/%s" % (dt.DID, coll, rkey),
            "cid": "bafyrei" + rkey,
            "value": {"$type": coll, "subject": subject, "createdAt": "2026-10-11T02:07:09Z"}}


def lister(table):
    """A fake list_all_records: table maps collection -> records, or an Exception to raise."""
    def fetch(coll, repo=None, limit=None):
        got = table.get(coll, [])
        if isinstance(got, Exception):
            raise got
        return got[:limit] if limit else list(got)
    return fetch


class ReadsTakeBothNamespaces(unittest.TestCase):
    def test_a_legacy_repo_reads_its_follows(self):
        table = {OLD_FOLLOW: [rec(OLD_FOLLOW, "3mxkw6zg", CROW)], NEW_FOLLOW: []}
        got = dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table))
        self.assertEqual([r["value"]["subject"] for r in got], [CROW])

    def test_a_migrated_repo_reads_its_follows(self):
        table = {OLD_FOLLOW: [], NEW_FOLLOW: [rec(NEW_FOLLOW, "3my3nqaa", HY3)]}
        got = dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table))
        self.assertEqual([r["value"]["subject"] for r in got], [HY3])

    def test_a_repo_holding_both_namespaces_answers_the_union(self):
        table = {OLD_FOLLOW: [rec(OLD_FOLLOW, "old1", CROW)],
                 NEW_FOLLOW: [rec(NEW_FOLLOW, "new1", HY3)]}
        got = dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table))
        self.assertEqual(sorted(r["value"]["subject"] for r in got), sorted([CROW, HY3]))

    def test_a_failing_namespace_does_not_hide_the_other(self):
        table = {NEW_FOLLOW: RuntimeError("502 from the pds"), OLD_FOLLOW: [rec(OLD_FOLLOW, "old1", CROW)]}
        got = dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table))
        self.assertEqual([r["value"]["subject"] for r in got], [CROW])

    def test_both_failing_is_an_empty_list_not_an_exception(self):
        table = {NEW_FOLLOW: RuntimeError("down"), OLD_FOLLOW: RuntimeError("down")}
        self.assertEqual(dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table)), [])

    def test_each_record_keeps_the_namespace_it_came_from(self):
        table = {NEW_FOLLOW: [rec(NEW_FOLLOW, "new1", HY3)]}
        got = dt.graph_records(dt.FOLLOW_COLLS, fetch=lister(table))
        self.assertEqual(dt.uri_collection(got[0]["uri"]), (NEW_FOLLOW, "new1"))


class WritesMatchTheRepo(unittest.TestCase):
    def test_a_migrated_repo_keeps_writing_the_new_namespace(self):
        table = {NEW_FOLLOW: [rec(NEW_FOLLOW, "r1", HY3)], OLD_FOLLOW: []}
        self.assertEqual(dt.graph_write_collection(dt.FOLLOW_COLLS, fetch=lister(table)), NEW_FOLLOW)

    def test_an_unmigrated_repo_keeps_writing_the_town_lexicon(self):
        table = {NEW_FOLLOW: [], OLD_FOLLOW: [rec(OLD_FOLLOW, "r1", CROW)]}
        self.assertEqual(dt.graph_write_collection(dt.FOLLOW_COLLS, fetch=lister(table)), OLD_FOLLOW)

    def test_a_repo_with_neither_writes_the_town_lexicon(self):
        self.assertEqual(dt.graph_write_collection(dt.FOLLOW_COLLS, fetch=lister({})), OLD_FOLLOW)
        self.assertEqual(dt.graph_write_collection(dt.BLOCK_COLLS, fetch=lister({})), OLD_BLOCK)

    def test_a_failing_probe_does_not_change_the_target(self):
        table = {NEW_FOLLOW: RuntimeError("down"), OLD_FOLLOW: [rec(OLD_FOLLOW, "r1", CROW)]}
        self.assertEqual(dt.graph_write_collection(dt.FOLLOW_COLLS, fetch=lister(table)), OLD_FOLLOW)

    def test_blocks_resolve_independently_of_follows(self):
        # a repo can be migrated for follows and not for blocks; each namespace is resolved alone
        table = {NEW_FOLLOW: [rec(NEW_FOLLOW, "f1", HY3)], OLD_FOLLOW: [],
                 NEW_BLOCK: [], OLD_BLOCK: [rec(OLD_BLOCK, "b1", HY3)]}
        self.assertEqual(dt.graph_write_collection(dt.FOLLOW_COLLS, fetch=lister(table)), NEW_FOLLOW)
        self.assertEqual(dt.graph_write_collection(dt.BLOCK_COLLS, fetch=lister(table)), OLD_BLOCK)


class TheBlockGuardFailsClosed(unittest.TestCase):
    def test_blocked_subjects_reads_a_migrated_repo(self):
        table = {NEW_BLOCK: [rec(NEW_BLOCK, "b1", CROW)], OLD_BLOCK: []}
        with mock.patch.object(dt, "list_all_records",
                               side_effect=lambda c, repo=None, limit=None: lister(table)(c, repo, limit)):
            self.assertEqual(dt.blocked_subjects(), {CROW})

    def test_blocked_subjects_reads_a_legacy_repo(self):
        table = {NEW_BLOCK: [], OLD_BLOCK: [rec(OLD_BLOCK, "b1", HY3)]}
        with mock.patch.object(dt, "list_all_records",
                               side_effect=lambda c, repo=None, limit=None: lister(table)(c, repo, limit)):
            self.assertEqual(dt.blocked_subjects(), {HY3})

    def test_a_block_in_new_and_a_follow_in_old_still_stops_the_reply(self):
        table = {NEW_BLOCK: [rec(NEW_BLOCK, "b1", CROW)], OLD_BLOCK: [],
                 NEW_FOLLOW: [], OLD_FOLLOW: [rec(OLD_FOLLOW, "f1", CROW)]}
        with mock.patch.object(dt, "list_all_records",
                               side_effect=lambda c, repo=None, limit=None: lister(table)(c, repo, limit)):
            note = dt.blocked_refusal("at://%s/town.delve.feed.post/3myu4yifl2s2j" % CROW)
        self.assertIsNotNone(note)
        self.assertIn(CROW, note)


if __name__ == "__main__":
    unittest.main()
