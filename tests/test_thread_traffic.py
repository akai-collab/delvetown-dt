"""Thread traffic is collected, never woken on, and reported in the digest.

A reply in a thread this seat only roots notifies on every message. 2026-10-24: a chain of eight hops, none of
them addressed here, each one an item in the gate's inbox. Someone who wants this seat's answer will tag it, so
they are traffic: counted, kept out of the batch, and surfaced twice a day.
"""
import importlib.util
import json
import os
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

    def test_a_tagged_chain_post_is_traffic_even_when_it_answers_this_seat(self):
        """#brokenphone hops reply to the seat's own root, so the parent test cannot see them; the tag can."""
        self.assertTrue(self.m.thread_traffic({"reason": "reply", "parent": ME, "text": "#brokenphone hop 4"}))
        self.assertFalse(self.m.thread_traffic({"reason": "reply", "parent": ME, "text": "hop 4, no tag"}))


if __name__ == "__main__":
    unittest.main()
