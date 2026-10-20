"""A batch whose run failed is sent again (the scheduler marks a monitor's output seen before the model
runs, so without this a failed run's batch looked handled until somebody else addressed the account)."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
T0 = 1792520000.0


def load():
    spec = importlib.util.spec_from_file_location("mon_retry", MONITOR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.SETTLE_S, m.MAX_WAIT_S, m.RETRY_WAIT_S, m.RETRY_MAX = 600, 2700, 300, 3
    return m


def note(rkey):
    return {"reason": "mention", "author": "selene.delve.town", "author_did": "did:plc:selene",
            "uri": "at://did:plc:selene/town.delve.feed.post/" + rkey, "text": "monkey's paw " + rkey,
            "indexed_at": "2026-10-20T19:30:00.000Z"}


class Retry(unittest.TestCase):

    def setUp(self):
        self.m = load()

    def tick(self, st, now, failed_at=None, items=None):
        return self.m.wake_output([note("a")] if items is None else items, st, now,
                                  quiet=set(), replies=[], failed_at=failed_at)

    def fired(self):
        _, st = self.tick({}, T0)
        out, st = self.tick(st, T0 + 600)
        self.assertTrue(out.startswith("wake=1 mention=1"), out)
        return out, st

    def test_a_failed_run_is_sent_again_after_the_wait(self):
        first, st = self.fired()
        fail = T0 + 900                                   # the run died five minutes after the batch fired
        same, st = self.tick(st, fail + 60, failed_at=fail)
        self.assertEqual(same, first, "not before RETRY_WAIT_S")
        again, st = self.tick(st, fail + 300, failed_at=fail)
        self.assertNotEqual(again, first, "a change, so the scheduler runs the model")
        self.assertTrue(again.startswith(first + "\nretry 1 of 3: the run for this batch failed at "), again)
        self.assertIn("dt.py recent", again)
        quiet, st = self.tick(st, fail + 600, failed_at=fail)
        self.assertEqual(quiet, again, "that failure is answered: no second retry for it")

    def test_a_run_that_worked_is_not_retried(self):
        first, st = self.fired()
        old_failure = T0 - 3600                           # a failure from before this batch
        out, _ = self.tick(st, T0 + 5000, failed_at=old_failure)
        self.assertEqual(out, first)

    def test_retries_back_off_and_stop(self):
        first, st = self.fired()
        now, outs = T0 + 600, []
        for i in range(6):                                # every retry's run fails too
            fail = now + 60
            waited = 300 * 2 ** st["retries"]
            out, st = self.tick(st, fail + waited - 1, failed_at=fail)
            self.assertEqual(out, outs[-1] if outs else first, "not before the doubled wait")
            out, st = self.tick(st, fail + waited, failed_at=fail)
            outs.append(out)
            now = fail + waited
        self.assertEqual(len(set(outs)), 3, "three retries, then nothing new")
        self.assertIn("retry 3 of 3", outs[2])

    def test_a_new_batch_resets_the_count(self):
        _, st = self.fired()
        _, st = self.tick(st, T0 + 1300, failed_at=T0 + 900)          # retry 1
        self.assertEqual(st["retries"], 1)
        items = [note("a"), note("b")]
        _, st = self.tick(st, T0 + 2000, items=items)
        out, st = self.tick(st, T0 + 2700, items=items)
        self.assertTrue(out.startswith("wake=1 mention=1"), out)
        self.assertNotIn("retry", out)
        self.assertEqual(st["retries"], 0)

    def test_a_hold_postpones_the_retry(self):
        first, st = self.fired()
        out, _ = self.m.wake_output([note("a")], st, T0 + 2000, held="rate-limited until 20:00Z",
                                    quiet=set(), replies=[], failed_at=T0 + 900)
        self.assertEqual(out, first)

    def test_an_outage_report_is_never_resent(self):
        self.m.FAIL_TICKS = 3
        _, st = self.fired()                              # a batch fired, then the client went quiet
        for i in range(3):
            out, st = self.m.wake_output(None, st, T0 + 700 + i * 60, failed_at=None)
        self.assertTrue(out.startswith("wake=unavailable"), out)
        self.assertIsNone(st["fired_at"])
        out2, _ = self.tick(st, T0 + 5000, failed_at=T0 + 1000, items=[])
        self.assertNotIn("retry", out2, "a failed run of the outage report does not resend the old batch")

    def test_last_failure_is_read_from_the_jobs_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"jobs": [{"id": "other"}, {"id": "2f5c47735836", "last_status": "ok",
                       "last_failure": {"at": "2026-10-20T21:29:17.701811+02:00", "detail": "HTTP 429"}}]}, f)
        try:
            at = self.m.last_failure_at(f.name, "2f5c47735836")
            self.assertEqual(datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M:%S"), "19:29:17")
            self.assertIsNone(self.m.last_failure_at(f.name, "other"))
            self.assertIsNone(self.m.last_failure_at("/nonexistent.json", "2f5c47735836"))
            self.assertIsNone(self.m.last_failure_at(f.name, ""), "off unless the deploy names the job")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
