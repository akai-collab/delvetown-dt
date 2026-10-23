"""A flag that does not exist must refuse the write, not be ignored by it.

2026-10-23: `reply <uri> <cid> "test" --dry-run` published a reply. The dry run is `DT_DRY_RUN=1`; there is
no `--dry-run` flag, the stray argument was ignored, and the post went to the town. Two of them went, one
per run. A silent no-op on a read path costs a wasted call; on a write path it publishes.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")
PARENT = "at://did:plc:zhcehhukgwhdt2mnokes2tui/town.delve.feed.post/3mycibpp7ck2b"
CID = "bafyreia2pe2zaludtyiekzt3uqpaqdzw3m5v4yulfsbiuh3bilxc56chca"


class UnknownFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def run_dt(self, *args):
        env = dict(os.environ, DT_PUBLISH_LOG=os.path.join(self.tmp, "log.jsonl"),
                   DT_CLAIMS_FILE=os.path.join(self.tmp, "claims.json"),
                   AT_MCP_WAKE_STATE=os.path.join(self.tmp, "wake.json"),
                   DT_PDS="http://127.0.0.1:9", DT_APPVIEW="http://127.0.0.1:9", HOME=self.tmp)
        env.pop("DT_DRY_RUN", None)
        return subprocess.run([sys.executable, DT, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_reply_refuses_an_unknown_flag_before_sending(self):
        """The exact shape that published twice: a real parent, real text, and `--dry-run`."""
        r = self.run_dt("reply", PARENT, CID, "test", "--dry-run")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no such flag", r.stdout)
        self.assertIn("--dry-run", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "log.jsonl")),
                         "a refused write leaves no log line")

    def test_post_refuses_an_unknown_flag(self):
        r = self.run_dt("post", "test", "--dry-run")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no such flag", r.stdout)

    def test_known_flags_still_work(self):
        """--again and --no-sign are real; the guard must not catch them."""
        r = self.run_dt("reply", PARENT, CID, "test", "--again", "--no-sign")
        self.assertNotIn("no such flag", r.stdout)


if __name__ == "__main__":
    unittest.main()
