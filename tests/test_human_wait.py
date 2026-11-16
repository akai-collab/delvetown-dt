"""A person's item has its own limit: it fires the batch after HUMAN_WAIT_S, whatever the settle window
is doing. The case is 2026-11-15: a hello waited an hour while agents' replies restarted the window.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")

PERSON = "did:plc:personpersonpersonperson"
AGENT = "did:plc:agentagentagentagentagen"
UNKNOWN = "did:plc:unknownunknownunknownun"
TURNED = "did:plc:turnedturnedturnedturned"
T0 = 1794773000.0          # 2026-11-15, an arbitrary fixed moment


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def item(rkey, did, bot=False, root=None):
    return {"reason": "reply", "author": rkey + ".delve.town", "author_did": did, "root": root, "bot": bot,
            "uri": "at://%s/town.delve.feed.post/%s" % (did, rkey), "text": "hullo " + rkey,
            "indexed_at": "2026-11-15T20:22:00.000Z"}


class HumanWait(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(MONITOR, "mon_human_wait")
        self.m.PUBLISH_LOG = os.path.join(self.tmp, "publish-log.jsonl")
        self.m.QUIET_FILE = os.path.join(self.tmp, "quiet.json")
        self.m.QUIET_THREADS_FILE = os.path.join(self.tmp, "quiet-threads.json")
        self.m.HUMANS_FILE = os.path.join(self.tmp, "verdicts-by-did.json")
        self.m.SETTLE_S, self.m.MAX_WAIT_S, self.m.HUMAN_WAIT_S = 1200, 5400, 1200
        self.verdicts({PERSON: {"handle": "p.delve.town", "verdict": "human"},
                       AGENT: {"handle": "a.delve.town", "verdict": "agent"},
                       UNKNOWN: {"handle": "u.delve.town", "verdict": "?"}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verdicts(self, data):
        with open(self.m.HUMANS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def busy_hour(self, first):
        """`first` arrives, then an agent's reply every ten minutes: the settle window never closes.
        Returns the offset of the first tick that woke, or None within the hour."""
        items, st = [first], {}
        for n, t in enumerate(range(0, 3601, 300)):
            if t and t % 600 == 0:
                items = items + [item("a%d" % n, AGENT, bot=True)]
            out, st = self.m.wake_output(items, st, T0 + t, held=None, replies=[], answered={})
            if out != "wake=none":
                return t, out
        return None, None

    def test_a_person_is_answered_at_the_human_wait_though_the_thread_never_settles(self):
        t, out = self.busy_hour(item("hello", PERSON))
        self.assertEqual(t, 1200)
        self.assertIn("hello:", out)
        self.assertTrue(out.startswith("wake=3 reply=3"), "what else was waiting rides along: " + out)

    def test_agents_alone_still_wait_for_the_settle_window(self):
        t, _ = self.busy_hour(item("first", AGENT, bot=True))
        self.assertIsNone(t)

    def test_it_is_not_instant(self):
        p = [item("hello", PERSON)]
        _, st = self.m.wake_output(p, {}, T0, replies=[], answered={})
        out, st = self.m.wake_output(p + [item("a", AGENT, bot=True)], st, T0 + 900, replies=[], answered={})
        self.assertEqual(out, "wake=none")

    def test_a_bot_label_wins_over_a_human_verdict(self):
        t, _ = self.busy_hour(item("hello", PERSON, bot=True))
        self.assertIsNone(t)

    def test_an_unknown_or_undecided_account_is_not_guessed_at(self):
        self.assertIsNone(self.busy_hour(item("hello", UNKNOWN))[0])
        self.assertIsNone(self.busy_hour(item("hello", "did:plc:nobodynobodynobodynobody"))[0])

    def test_zero_turns_it_off(self):
        self.m.HUMAN_WAIT_S = 0
        self.assertIsNone(self.busy_hour(item("hello", PERSON))[0])

    def test_off_by_default(self):
        os.environ.pop("AT_MCP_WAKE_HUMAN_WAIT_S", None)
        self.assertEqual(load(MONITOR, "mon_human_default").HUMAN_WAIT_S, 0)

    def test_a_missing_or_broken_file_means_nobody_is_a_person(self):
        os.remove(self.m.HUMANS_FILE)
        self.assertEqual(self.m.human_dids(T0), set())
        self.assertIsNone(self.busy_hour(item("hello", PERSON))[0])
        with open(self.m.HUMANS_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(self.m.human_dids(T0), set())

    def test_a_hold_still_holds(self):
        p = [item("hello", PERSON)]
        _, st = self.m.wake_output(p, {}, T0, replies=[], answered={})
        more = p + [item("a", AGENT, bot=True)]
        _, st = self.m.wake_output(more, st, T0 + 900, replies=[], answered={})
        out, st = self.m.wake_output(more, st, T0 + 1300, held="rate-limited until 23:00Z", replies=[], answered={})
        self.assertEqual(out, "wake=none")
        out, st = self.m.wake_output(more, st, T0 + 1600, replies=[], answered={})
        self.assertTrue(out.startswith("wake=2"), out)

    def test_a_paced_person_waits_out_the_pace(self):
        root = "at://%s/town.delve.feed.post/thread" % AGENT
        p = [item("again", PERSON, root=root)]
        replies = [(T0 - 60, root, PERSON)]            # this account answered them a minute ago: paced 20 min
        self.m.PACE_PAIR_S = 1800
        _, st = self.m.wake_output(p, {}, T0, replies=replies, answered={})
        more = p + [item("a", AGENT, bot=True)]
        _, st = self.m.wake_output(more, st, T0 + 900, replies=replies, answered={})
        out, st = self.m.wake_output(more, st, T0 + 1300, replies=replies, answered={})
        self.assertEqual(out, "wake=none", "past the human wait, but its thread is still paced")

    def test_the_dated_verdict_covering_now_wins(self):
        self.verdicts({TURNED: {"handle": "t.delve.town", "verdict": "human", "as": [
            {"verdict": "human", "from": "2026-10-13T06:07:15Z", "until": "2026-11-06T05:40:00Z"},
            {"verdict": "agent", "from": "2026-11-06T05:40:00Z", "until": "2026-11-10T14:02:00Z"},
            {"verdict": "human", "from": "2026-11-10T14:02:00Z"}]}})
        self.assertIn(TURNED, self.m.human_dids(T0))
        self.assertNotIn(TURNED, self.m.human_dids(T0 - 7 * 86400), "8 Nov: an AI ran the account")

    def test_the_reader_keeps_the_bot_label(self):
        n = {"reason": "reply", "uri": "at://x/y/z", "record": {"text": "hi"},
             "author": {"did": AGENT, "handle": "a.delve.town", "labels": [{"val": "bot"}]}}
        self.assertTrue(self.m._item(n)["bot"])
        n["author"]["labels"] = []
        self.assertFalse(self.m._item(n)["bot"])


if __name__ == "__main__":
    unittest.main()
