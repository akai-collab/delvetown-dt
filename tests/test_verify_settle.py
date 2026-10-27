"""Three ideas from Void's Delve client (tangled.org/void.comind.network/interacting-with-delve-town), 2026-10-27.

1. One `town.delve.feed.getPosts` call asks about 25 records; getPostThread asked about one.
2. After a write, check that the town holds *what was sent* — author, text, parent, root — not only that
   something sits at the URI.
3. A post the PDS accepted can reach the town minutes later (Void's first outside reply: ~22 minutes), so
   young writes are listed as settling rather than lost, and are not re-filed yet.

Offline: every appview answer here is a stub.
"""

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def uri(rkey, did=None):
    return "at://%s/town.delve.feed.post/%s" % (did or dt.DID, rkey)


def view(u, text="hi", parent=None, root=None, did=None):
    rec = {"$type": "town.delve.feed.post", "text": text}
    if parent or root:
        rec["reply"] = {"parent": {"uri": parent}, "root": {"uri": root}}
    return {"uri": u, "author": {"did": did or dt.DID}, "record": rec}


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


class AppviewPosts(unittest.TestCase):
    def test_asks_twenty_five_at_a_time_and_once_per_uri(self):
        calls = []

        def get(batch):
            calls.append(list(batch))
            return [view(u) for u in batch if not u.endswith("/7")]   # the appview lacks one

        uris = [uri(str(i)) for i in range(30)] + [uri("3")]          # a repeat is asked once
        found = dt.appview_posts(uris, get)
        self.assertEqual([len(c) for c in calls], [25, 5])
        self.assertEqual(len(found), 29)
        self.assertNotIn(uri("7"), found)

    def test_a_failed_ask_raises_rather_than_reading_as_absent(self):
        def get(batch):
            raise RuntimeError("appview down")
        with self.assertRaises(RuntimeError):
            dt.appview_posts([uri("a")], get)

    def test_nothing_to_ask_asks_nothing(self):
        self.assertEqual(dt.appview_posts([], lambda b: self.fail("asked")), {})


class AppviewVerify(unittest.TestCase):
    P, R = uri("parent", "did:plc:other"), uri("root", "did:plc:other")

    def test_an_exact_copy_is_indexed_with_no_mismatch(self):
        get = lambda b: [view(uri("a"), "hello", self.P, self.R)]
        self.assertEqual(dt.appview_verify(uri("a"), "hello", self.P, self.R, get), (True, []))

    def test_each_differing_field_is_named(self):
        get = lambda b: [view(uri("a"), "hullo", uri("elsewhere"), self.R, did="did:plc:someone")]
        idx, mism = dt.appview_verify(uri("a"), "hello", self.P, self.R, get)
        self.assertTrue(idx)
        self.assertEqual(mism, ["author", "text", "parent"])

    def test_not_held_is_not_indexed_and_claims_no_mismatch(self):
        self.assertEqual(dt.appview_verify(uri("a"), "hello", get=lambda b: []), (False, []))

    def test_a_failed_probe_reads_as_not_indexed(self):
        def get(b):
            raise RuntimeError("down")
        self.assertEqual(dt.appview_verify(uri("a"), "hello", get=get), (False, []))

    def test_fields_not_given_are_not_checked(self):
        get = lambda b: [view(uri("a"), "anything")]
        self.assertEqual(dt.appview_verify(uri("a"), get=get), (True, []))


class UnindexedBatchAndSettle(unittest.TestCase):
    FETCH = staticmethod(lambda rk: {"value": {"text": "text " + rk}})

    def test_one_ask_answers_for_the_whole_page(self):
        calls = []

        def posts(batch):
            calls.append(len(batch))
            return [view(u) for u in batch if not u.endswith("/b")]

        entries = [{"rkey": r, "kind": "post", "ts": ts(120)} for r in "abc"]
        out = dt.unindexed_entries(entries, fetch=self.FETCH, posts=posts, now=NOW)
        self.assertEqual(calls, [3])
        self.assertEqual([e["rkey"] for e in out], ["b"])
        self.assertNotIn("settling", out[0])

    def test_a_young_write_is_settling_not_lost(self):
        entries = [{"rkey": "new", "kind": "reply", "ts": ts(5)},
                   {"rkey": "old", "kind": "reply", "ts": ts(45)}]
        out = {e["rkey"]: e for e in dt.unindexed_entries(entries, fetch=self.FETCH, posts=lambda b: [], now=NOW)}
        self.assertTrue(out["new"]["settling"])
        self.assertEqual(out["new"]["age_s"], 300)
        self.assertNotIn("settling", out["old"])

    def test_an_unreadable_stamp_counts_as_settled(self):
        out = dt.unindexed_entries([{"rkey": "x", "kind": "post", "ts": "t1"}], fetch=self.FETCH,
                                   posts=lambda b: [], now=NOW)
        self.assertNotIn("settling", out[0])

    def test_when_the_batch_cannot_be_asked_each_entry_is_probed(self):
        def posts(b):
            raise RuntimeError("getPosts down")
        entries = [{"rkey": r, "kind": "post", "ts": ts(120)} for r in "ab"]
        with mock.patch.object(dt, "appview_has", lambda u: u.endswith("/a")):
            out = dt.unindexed_entries(entries, fetch=self.FETCH, posts=posts, now=NOW)
        self.assertEqual([e["rkey"] for e in out], ["b"])

    def test_no_posts_in_the_page_asks_nothing(self):
        entries = [{"rkey": "l", "kind": "like", "ts": ts(120)}]
        self.assertEqual(dt.unindexed_entries(entries, posts=lambda b: self.fail("asked")), [])


if __name__ == "__main__":
    unittest.main()
