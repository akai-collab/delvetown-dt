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


def load_monitor():
    """The monitor as a module: its logic is importable now, and only main() talks to at_mcp."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("delvetown_monitor", MONITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def note(rkey, reason="reply", author="carly.delve.town", at="2026-10-17T10:00:00.000Z"):
    return {"reason": reason, "author": author, "text": "hullo " + rkey, "indexed_at": at,
            "uri": "at://did:plc:zzz/town.delve.feed.post/" + rkey}


class WakeGate(unittest.TestCase):
    """AT_MCP_MONITOR_GATE=wake: the output changes only for a settled batch of things that asked
    something of the account, and only when the account can answer. The scheduler wakes the model on
    a change, so every 'do not wake' below is asserted as 'printed exactly what it printed last'."""

    def setUp(self):
        self.m = load_monitor()
        self.m.SETTLE_S, self.m.MAX_WAIT_S, self.m.FAIL_TICKS = 600, 2700, 3

    def tick(self, items, state, now, held=None):
        return self.m.wake_output(items, state, now, held)

    def test_a_like_or_a_follow_never_wakes(self):
        out, st = self.tick([note("a", "like"), note("b", "follow"), note("c", "repost")], {}, 0)
        out2, _ = self.tick([note("a", "like"), note("b", "follow"), note("c", "repost")], st, 5000)
        self.assertEqual(out, "wake=none")
        self.assertEqual(out2, "wake=none")

    def test_a_reply_waits_for_the_thread_to_settle(self):
        out, st = self.tick([note("r1")], {}, 0)
        self.assertEqual(out, "wake=none", "a reply that has just landed does not wake yet")
        out, st = self.tick([note("r1"), note("r2")], st, 400)        # the thread is still moving
        self.assertEqual(out, "wake=none")
        out, st = self.tick([note("r1"), note("r2")], st, 900)        # 500s since r2: not settled
        self.assertEqual(out, "wake=none")
        out, st = self.tick([note("r1"), note("r2")], st, 1000)       # 600s since r2: settled
        self.assertTrue(out.startswith("wake=2 reply=2 id="), out)
        self.assertIn("@carly.delve.town r1: hullo r1", out)
        self.assertIn("r2: hullo r2", out)

    def test_a_busy_thread_cannot_postpone_its_answer_for_ever(self):
        st, items = {}, []
        for i, now in enumerate(range(0, 2700, 300)):                 # a new reply every five minutes
            items.append(note("r%d" % i))
            out, st = self.tick(list(items), st, now)
            self.assertEqual(out, "wake=none")
        out, st = self.tick(items + [note("late")], st, 2700)
        self.assertTrue(out.startswith("wake="), "MAX_WAIT_S since the oldest forces the wake")

    def test_the_same_reply_never_wakes_twice(self):
        _, st = self.tick([note("r1")], {}, 0)
        fired, st = self.tick([note("r1")], st, 600)
        self.assertTrue(fired.startswith("wake=1"))
        again, st = self.tick([note("r1")], st, 99999)
        self.assertEqual(again, fired, "unchanged output: no second wake for the same reply")

    def test_reactions_ride_along_with_a_wake(self):
        _, st = self.tick([note("r1"), note("l1", "like"), note("l2", "like")], {}, 0)
        out, st = self.tick([note("r1"), note("l1", "like"), note("l2", "like")], st, 600)
        self.assertTrue(out.endswith("also: 2 like"), out)
        self.assertNotIn("l1:", out)

    def test_an_unknown_reason_wakes(self):
        """A notification kind this file has never seen is shown once, not dropped."""
        _, st = self.tick([note("x", "starterpack_joined")], {}, 0)
        out, _ = self.tick([note("x", "starterpack_joined")], st, 600)
        self.assertTrue(out.startswith("wake=1 starterpack_joined=1"), out)

    def test_a_held_batch_waits_and_is_not_dropped(self):
        _, st = self.tick([note("r1")], {}, 0)
        out, st = self.tick([note("r1")], st, 600, held="rate-limited until 03:45Z")
        self.assertEqual(out, "wake=none", "the account cannot answer: do not wake it to try")
        out, st = self.tick([note("r1")], st, 4000)
        self.assertTrue(out.startswith("wake=1"), "the hold lifted: the batch is still there")

    def test_one_failing_tick_is_silent_and_a_long_outage_wakes_once(self):
        _, st = self.tick([note("r1")], {}, 0)
        good, st = self.tick([note("r1")], st, 600)
        outs = []
        for now in range(700, 700 + 6 * 60, 60):
            out, st = self.tick(None, st, now)
            outs.append(out)
        self.assertEqual(outs[:2], [good, good], "the first failures print the last output again")
        self.assertTrue(outs[2].startswith("wake=unavailable"), outs[2])
        self.assertEqual(set(outs[3:]), {outs[2]}, "and the outage wakes exactly once")
        back, st = self.tick([note("r1")], st, 2000)
        self.assertEqual(back, outs[2], "recovering with nothing new is not a wake either")

    def test_handled_uris_expire(self):
        st = {"fired": {"at://old": 0, "at://new": 10 ** 8}}
        _, st = self.tick([], st, 10 ** 8 + 60)          # 14 days have long passed for "old"
        self.assertEqual(list(st["fired"]), ["at://new"])


class HeldReason(unittest.TestCase):
    """The account's own records say when a wake would be wasted: dt.py's lockout and write guard."""

    def setUp(self):
        self.m = load_monitor()
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "session.json")
        self.log = os.path.join(self.tmp, "publish-log.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def held(self, now):
        return self.m.held_reason(now, self.cache, self.log)

    def test_nothing_on_disk_is_not_a_hold(self):
        self.assertIsNone(self.held(1000))

    def test_a_recorded_lockout_holds_until_it_passes(self):
        import json
        for key in ("login_locked_until", "locked_until"):
            with open(self.cache, "w") as f:
                json.dump({key: 5000}, f)
            self.assertIn("rate-limited", self.held(1000))
            self.assertIsNone(self.held(5001))

    def test_a_full_write_guard_holds(self):
        import json
        from datetime import datetime, timezone
        now = 10 ** 9
        with open(self.log, "w") as f:
            for i in range(6):
                ts = datetime.fromtimestamp(now - 60 * i, timezone.utc).isoformat()
                f.write(json.dumps({"kind": "reply", "ts": ts}) + "\n")
            f.write(json.dumps({"kind": "like", "ts": ts}) + "\n")      # reactions do not count
        self.assertIn("write guard", self.held(now))
        self.assertIsNone(self.held(now + 3601))


def run_wake(mode, state_path, settle="0"):
    env = dict(os.environ)
    env.update({
        "AT_MCP_BIN": sys.executable,
        "AT_MCP_BIN_ARGS": os.path.join(ROOT, "tests", "fake_at_mcp.py"),
        "DELVETOWN_ENV_FILE": "/dev/null",
        "AT_MCP_HANDLE": "test.delve.town",
        "AT_MCP_MONITOR_GATE": "wake",
        "AT_MCP_WAKE_STATE": state_path,
        "AT_MCP_WAKE_SETTLE_S": settle,
        "DT_SESSION_CACHE": state_path + ".nocache",
        "DT_PUBLISH_LOG": state_path + ".nolog",
        "FAKE_MODE": mode,
    })
    with tempfile.TemporaryDirectory() as mcp_state:
        env["AT_MCP_MONITOR_STATE"] = mcp_state
        proc = subprocess.run([sys.executable, MONITOR], env=env, capture_output=True, text=True,
                              timeout=60)
    return proc.stdout, proc.stderr


class WakeGateEndToEnd(unittest.TestCase):
    """The same rules through the real entry point and the fake at_mcp."""

    def test_a_settled_reply_wakes_once_then_the_output_holds_still(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "wake.json")
            first, err = run_wake("ok", state)
            self.assertEqual(err, "")
            self.assertTrue(first.startswith("wake=2 mention=1 reply=1 id="), first)
            self.assertNotIn("did:plc:", first)
            second, _ = run_wake("ok", state)
            self.assertEqual(first, second, "nothing new: byte-identical output, so no second wake")

    def test_a_failing_client_does_not_wake_on_its_first_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "wake.json")
            first, _ = run_wake("ok", state)
            for mode in ("error", "die"):
                out, err = run_wake(mode, state)
                self.assertEqual(out, first, mode)
                self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
