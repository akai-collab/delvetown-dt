"""The quiet list and thread pacing: what the monitor will not wake for, and what dt.py will not send.

Both files carry their own copy of the rules (the deployed monitor is a lone file and cannot import the
repo), so the cases at the bottom hold the two copies to the same answers.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
DT = os.path.join(ROOT, "dt.py")

ME = "did:plc:pupeplyyuw54mc6ngxavghv2"
KALLISTI = "did:plc:kallistikallistikallisti"
SELENE = "did:plc:seleneseleneselenesele"
ROOT_URI = "at://%s/town.delve.feed.post/thread1" % SELENE
OTHER_ROOT = "at://%s/town.delve.feed.post/thread2" % SELENE
T0 = 1792400000.0          # 2026-10-19, an arbitrary fixed moment


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def reply_entry(t, parent_did, root=ROOT_URI, rkey="x"):
    return {"ts": iso(t), "kind": "reply", "rkey": rkey,
            "parent_uri": "at://%s/town.delve.feed.post/p%s" % (parent_did, rkey), "root_uri": root}


def item(rkey, author="kallisti.delve.town", did=KALLISTI, root=ROOT_URI, reason="reply"):
    return {"reason": reason, "author": author, "author_did": did, "root": root,
            "uri": "at://%s/town.delve.feed.post/%s" % (did, rkey), "text": "hullo " + rkey,
            "indexed_at": "2026-10-19T04:00:00.000Z"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "publish-log.jsonl")
        self.quiet = os.path.join(self.tmp, "quiet.json")
        self.m = load(MONITOR, "mon_pacing")
        self.m.PUBLISH_LOG, self.m.QUIET_FILE = self.log, self.quiet
        self.m.SETTLE_S, self.m.MAX_WAIT_S = 600, 2700
        self.m.PACE_PAIR_S, self.m.PACE_THREAD_S, self.m.PACE_RESET_S = 1200, 600, 6 * 3600

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_log(self, entries):
        with open(self.log, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def write_quiet(self, data):
        with open(self.quiet, "w", encoding="utf-8") as f:
            json.dump(data, f)


class QuietList(Fixture):
    """An account on the list never wakes the seat, and nothing about it is public."""

    def tick(self, items, st, now):
        return self.m.wake_output(items, st, now)

    def test_a_listed_account_never_wakes(self):
        self.write_quiet({"accounts": [{"did": KALLISTI, "handle": "kallisti.delve.town"}]})
        out, st = self.tick([item("k1")], {}, T0)
        out, st = self.tick([item("k1")], st, T0 + 5000)
        self.assertEqual(out, "wake=none")

    def test_it_matches_by_did_through_a_rename(self):
        """octavius became octavia on 19 Oct: a list keyed by the old handle would have reopened."""
        self.write_quiet({"accounts": [{"did": KALLISTI, "handle": "kallisti.delve.town"}]})
        renamed = item("k2", author="kallista.delve.town")
        _, st = self.tick([renamed], {}, T0)
        self.assertEqual(self.tick([renamed], st, T0 + 5000)[0], "wake=none")

    def test_someone_else_in_the_same_window_still_wakes_and_the_muted_one_is_only_counted(self):
        self.write_quiet({"accounts": [{"did": KALLISTI}]})
        items = [item("k1"), item("s1", author="selene.delve.town", did=SELENE)]
        _, st = self.tick(items, {}, T0)
        out, st = self.tick(items, st, T0 + 700)
        self.assertTrue(out.startswith("wake=1 reply=1 id="), out)
        self.assertIn("@selene.delve.town s1", out)
        self.assertNotIn("k1", out, "a muted item is counted, never shown")
        self.assertTrue(out.endswith("also: 1 muted"), out)

    def test_adding_an_account_quiets_what_is_already_waiting(self):
        _, st = self.tick([item("k1")], {}, T0)                       # pending, not yet settled
        self.write_quiet({"accounts": [{"did": KALLISTI}]})
        out, st = self.tick([item("k1")], st, T0 + 700)
        self.assertEqual(out, "wake=none")
        self.assertEqual(st["pending"], {})

    def test_removing_it_applies_on_the_next_tick(self):
        self.write_quiet({"accounts": [{"did": KALLISTI}]})
        _, st = self.tick([item("k1")], {}, T0)
        self.write_quiet({"accounts": []})
        _, st = self.tick([item("k1")], st, T0 + 60)
        out, _ = self.tick([item("k1")], st, T0 + 700)
        self.assertTrue(out.startswith("wake=1"), out)

    def test_the_older_shapes_still_read(self):
        """The 19 Oct stopgap wrote {"handles": [...]}; a hand-written file may be a bare list."""
        self.write_quiet({"handles": ["kallisti.delve.town", "did:plc:other"]})
        self.assertEqual(self.m.quiet_list(), {"kallisti.delve.town", "did:plc:other"})
        self.write_quiet(["@Kallisti.delve.town"])
        self.assertEqual(self.m.quiet_list(), {"kallisti.delve.town"})

    def test_a_missing_or_broken_file_is_an_empty_list(self):
        self.assertEqual(self.m.quiet_list(), set())
        with open(self.quiet, "w") as f:
            f.write("{not json")
        self.assertEqual(self.m.quiet_list(), set())

    def test_the_file_is_named_for_the_account(self):
        os.environ["AT_MCP_HANDLE"], saved = "skillissue.delve.town", os.environ.get("AT_MCP_HANDLE")
        os.environ.pop("DT_QUIET_FILE", None)
        try:
            m = load(MONITOR, "mon_pacing_paths")
        finally:
            if saved is None:
                os.environ.pop("AT_MCP_HANDLE")
            else:
                os.environ["AT_MCP_HANDLE"] = saved
        self.assertTrue(m.QUIET_FILE.endswith("/quiet-skillissue.json"), m.QUIET_FILE)


class Pacing(Fixture):
    """Deep threads are fine; fast ones slow down. The wait doubles with each turn in a run."""

    def test_the_wait_doubles_and_a_long_gap_resets_it(self):
        w = self.m.pace_wait
        self.assertIsNone(w([], 1200))
        self.assertEqual(w([T0], 1200), T0 + 1200)
        self.assertEqual(w([T0, T0 + 1500], 1200), T0 + 1500 + 2400)
        self.assertEqual(w([T0, T0 + 1500, T0 + 4000], 1200), T0 + 4000 + 4800)
        self.assertEqual(w([T0, T0 + 7 * 3600], 1200), T0 + 7 * 3600 + 1200, "6h apart: a new run")
        self.assertEqual(w([T0 + i for i in range(20)], 1200), T0 + 19 + 6 * 3600, "capped at the reset")
        self.assertIsNone(w([T0], 0), "0 turns the clock off")

    def test_the_19_october_exchange_is_slowed(self):
        """Seven replies to one bot in one thread, 04:38Z to 10:35Z. Under the pace, against a bot that
        answers every five minutes, the seat's turns come 20, 40, 80 and 160 minutes apart: five replies
        in those six hours instead of seven, and the next one not before the run resets."""
        start = T0
        sent, now = [], start
        while now < start + 6 * 3600:
            replies = [(t, ROOT_URI, KALLISTI) for t in sent]
            if not self.m.paced_until(item("k"), now, replies):
                sent.append(now)
            now += 300                       # kallisti answers every five minutes
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        self.assertEqual(len(sent), 5, sent)
        self.assertEqual(gaps, [1200, 2400, 4800, 9600])

    def test_another_thread_and_another_account_are_their_own_clocks(self):
        replies = [(T0, ROOT_URI, KALLISTI)]
        self.assertTrue(self.m.paced_until(item("k"), T0 + 60, replies))
        self.assertIsNone(self.m.paced_until(item("k", root=OTHER_ROOT), T0 + 60, replies))
        # selene in the same thread: only the thread clock (10 min) applies, not the pair clock
        s = item("s", author="selene.delve.town", did=SELENE)
        self.assertTrue(self.m.paced_until(s, T0 + 300, replies))
        self.assertIsNone(self.m.paced_until(s, T0 + 601, replies))

    def test_a_mention_with_no_thread_is_never_paced(self):
        self.assertIsNone(self.m.paced_until(item("k", root=None), T0 + 1, [(T0, ROOT_URI, KALLISTI)]))

    def test_a_paced_item_waits_and_then_wakes_on_its_own(self):
        self.write_log([reply_entry(T0, KALLISTI)])
        _, st = self.m.wake_output([item("k1")], {}, T0 + 60)
        out, st = self.m.wake_output([item("k1")], st, T0 + 900)       # settled, but paced to +1200
        self.assertEqual(out, "wake=none")
        self.assertIn(item("k1")["uri"], st["pending"], "kept, not dropped")
        out, st = self.m.wake_output([item("k1")], st, T0 + 1260)
        self.assertTrue(out.startswith("wake=1 reply=1"), out)

    def test_a_batch_that_fires_for_someone_else_leaves_the_paced_item_waiting(self):
        self.write_log([reply_entry(T0, KALLISTI)])
        items = [item("k1"), item("o1", root=OTHER_ROOT, author="selene.delve.town", did=SELENE)]
        _, st = self.m.wake_output(items, {}, T0 + 60)
        out, st = self.m.wake_output(items, st, T0 + 700)
        self.assertTrue(out.startswith("wake=1 reply=1"), out)
        self.assertIn("o1", out)
        self.assertNotIn("k1:", out)
        self.assertIn("paced: 1 waiting, next at", out)
        self.assertEqual(list(st["pending"]), [item("k1")["uri"]])
        again, st = self.m.wake_output(items, st, T0 + 800)
        self.assertEqual(again, out, "nothing new: no second wake")

    def test_the_publish_log_is_read_for_replies_only(self):
        self.write_log([reply_entry(T0, KALLISTI), {"ts": iso(T0), "kind": "post"},
                        {"ts": iso(T0), "kind": "like", "subject": ROOT_URI}, "not json"])
        self.assertEqual(self.m.own_replies(T0 + 10), [(T0, ROOT_URI, KALLISTI)])


class Client(Fixture):
    """dt.py: the same pace, so a clock wake cannot make the fast reply the gate held back; and the
    quiet list, which `dt.py quiet` edits but `reply` does not consult."""

    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in ("DT_QUIET_FILE", "DT_PUBLISH_LOG", "DT_DRY_RUN")}
        os.environ.update({"DT_QUIET_FILE": self.quiet, "DT_PUBLISH_LOG": self.log, "DT_DRY_RUN": "1"})
        try:
            self.dt = load(DT, "dt_pacing")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def run_dt(self, *args):
        # an unreachable PDS: no lookup may reach the town, and a dry run sends nothing anyway
        env = dict(os.environ, DT_QUIET_FILE=self.quiet, DT_PUBLISH_LOG=self.log, DT_DRY_RUN="1",
                   DT_PDS="http://127.0.0.1:9", DT_APPVIEW="http://127.0.0.1:9", HOME=self.tmp)
        return subprocess.run([sys.executable, DT, *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def test_a_listed_account_can_still_be_answered(self):
        """The list breaks the wake loop; it is not a judgement. A run that comes across a listed
        account on its own may answer it — an account that must not be answered is blocked."""
        self.write_quiet({"accounts": [{"did": KALLISTI}]})
        r = self.run_dt("reply", "at://%s/town.delve.feed.post/abc" % KALLISTI, "bafyx", "an answer")
        self.assertNotIn("refused", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_fast_reply_is_refused_on_the_real_path(self):
        parent = "at://%s/town.delve.feed.post/abc" % KALLISTI
        self.write_log([{"ts": iso(datetime.now(timezone.utc).timestamp() - 300), "kind": "reply",
                         "parent_uri": parent, "root_uri": parent}])
        r = self.run_dt("reply", parent, "bafyx", "an answer")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("waits until", json.loads(r.stdout)["refused"])

    def test_a_fast_reply_is_refused_with_the_time_it_may_go(self):
        entries = [reply_entry(T0, KALLISTI)]
        parent = "at://%s/town.delve.feed.post/next" % KALLISTI
        msg = self.dt.pace_refusal(ROOT_URI, parent, now=T0 + 60, entries=entries)
        self.assertIn("to %s in this thread" % KALLISTI, msg)
        self.assertIn(datetime.fromtimestamp(T0 + 1200, timezone.utc).strftime("%H:%MZ"), msg)
        self.assertIsNone(self.dt.pace_refusal(ROOT_URI, parent, now=T0 + 1201, entries=entries))
        self.assertIsNone(self.dt.pace_refusal(ROOT_URI, parent, force=True, now=T0 + 60, entries=entries))

    def test_the_monitor_and_the_client_agree(self):
        """Every combination below gets the same verdict from both copies."""
        logs = [[], [reply_entry(T0, KALLISTI)],
                [reply_entry(T0, KALLISTI, rkey="a"), reply_entry(T0 + 1300, KALLISTI, rkey="b")],
                [reply_entry(T0, SELENE)], [reply_entry(T0, KALLISTI, root=OTHER_ROOT)]]
        for entries in logs:
            for dt_s in (30, 650, 1250, 2500, 3800, 7 * 3600):
                for who, did in (("kallisti.delve.town", KALLISTI), ("selene.delve.town", SELENE)):
                    now = T0 + 1300 + dt_s
                    self.write_log(entries)
                    mon = bool(self.m.paced_until(item("z", author=who, did=did), now, self.m.own_replies(now)))
                    cli = bool(self.dt.pace_refusal(ROOT_URI, "at://%s/town.delve.feed.post/z" % did,
                                                    now=now, entries=entries))
                    self.assertEqual(mon, cli, (entries, dt_s, who))

    def test_quiet_add_list_remove(self):
        # an unreachable PDS: the handle lookup for a DID must fail quietly, not reach the town
        env = dict(os.environ, DT_QUIET_FILE=self.quiet, DT_PUBLISH_LOG=self.log, DT_DRY_RUN="1",
                   DT_PDS="http://127.0.0.1:9")
        run = lambda *a: subprocess.run([sys.executable, DT, "quiet", *a], env=env, capture_output=True,
                                        text=True, timeout=60)
        added = run("add", KALLISTI, "rapid-fire", "bot")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        with open(self.quiet) as f:
            data = json.load(f)
        self.assertEqual(data["accounts"][0]["did"], KALLISTI)
        self.assertEqual(data["accounts"][0]["note"], "rapid-fire bot")
        run("add", KALLISTI)                                            # adding twice keeps one entry
        self.assertEqual(json.loads(run("list").stdout)["count"], 1)
        self.assertEqual(json.loads(run().stdout)["count"], 1, "bare `quiet` lists")
        self.assertEqual(json.loads(run("remove", KALLISTI).stdout)["count"], 0)
        self.assertIn("was not on", run("remove", KALLISTI).stdout)


if __name__ == "__main__":
    unittest.main()
