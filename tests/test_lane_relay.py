"""The Quiet Year lane: set aside by the gate, handed to the session that owns it by scripts/lane_relay.py.

2026-10-24: the lane was filtered out of the gate (a cron cannot answer it) and so reached nobody; the operator
watched the town by hand for hours and told the session to check, again and again. The relay wakes that session
through Hermes' kanban notify+wake, once a burst has settled, and never asks the operator. A fake `hermes`
records every call; nothing else runs.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
RELAY = os.path.join(ROOT, "scripts", "lane_relay.py")
DIGEST = os.path.join(ROOT, "scripts", "digest.py")
T0 = 1792900000.0

FAKE = r'''#!%s
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(a) + "\n")
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("gateway not running\n"); sys.exit(1)
if "create" in a:
    print(json.dumps({"id": "t_lane1", "status": "ready"}))
''' % sys.executable


def load(path, name, env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def item(rkey, root="at://did:plc:kai/town.delve.feed.post/root1"):
    return {"uri": "at://did:plc:kai/town.delve.feed.post/" + rkey, "root": root, "reason": "reply",
            "author": "kaiasky.delve.town", "text": "pushed the frost card fix, review? " + rkey,
            "indexed_at": "2026-10-24T14:42:00.000Z"}


class Lane(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.state = os.path.join(self.base, ".local", "state", "delvetown-dt")
        os.makedirs(self.state)
        with open(os.path.join(ROOT, "seats.json")) as f:
            self.seats = json.load(f)
        # every fixture value is set here, not inherited from the live seats.json: chat_type is dm on
        # this host now, and a test that reads production drifted the moment the real target changed.
        self.target(chat_id="-100123", thread_id="77", chat_type="group")
        self.calls = os.path.join(self.base, "calls.log")
        self.bin = os.path.join(self.base, "hermes")
        with open(self.bin, "w") as f:
            f.write(FAKE)
        os.chmod(self.bin, 0o755)
        env = {"HERMES_REAL_HOME": self.base, "HERMES_BIN": self.bin, "FAKE_CALLS": self.calls}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in list(env) + ["FAKE_FAIL"]])
        self.relay = load(RELAY, "lane_relay", env)
        self.m = load(MONITOR, "mon_lane", dict(env, AT_MCP_HANDLE="akai.delve.town"))
        self.outbox = os.path.join(self.state, "lane-quietyear-akai.jsonl")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def target(self, **kw):
        self.seats["seats"]["akai"]["lanes"]["quietyear"]["wake"].update(kw)
        with open(os.path.join(self.state, "seats.json"), "w") as f:
            json.dump(self.seats, f)

    def arrive(self, items, now):
        return self.m.record_lane(items, now=now, path=self.outbox)

    def run_pass(self, now):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.relay.one_pass(now)
        return buf.getvalue()

    def called(self):
        try:
            with open(self.calls) as f:
                return [json.loads(l) for l in f]
        except OSError:
            return []


class TheGateSetsItAside(Lane):

    def test_a_lane_item_is_recorded_once_and_never_batched(self):
        self.m._SESSION_LANE_CACHE["at://did:plc:kai/town.delve.feed.post/root1"] = True
        keep, lane = self.m.split_session_lane([item("a"), {"uri": "at://x/y/z", "text": "hello"}])
        self.assertEqual([i["uri"] for i in keep], ["at://x/y/z"])
        self.assertEqual(self.arrive(lane, T0), 1)
        self.assertEqual(self.arrive(lane, T0 + 300), 0, "the next tick sees it again: still one line")
        with open(self.outbox) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual((len(rows), rows[0]["lane"], rows[0]["seen_at"]), (1, "quietyear", T0))


class TheRelayWakesTheSession(Lane):

    def test_nothing_waiting_does_nothing_and_says_nothing(self):
        self.assertEqual(self.run_pass(T0), "")
        self.assertEqual(self.called(), [])

    def test_a_burst_waits_until_it_settles_then_wakes_once(self):
        self.arrive([item("a")], T0)
        self.arrive([item("b")], T0 + 300)
        self.assertEqual(self.run_pass(T0 + 600), "", "b arrived 5 minutes ago: still moving")
        self.assertEqual(self.called(), [])
        self.assertEqual(self.run_pass(T0 + 900), "")
        calls = self.called()
        self.assertEqual([c[3] for c in calls], ["create", "notify-subscribe", "complete"])
        sub = calls[1]
        for flag, val in (("--platform", "telegram"), ("--chat-id", "-100123"), ("--thread-id", "77"),
                          ("--delivery-mode", "notify+wake"), ("--chat-type", "group")):
            self.assertEqual(sub[sub.index(flag) + 1], val)
        summary = calls[2][calls[2].index("--summary") + 1]
        self.assertIn("quietyear lane: 2 new", summary)
        self.assertIn("Nothing here needs the operator", summary)
        self.assertIn("frost card fix, review? a", summary)
        self.assertIn("https://delve.town/profile/did:plc:kai/post/b", summary)
        self.run_pass(T0 + 2000)
        self.assertEqual(len(self.called()), 3, "handed on once")

    def test_a_burst_that_never_stops_is_handed_on_at_max_wait(self):
        for i, t in enumerate(range(0, 1801, 300)):
            self.arrive([item("m%d" % i)], T0 + t)
        self.run_pass(T0 + 1800)
        self.assertEqual(len(self.called()), 3)

    def test_a_second_burst_waits_for_the_gap_then_carries_everything(self):
        """The floor between wakes: 50 minutes, and the next wake is the whole accumulation."""
        self.arrive([item("a")], T0)
        self.run_pass(T0 + 900)                      # settles, no previous wake: goes at once
        self.assertEqual(len(self.called()), 3)
        self.arrive([item("b")], T0 + 1000)
        self.arrive([item("c")], T0 + 1300)
        self.run_pass(T0 + 2000)                     # settled, but 18 minutes after the last wake
        self.assertEqual(len(self.called()), 3, "inside the gap nothing is handed on")
        self.run_pass(T0 + 3899)
        self.assertEqual(len(self.called()), 3, "one second short of 50 minutes: still held")
        self.run_pass(T0 + 3900)                     # 50 minutes after the wake at T0+900
        calls = self.called()
        self.assertEqual(len(calls), 6)
        summary = calls[5][calls[5].index("--summary") + 1]
        self.assertIn("quietyear lane: 2 new", summary)
        self.assertIn("review? b", summary)
        self.assertIn("review? c", summary)
        self.run_pass(T0 + 5000)
        self.assertEqual(len(self.called()), 6, "and once only")

    def test_the_gap_defaults_to_fifty_minutes(self):
        conf = {"settle_s": 600, "max_wait_s": 1800}
        settled = [{"seen_at": T0, "uri": "at://x/y/z"}]
        self.assertFalse(self.relay.due(settled, conf, T0 + 2900, last_wake=T0),
                         "settled, but inside the 50-minute floor")
        self.assertTrue(self.relay.due(settled, conf, T0 + 3000, last_wake=T0))

    def test_the_same_batch_has_the_same_idempotency_key(self):
        a = [item("a"), item("b")]
        key = lambda batch: self.relay.hashlib.md5("\n".join(sorted(i["uri"] for i in batch)).encode()).hexdigest()
        self.assertEqual(key(a), key(list(reversed(a))))

    def test_no_target_wakes_nobody_and_status_says_so(self):
        self.target(chat_id="")
        self.arrive([item("a")], T0)
        self.run_pass(T0 + 5000)
        self.assertEqual(self.called(), [])
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.relay.status()
        st = json.loads(buf.getvalue())["akai/quietyear"]
        self.assertEqual((st["waiting"], st["wakes"]), (1, "no target set in seats.json"))

    def test_pause_holds_and_resume_hands_on(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.relay.set_paused("quietyear", True)
        self.arrive([item("a")], T0)
        self.run_pass(T0 + 5000)
        self.assertEqual(self.called(), [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.relay.set_paused("quietyear", False)
        self.run_pass(T0 + 5300)
        self.assertEqual(len(self.called()), 3)

    def test_a_failure_is_reported_once_an_hour_and_retried(self):
        self.arrive([item("a")], T0)
        os.environ["FAKE_FAIL"] = "1"
        first = self.run_pass(T0 + 1000)
        self.assertIn("could not wake its session", first)
        self.assertEqual(self.run_pass(T0 + 1300), "", "not again within the hour")
        os.environ.pop("FAKE_FAIL")
        self.run_pass(T0 + 1600)
        self.assertEqual([c[3] for c in self.called()][-3:], ["create", "notify-subscribe", "complete"])


class TheDigestShowsTheLane(Lane):

    def test_a_lane_nobody_can_wake_is_marked(self):
        self.target(chat_id="")
        self.arrive([item("a")], T0)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DT_")}
        env.update(HOME=self.base, HERMES_REAL_HOME=self.base, HERMES_BIN=self.bin, FAKE_CALLS=self.calls)
        out = subprocess.run([sys.executable, DIGEST], env=env, capture_output=True, text=True, timeout=60).stdout
        self.assertIn("! quietyear lane: 0 handed on in 12h; 1 waiting", out)
        self.assertIn("NO WAKE TARGET", out)


if __name__ == "__main__":
    unittest.main()
