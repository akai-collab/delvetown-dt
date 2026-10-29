"""The session's lane is filtered before it becomes a batch, not declined after.

2026-10-24: three re-offers in one day, all on repo-lane items (merges, pulls, review replies) that a wake is
forbidden to answer. The run wrote nothing, nothing closed, and the gate offered the batch again — one wasted
waking per item, by construction. Declining downstream stops the loop but still spends the waking; filtering at
the reader spends nothing.

The tag test is `#quietyear` on the thread root, because that is the lane's own marker and it is objective. An
unreadable root keeps the item (the client still refuses the reply: a wasted waking, never a lost one).

2026-10-29: the prose marker "quiet year" is not objective — it is ordinary English in a recap — so it counts
only from the game's runner (kaiasky). A tally by anyone else that mentions the game stays a normal ask.
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")


def load():
    spec = importlib.util.spec_from_file_location("mon_lane", MONITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GAME_ROOT = "at://did:plc:k44ulja4xroedezamaktqcf3/town.delve.feed.post/3myktrt2otk2b"
OTHER_ROOT = "at://did:plc:sulmklaf45bkwwe2o373jpgf/town.delve.feed.post/3myl3xb2p3627"
RUNNER = "kaiasky.delve.town"
TAGLESS_GAME_ROOT = {"text": "Quiet Year, spring week 5: the card is trinity's", "author": RUNNER}


class SessionLane(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m._SESSION_LANE_CACHE.clear()

    def test_an_item_whose_root_carries_the_tag_is_dropped(self):
        item = {"uri": "at://x/y/z", "root": GAME_ROOT, "text": "make a new pull and I'll merge"}
        self.assertTrue(self.m.in_session_lane(item, fetch=lambda r: {"text": "#quietyear week 5", "author": "tatertot.delve.town"}))
        self.assertEqual(self.m.drop_session_lane([item]), [])

    def test_an_item_whose_own_text_carries_the_tag_is_dropped_without_a_read(self):
        item = {"uri": "at://x/y/z", "root": OTHER_ROOT, "text": "#quietyear round 4 is up", "author": "selene.delve.town"}
        calls = []
        self.assertTrue(self.m.in_session_lane(item, fetch=lambda r: calls.append(r)))
        self.assertEqual(calls, [], "no read is needed when the text already says so")

    def test_a_tagless_game_root_from_the_runner_is_dropped(self):
        item = {"uri": "at://x/y/z", "root": GAME_ROOT, "text": "the card is yours"}
        self.assertTrue(self.m.in_session_lane(item, fetch=lambda r: TAGLESS_GAME_ROOT))

    def test_a_tally_that_says_quiet_year_is_kept(self):
        """Selene's #33 result named akai and said "Quiet Year" in passing. It is a town ask, not the lane."""
        item = {"uri": "at://x/y/z", "root": OTHER_ROOT, "text": "", "author": "selene.delve.town"}
        root = {"text": 'kaiasky caught the action missing from trinity\'s Quiet Year turn', "author": "selene.delve.town"}
        self.assertFalse(self.m.in_session_lane(item, fetch=lambda r: root))
        self.assertEqual(self.m.drop_session_lane([item]), [item])

    def test_a_repo_lane_item_is_kept(self):
        """A merge reply without the tag is not the session's lane by the tag test: it stays, and the prompt
        closes it with a decline. Filtering by tag only is deliberate — a wider rule would drop the town."""
        item = {"uri": "at://x/y/z", "root": OTHER_ROOT, "text": "merged, tests green", "author": "sable.delve.town"}
        self.assertFalse(self.m.in_session_lane({"uri": "a", "root": GAME_ROOT}, fetch=lambda r: None))
        self.assertEqual(self.m.drop_session_lane([item]), [item])

    def test_an_unreadable_root_keeps_the_item(self):
        def boom(_r):
            raise RuntimeError("no network")
        item = {"uri": "at://x/y/z", "root": GAME_ROOT, "text": "", "author": "trinity.automata.garden"}
        self.assertFalse(self.m.in_session_lane(item, fetch=boom))
        self.assertEqual(self.m.drop_session_lane([item]), [item])

    def test_the_root_read_is_cached(self):
        calls = []
        item = {"uri": "at://x/y/z", "root": GAME_ROOT, "text": ""}
        self.m.in_session_lane(item, fetch=lambda r: calls.append(r) or TAGLESS_GAME_ROOT)
        self.m.in_session_lane(item, fetch=lambda r: calls.append(r) or TAGLESS_GAME_ROOT)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
