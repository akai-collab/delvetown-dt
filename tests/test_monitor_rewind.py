"""The gate's state must not move backwards, and two ticks must not race over it.

On 2026-10-23 the gate's stored brief rewound to an older pair (04:30/05:37Z) after it had already
printed a newer one (10:45/11:02Z). The scheduler only cares whether the output changed, so a rewind
wakes the model to answer what it answered an hour earlier. Two causes are plausible and both are
cheap to close: a stale page from the appview, and two runs interleaving their read-modify-write.
"""

import datetime
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dt_monitor", ROOT / "delvetown_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def item(uri, indexed_at, reason="reply", author="someone.delve.town", text="hi"):
    return {"uri": uri, "indexed_at": indexed_at, "reason": reason, "author": author, "text": text}


def ts(hhmm):
    return f"2026-10-23T{hhmm}:00.000Z"


class MonitorRewind(unittest.TestCase):
    def test_a_rewind_is_ignored(self):
        old = item("at://x/post/1", ts("04:30"))
        newer_published = "wake=2 reply=2 id=newer\n" + "10:45Z reply @cairn.delve.town 3myjdry2ave27: newer brief"
        state = {"last": newer_published, "base": newer_published, "fired": {},
                 "newest": monitor._newest_epoch([item("at://x/post/2", ts("10:45"))])}

        out, st = monitor.wake_output([old], state, now=monitor._newest_epoch([old]) + 10000)

        self.assertEqual(out, newer_published, "an older reading must not replace the newer brief")
        self.assertEqual(st["last"], newer_published)
        self.assertEqual(st["newest"], state["newest"], "the high-water mark must not move down")

    def test_a_newer_reading_still_publishes(self):
        fresh = item("at://x/post/9", ts("12:10"))
        now = monitor._newest_epoch([fresh]) + 60
        state = {"last": "old brief", "base": "old brief", "fired": {},
                 "newest": monitor._newest_epoch([item("at://x/post/1", ts("04:30"))]),
                 # first seen long enough ago that the settle window has passed
                 "pending": {"at://x/post/9": {"t": now - monitor.SETTLE_S - 60, "item": fresh}}}
        out, st = monitor.wake_output([fresh], state, now=now)

        self.assertNotEqual(out, "old brief", "a genuinely newer batch must still print")
        self.assertGreaterEqual(st["newest"], monitor._newest_epoch([fresh]))

    def test_an_empty_reading_is_not_a_rewind(self):
        """No items means nothing to say, not a regression: the guard must not freeze the gate."""
        state = {"last": "old brief", "base": "old brief", "fired": {}, "newest": 2000.0}
        out, st = monitor.wake_output([], state, now=3000.0)
        self.assertEqual(out, "old brief")
        self.assertEqual(st["newest"], 2000.0)

    def test_items_without_timestamps_do_not_trip_the_guard(self):
        state = {"last": "old brief", "base": "old brief", "fired": {}, "newest": 2000.0}
        out, st = monitor.wake_output([{"uri": "at://x/post/1", "reason": "reply", "author": "a", "text": "b"}],
                                      state, now=3000.0)
        self.assertEqual(out, "old brief")

    def test_newest_epoch_reads_the_index_not_the_client_clock(self):
        items = [item("at://x/post/1", ts("04:30")), item("at://x/post/2", ts("11:02"))]
        newest = monitor._newest_epoch(items)
        self.assertEqual(datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).strftime("%H:%M"), "11:02")
        self.assertEqual(monitor._newest_epoch([]), 0)
        self.assertEqual(monitor._newest_epoch([{"indexed_at": "not a time"}]), 0)


if __name__ == "__main__":
    unittest.main()
