"""The gate's wall-liveness line: a dead timer must not look like a quiet canvas.

wall-digest.py writes its stamp on every pass, silent ones included, and the digest itself prints nothing
when the canvas is unchanged. The gate reads the stamp and says one line when it goes stale; the line quotes
the stamp rather than the age so that two ticks with no news stay byte-identical, which is what keeps the
scheduler from waking a run every five minutes.
"""
import datetime
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
T0 = 1794773000.0          # 2026-11-15T20:03:20Z, an arbitrary fixed moment


def load():
    spec = importlib.util.spec_from_file_location("dtmon_wall", MONITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WallStamp(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp()
        self.stamp = os.path.join(self.tmp, "last-seen.txt")
        self.m.WALL_STAMP = self.stamp
        self.m.WALL_STALE_S = 2700

    def write(self, when):
        with open(self.stamp, "w", encoding="utf-8") as f:
            f.write("%s 71 strokes 0 new\n" % when)

    def iso(self, epoch):
        return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    def test_fresh_stamp_says_nothing(self):
        self.write(self.iso(T0 - 600))
        self.assertIsNone(self.m.wall_note(T0))

    def test_stale_stamp_names_the_stamp(self):
        self.write(self.iso(T0 - 3600))
        line = self.m.wall_note(T0)
        self.assertTrue(line.startswith("wall: no pass since "))
        self.assertIn(self.iso(T0 - 3600), line)
        self.assertIn("stalled", line)

    def test_line_is_stable_across_ticks(self):
        self.write(self.iso(T0 - 3600))
        first = self.m.wall_note(T0)
        second = self.m.wall_note(T0 + 600)
        self.assertEqual(first, second, "an age in the line would wake every tick")

    def test_missing_stamp_says_nothing(self):
        self.assertIsNone(self.m.wall_note(T0))

    def test_unreadable_stamp_says_nothing(self):
        with open(self.stamp, "w", encoding="utf-8") as f:
            f.write("not a stamp\n")
        self.assertIsNone(self.m.wall_note(T0))


if __name__ == "__main__":
    unittest.main()
