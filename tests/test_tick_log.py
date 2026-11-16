"""The per-tick log: one line per gate tick saying why it waited or that it fired.

The case is 2026-11-15: a two-hour gap in which the state file was rewritten every tick, so the only
evidence left was file timestamps. The file must never reach stdout — that is what the scheduler
hashes, and a line that moves on a timer would wake the model for a non-event.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")

AGENT = "did:plc:agentagentagentagentagen"
T0 = 1794773000.0          # 2026-11-15, an arbitrary fixed moment


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def item(rkey, did, bot=True, root=None):
    return {"reason": "reply", "author": rkey + ".delve.town", "author_did": did, "root": root, "bot": bot,
            "uri": "at://%s/town.delve.feed.post/%s" % (did, rkey), "text": "hullo " + rkey,
            "indexed_at": "2026-11-15T20:22:00.000Z"}


class TickLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(MONITOR, "mon_tick_log")
        self.m.PUBLISH_LOG = os.path.join(self.tmp, "publish-log.jsonl")
        self.m.QUIET_FILE = os.path.join(self.tmp, "quiet.json")
        self.m.QUIET_THREADS_FILE = os.path.join(self.tmp, "quiet-threads.json")
        self.m.HUMANS_FILE = os.path.join(self.tmp, "verdicts-by-did.json")
        self.m.TICKS_LOG = os.path.join(self.tmp, "monitor-ticks-test.log")
        self.m.GATE = "wake"      # the tick log belongs to a gate tick, not a bare digest run
        self.m.SETTLE_S, self.m.MAX_WAIT_S, self.m.HUMAN_WAIT_S = 1200, 5400, 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lines(self):
        if not os.path.exists(self.m.TICKS_LOG):
            return []
        with open(self.m.TICKS_LOG, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def fields(self, line):
        return dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)

    def test_the_path_is_named_for_the_seat_and_off_by_default(self):
        os.environ.pop("AT_MCP_TICKS_LOG", None)
        os.environ.pop("AT_MCP_TICKS", None)
        self.assertEqual(load(MONITOR, "mon_tick_off").TICKS_LOG, "", "a test import must write nothing")
        os.environ["AT_MCP_TICKS"] = "1"
        try:
            m = load(MONITOR, "mon_tick_on")
            self.assertTrue(m.TICKS_LOG.endswith("monitor-ticks-%s.log" % m.ACCOUNT), m.TICKS_LOG)
        finally:
            os.environ.pop("AT_MCP_TICKS", None)

    def test_a_settling_tick_says_so(self):
        out, _ = self.m.wake_output([item("a", AGENT)], {}, T0 + 60, held=None, replies=[], answered={})
        self.assertEqual(out, "wake=none")
        last = self.fields(self.lines()[-1])
        self.assertEqual(last["verdict"], "settling")
        self.assertEqual(last["pending"], "1")
        self.assertEqual(last["ready"], "1")

    def test_nothing_pending_says_nothing(self):
        self.m.wake_output([], {}, T0, held=None, replies=[], answered={})
        self.assertEqual(self.fields(self.lines()[-1])["verdict"], "nothing")

    def test_a_paced_queue_says_paced_and_names_the_next_time(self):
        root = "at://did:plc:rootrootrootrootrootroot/town.delve.feed.post/r"
        it = item("a", AGENT, root=root)
        # a reply to this author in this thread already went out a minute ago: pace is 20 minutes
        out, _ = self.m.wake_output([it], {}, T0 + 60, held=None,
                                    replies=[(T0 - 60, root, AGENT)], answered={})
        self.assertEqual(out, "wake=none")
        last = self.fields(self.lines()[-1])
        self.assertEqual(last["verdict"], "paced")
        self.assertIn("next=", self.lines()[-1])
        self.assertEqual(last["ready"], "0")

    def test_a_fired_batch_says_fired_with_its_size(self):
        _, st = self.m.wake_output([item("a", AGENT)], {}, T0 + 60, held=None, replies=[], answered={})
        out, _ = self.m.wake_output([item("a", AGENT)], st, T0 + 1300, held=None, replies=[], answered={})
        self.assertTrue(out.startswith("wake=1"), out)
        last = self.fields(self.lines()[-1])
        self.assertEqual(last["verdict"], "fired")
        self.assertEqual(last["note"], "batch=1")

    def test_a_hold_is_named_as_a_hold(self):
        out, _ = self.m.wake_output([item("a", AGENT)], {}, T0 + 1300, held="rate-limited until 23:00Z",
                                    replies=[], answered={})
        self.assertEqual(out, "wake=none")
        last = self.fields(self.lines()[-1])
        self.assertEqual(last["verdict"], "held")
        self.assertIn("rate-limited", self.lines()[-1])

    def test_the_log_never_reaches_stdout(self):
        out, _ = self.m.wake_output([item("a", AGENT)], {}, T0 + 1300, held=None, replies=[], answered={})
        self.assertNotIn("verdict=", out or "")
        self.assertNotIn("monitor-ticks", out or "")

    def test_the_file_is_trimmed_not_grown(self):
        self.m.TICKS_KEEP = 5
        with open(self.m.TICKS_LOG, "w", encoding="utf-8") as f:
            for n in range(3000):
                f.write("2026-11-15T00:00:00Z seat=x verdict=nothing pending=0 ready=0 oldest=0m\n")
        self.assertGreater(os.path.getsize(self.m.TICKS_LOG), 120_000)
        self.m.wake_output([], {}, T0, held=None, replies=[], answered={})
        self.assertLessEqual(len(self.lines()), 5)


if __name__ == "__main__":
    unittest.main()
