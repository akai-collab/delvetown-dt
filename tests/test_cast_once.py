"""One cast per scheduled run, enforced by the client.

2026-11-14: casts 82 and 83 were logged three seconds apart in one clock wake, and 82 was never signed. Runs
call `dt cast` twice for ordinary reasons (output cut by `head`, a first try down a wrong path); each call
tossed again. Inside a cron run the second call now returns the first cast.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("DT_DRY_RUN", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dt_cast_once", os.path.join(HERE, "..", "dt.py"))
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)

CRON = {"HERMES_CRON_SESSION": "1", "HERMES_SESSION_ID": "cron_2f5c47735836_20261114_180500"}


class CastOncePerRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = dt.CAST_LOG
        dt.CAST_LOG = os.path.join(self.tmp, "casts.jsonl")

    def tearDown(self):
        dt.CAST_LOG = self.old
        shutil.rmtree(self.tmp)

    def rows(self):
        with open(dt.CAST_LOG, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_a_second_cast_in_one_run_returns_the_first(self):
        run = dt.cast_run_id(CRON)
        first = dt.cast_hexagram(run=run)
        again = dt.cast_hexagram(run=run)
        self.assertEqual(again["cast"], first["cast"])
        self.assertEqual(again["sig"], first["sig"])
        self.assertTrue(again["repeat"])
        self.assertEqual(len(self.rows()), 1, "nothing new is logged")

    def test_two_runs_each_get_their_own(self):
        a = dt.cast_hexagram(run="cron_a")
        b = dt.cast_hexagram(run="cron_b")
        self.assertEqual((a["cast"], b["cast"]), (1, 2))
        self.assertNotIn("repeat", b)

    def test_outside_cron_every_call_casts(self):
        self.assertIsNone(dt.cast_run_id({"HERMES_SESSION_ID": "chat_123"}))
        self.assertIsNone(dt.cast_run_id({}))
        a = dt.cast_hexagram(run=dt.cast_run_id({"HERMES_SESSION_ID": "chat_123"}))
        b = dt.cast_hexagram(run=dt.cast_run_id({"HERMES_SESSION_ID": "chat_123"}))
        self.assertEqual((a["cast"], b["cast"]), (1, 2))

    def test_cron_without_a_session_id_is_not_keyed(self):
        self.assertIsNone(dt.cast_run_id({"HERMES_CRON_SESSION": "1"}))

    def test_the_run_is_written_into_the_log_row(self):
        dt.cast_hexagram(run="cron_a")
        self.assertEqual(self.rows()[0]["run"], "cron_a")

    def test_a_dealt_voice_is_never_keyed(self):
        dt.cast_hexagram(run="cron_a")
        dealt = dt.cast_hexagram(log=False, run="cron_a")
        self.assertNotIn("repeat", dealt)
        self.assertEqual(len(self.rows()), 1)

    def test_old_rows_without_a_run_never_match(self):
        with open(dt.CAST_LOG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"cast": 1, "sig": "x · cast 1"}) + "\n")
        rec = dt.cast_hexagram(run="cron_a")
        self.assertEqual(rec["cast"], 2)


if __name__ == "__main__":
    unittest.main()
