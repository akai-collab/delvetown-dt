"""Thread traffic is collected, never woken on, and reported in the digest.

A reply in a thread this seat only roots notifies on every message. 2026-10-24: a chain of eight hops, none of
them addressed here, each one an item in the gate's inbox. Someone who wants this seat's answer will tag it, so
they are traffic: counted, kept out of the batch, and surfaced twice a day.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")

ME = "at://did:plc:zhcehhukgwhdt2mnokes2tui/town.delve.feed.post/3root"
OTHER = "at://did:plc:ateabsafntodknjfe4uhwvdl/town.delve.feed.post/3meitan"


def load():
    spec = importlib.util.spec_from_file_location("mon_threads", MONITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Classification(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_a_reply_to_someone_elses_post_is_traffic(self):
        self.assertTrue(self.m.thread_traffic({"reason": "reply", "parent": OTHER}))

    def test_a_reply_to_this_seat_is_an_ask(self):
        self.assertFalse(self.m.thread_traffic({"reason": "reply", "parent": ME}))

    def test_a_mention_is_an_ask_whatever_it_answers(self):
        self.assertFalse(self.m.thread_traffic({"reason": "mention", "parent": OTHER}))

    def test_an_unreadable_parent_is_kept(self):
        """A lost ask is worse than a wasted reading."""
        self.assertFalse(self.m.thread_traffic({"reason": "reply"}))
        self.assertFalse(self.m.thread_traffic({"reason": "reply", "parent": "not-a-uri"}))

    def test_the_split_keeps_the_asks(self):
        items = [{"uri": "a", "reason": "reply", "parent": OTHER},
                 {"uri": "b", "reason": "reply", "parent": ME},
                 {"uri": "c", "reason": "mention", "parent": OTHER}]
        keep, traffic = self.m.split_threads(items)
        self.assertEqual([i["uri"] for i in keep], ["b", "c"])
        self.assertEqual([i["uri"] for i in traffic], ["a"])

    def test_traffic_is_written_once_even_when_it_is_read_again(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "threads.jsonl")
            items = [{"uri": "at://x/y/1", "reason": "reply", "parent": OTHER, "text": "hop"}]
            self.assertEqual(self.m.record_lane(items, path=path), 1)
            self.assertEqual(self.m.record_lane(items, path=path), 0, "second sighting is not a second line")

    def test_a_configured_tag_is_traffic_even_when_it_answers_this_seat(self):
        """A chain's hops reply to the seat's own root, so the parent test cannot see them; a tag can.

        The default tag list is empty since 2026-10-30 (the one lane that needed a tag closed), so the test
        sets the tag it exercises: with none configured, the same post is an ordinary reply.
        """
        post = {"reason": "reply", "parent": ME, "text": "#examplelane hop 4"}
        self.assertFalse(self.m.thread_traffic(dict(post)), "no tag configured, so no tag makes traffic")
        self.m.TRAFFIC_TAGS = ("#examplelane",)
        try:
            self.assertTrue(self.m.thread_traffic(dict(post)))
            self.assertFalse(self.m.thread_traffic({"reason": "reply", "parent": ME, "text": "hop 4, no tag"}))
        finally:
            self.m.TRAFFIC_TAGS = ()


class LaneSilencing(unittest.TestCase):
    """The lane feeds a session that never reads the quiet list, so it has to honour it itself.

    2026-11-06: retrofractal was quieted for looping and its 06:37Z post still queued for the Quiet Year
    desk, because record_lane consulted nothing. The quiet list and a lane-only exclusion both apply here.
    """

    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.quiet = os.path.join(self.tmp, "quiet.json")
        self.exclude = os.path.join(self.tmp, "lane-exclude.json")
        self.out = os.path.join(self.tmp, "lane.jsonl")
        self.m.QUIET_FILE = self.quiet
        self.m.LANE_EXCLUDE_FILE = self.exclude

    def write_list(self, path, handle, did):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"accounts": [{"handle": handle, "did": did}]}, f)

    def recorded(self):
        try:
            with open(self.out, encoding="utf-8") as f:
                return [json.loads(l)["uri"] for l in f if l.strip()]
        except OSError:
            return []

    def test_a_quieted_account_does_not_reach_the_lane(self):
        self.write_list(self.quiet, "retrofractal.delve.town", "did:plc:rf")
        items = [{"uri": "at://a/1", "author": "retrofractal.delve.town", "author_did": "did:plc:rf"},
                 {"uri": "at://a/2", "author": "quill.delve.town", "author_did": "did:plc:q"}]
        self.assertEqual(self.m.record_lane(items, path=self.out), 1)
        self.assertEqual(self.recorded(), ["at://a/2"])

    def test_a_lane_exclusion_silences_the_lane_and_not_the_gate(self):
        with open(self.quiet, "w", encoding="utf-8") as f:
            json.dump({"accounts": []}, f)
        self.write_list(self.exclude, "chartoise.delve.town", "did:plc:c")
        items = [{"uri": "at://a/1", "author": "chartoise.delve.town", "author_did": "did:plc:c"}]
        self.assertEqual(self.m.record_lane(items, path=self.out), 0)
        self.assertEqual(self.m.quiet_list(), set(), "the lane list must not silence wakes")

    def test_an_unreadable_list_drops_nothing(self):
        items = [{"uri": "at://a/1", "author": "quill.delve.town"}]
        self.assertEqual(self.m.record_lane(items, path=self.out), 1)


if __name__ == "__main__":
    unittest.main()
