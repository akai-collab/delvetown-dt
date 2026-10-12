"""The monitor's contract, tested against a fake at_mcp: the gate line, the distilled brief, and
the failure paths that must not schedule a wake.

Offline and accountless: FAKE/AT_MCP_BIN points at tests/fake_at_mcp.py, the env file is
/dev/null, and the state dir is a temporary directory.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
GATE = re.compile(r"^unread=\S+ digest=\S+$")


def run_monitor(mode="ok", seconds="20", brief=None):
    env = dict(os.environ)
    env.update({
        "AT_MCP_BIN": sys.executable,
        "AT_MCP_BIN_ARGS": os.path.join(ROOT, "tests", "fake_at_mcp.py"),
        "DELVETOWN_ENV_FILE": "/dev/null",
        "AT_MCP_HANDLE": "test.delve.town",
        "FAKE_MODE": mode,
        "FAKE_SECONDS": seconds,
        "PYTHONPATH": os.path.join(ROOT, "tests"),
    })
    env.pop("AT_MCP_MONITOR_BRIEF", None)
    if brief is not None:
        env["AT_MCP_MONITOR_BRIEF"] = brief
    with tempfile.TemporaryDirectory() as state:
        env["AT_MCP_MONITOR_STATE"] = state
        proc = subprocess.run([sys.executable, MONITOR], env=env, capture_output=True, text=True,
                              timeout=60)
    return proc.stdout, proc.stderr


class Monitor(unittest.TestCase):
    """The monitor runs a fake binary: see fake_at_mcp.py for the modes."""

    def test_gate_line_then_distilled_brief(self):
        out, err = run_monitor("ok")
        lines = out.strip().splitlines()
        self.assertRegex(lines[0], GATE)
        self.assertEqual(lines[0].split()[0], "unread=254")
        brief = lines[1:]
        self.assertEqual(len(brief), 2, "one line per notification")
        self.assertTrue(brief[0].startswith("18:57Z mention @selene.delve.town 3mxp74ib2yx2f:"), brief[0])
        self.assertTrue(brief[1].startswith("19:04Z reply @carly.delve.town "), brief[1])
        # the fields nobody reads are gone, and long text is cut rather than the item list
        self.assertNotIn("facets", out)
        self.assertNotIn("cid", out)
        self.assertNotIn("did:plc:", out)
        self.assertLess(len(out), 1200, "ten small items must fit in the prompt cheaply")
        self.assertEqual(err.strip(), "", "nothing on stderr: the scheduler merges it into the hash")

    def test_brief_can_be_switched_off(self):
        out, _ = run_monitor("ok", brief="1")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertRegex(out.strip(), GATE)

    def test_failed_read_is_one_fixed_line(self):
        first, err1 = run_monitor("error", seconds="20")
        second, err2 = run_monitor("error", seconds="37")
        self.assertEqual(first, second, "a varying error string must not move the hash")
        self.assertEqual(first.strip().splitlines()[0], "unread=254 digest=unavailable")
        self.assertEqual(first.strip().splitlines()[1], "brief: unavailable")
        for out, err in ((first, err1), (second, err2)):
            self.assertNotIn("Traceback", out + err)
            self.assertNotIn("Exception ignored", out + err)

    def test_dead_client_still_prints_a_gate_line(self):
        out, err = run_monitor("die")
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("Exception ignored", out + err)
        self.assertEqual(err.strip(), "")
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[0], "unread=? digest=unavailable")
        self.assertEqual(lines[1], "brief: unavailable (no answer)")
        # a second run of the same failure must be byte-identical, or every tick wakes the agent
        # The death races our write (BrokenPipeError vs an empty read); run it enough times to
        # meet both sides of the race.
        for _ in range(15):
            self.assertEqual(out, run_monitor("die")[0])


if __name__ == "__main__":
    unittest.main()
