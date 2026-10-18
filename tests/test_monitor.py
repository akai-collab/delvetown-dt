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
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
GATE = re.compile(r"^unread=\S+ digest=\S+$")


def run_monitor(mode="ok", seconds="20", brief=None, extra=None):
    env = dict(os.environ)
    env.update({
        "AT_MCP_BIN": sys.executable,
        "AT_MCP_BIN_ARGS": os.path.join(ROOT, "tests", "fake_at_mcp.py"),
        "DELVETOWN_ENV_FILE": "/dev/null",
        "AT_MCP_HANDLE": "test.delve.town",
        "DT_MONITOR_READER": "at_mcp",
        "FAKE_MODE": mode,
        "FAKE_SECONDS": seconds,
        "PYTHONPATH": os.path.join(ROOT, "tests"),
    })
    env.pop("AT_MCP_MONITOR_BRIEF", None)
    if brief is not None:
        env["AT_MCP_MONITOR_BRIEF"] = brief
    env.update(extra or {})
    with tempfile.TemporaryDirectory() as state:
        env["AT_MCP_MONITOR_STATE"] = state
        proc = subprocess.run([sys.executable, MONITOR], env=env, capture_output=True, text=True,
                              timeout=60)
    return proc.stdout, proc.stderr


class Monitor(unittest.TestCase):

    def test_the_monitors_own_knobs_do_not_reach_the_client(self):
        """Our opt-ins are AT_MCP_-prefixed, and at_mcp refuses to boot on an unknown AT_MCP_* setting,
        so a monitor that passes its whole environment through kills the client it reads with. That is
        what the gate's own documented opt-in did on the machine: every tick said "no answer" while the
        digest, with no such variable set, worked. The fake client now refuses the same way it does."""
        for key, value in (("AT_MCP_MONITOR_GATE", "wake"), ("AT_MCP_MONITOR_BRIEF", "1"),
                           ("AT_MCP_WAKE_SETTLE_S", "30")):
            out, err = run_monitor("ok", extra={key: value})
            first = (out.strip().splitlines() or [""])[0]
            self.assertNotIn("unavailable", first,
                             "%s reached the client and it refused to boot: %r" % (key, first))
            self.assertTrue(first.startswith(("wake=", "unread=")), first)


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


class Paths(unittest.TestCase):
    """The monitor reads the same state files dt.py writes, named for the account it watches. Two
    accounts can share a machine; a monitor that read akai's session cache while watching another
    would hold that account's wake for a lockout that was never its own, and miss its own."""

    ENV = ("AT_MCP_WAKE_STATE", "DT_SESSION_CACHE", "DT_PUBLISH_LOG", "DT_MONITOR_SESSION")

    def module_for(self, handle, extra=None):
        """Load the monitor fresh, with the per-account path vars cleared unless the test sets one."""
        import importlib.util
        saved = {k: os.environ.get(k) for k in self.ENV + ("AT_MCP_HANDLE",)}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["AT_MCP_HANDLE"] = handle
        os.environ.update(extra or {})
        try:
            spec = importlib.util.spec_from_file_location("mon_" + handle.split(".")[0], MONITOR)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return mod

    def test_akai_keeps_the_bare_names(self):
        m = self.module_for("akai.delve.town")
        self.assertTrue(m.SESSION_CACHE.endswith("/session.json"), m.SESSION_CACHE)
        self.assertTrue(m.PUBLISH_LOG.endswith("/publish-log.jsonl"), m.PUBLISH_LOG)
        self.assertTrue(m.WAKE_STATE.endswith("/wake-akai.json"), m.WAKE_STATE)

    def test_another_account_gets_its_own_client_paths(self):
        m = self.module_for("skillissue.delve.town")
        self.assertTrue(m.SESSION_CACHE.endswith("/session-skillissue.json"), m.SESSION_CACHE)
        self.assertTrue(m.PUBLISH_LOG.endswith("/publish-log-skillissue.jsonl"), m.PUBLISH_LOG)
        self.assertTrue(m.WAKE_STATE.endswith("/wake-skillissue.json"), m.WAKE_STATE)
        self.assertTrue(m.MONITOR_SESSION.endswith("/monitor-session-skillissue.json"), m.MONITOR_SESSION)

    def test_an_explicit_path_still_wins(self):
        m = self.module_for("skillissue.delve.town",
                            extra={"DT_PUBLISH_LOG": "/tmp/explicit-log.jsonl"})
        self.assertEqual(m.PUBLISH_LOG, "/tmp/explicit-log.jsonl")


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

    def test_a_held_reply_survives_scrolling_out_of_the_window(self):
        """Twenty-five likes during a lockout push the reply out of what the monitor reads; the reply
        must still wake once the hold lifts."""
        _, st = self.tick([note("r1")], {}, 0)
        _, st = self.tick([note("r1")], st, 700, held="rate-limited until 03:45Z")
        likes = [note("l%d" % i, "like") for i in range(25)]
        out, st = self.tick(likes, st, 800, held="rate-limited until 03:45Z")
        self.assertEqual(out, "wake=none")
        out, st = self.tick(likes, st, 5000)
        self.assertTrue(out.startswith("wake=1 reply=1"), out)
        self.assertIn("r1: hullo r1", out)
        again, st = self.tick(likes, st, 9000)
        self.assertEqual(again, out, "and it wakes once")

    def test_pending_state_from_before_items_were_kept_still_fires(self):
        out, st = self.tick([note("r1")], {"pending": {note("r1")["uri"]: 0}}, 700)
        self.assertTrue(out.startswith("wake=1"), out)

    def test_an_unanswered_pending_entry_expires(self):
        _, st = self.tick([note("r1")], {}, 0, held="x")
        _, st = self.tick([], st, self.m.FIRED_TTL_S + 1, held="x")
        self.assertEqual(st["pending"], {})

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
        "DT_MONITOR_READER": "at_mcp",
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


def _jwt(exp):
    import base64
    import json
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return "%s.%s.sig" % (enc({"alg": "none"}), enc({"exp": int(exp), "jti": os.urandom(6).hex()}))


class FakeTown:
    """A PDS and an appview in one local HTTP server, counting what the monitor asks for.

    `login` is what createSession answers (200, 401 or 429); `access_ttl` and `refresh_ttl` set the
    lifetime of the tokens it hands out, so a test can make the next tick find them expired.
    """
    PASSWORD = "test-pass-word-1234"

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.calls, self.login, self.access_ttl, self.refresh_ttl = [], 200, 3600, 86400
        self.reject_access = set()
        town = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _pair(self):
                now = time.time()
                return {"did": "did:plc:test", "handle": "test.delve.town",
                        "accessJwt": _jwt(now + town.access_ttl),
                        "refreshJwt": _jwt(now + town.refresh_ttl)}

            def _route(self):
                method = self.path.split("?")[0].rsplit(".", 1)[-1]
                town.calls.append(method)
                auth = (self.headers.get("Authorization") or "")[7:]
                if method == "createSession":
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if town.login == 429:
                        return self._send(429, {"error": "RateLimitExceeded"}, {"Retry-After": "900"})
                    if town.login != 200 or body.get("password") != town.PASSWORD:
                        return self._send(401, {"error": "AuthenticationRequired"})
                    return self._send(200, dict(self._pair(), echo=body.get("password")),
                                      {"RateLimit-Remaining": "299"})
                if method == "refreshSession":
                    return self._send(200, self._pair())
                if method == "getServiceAuth":
                    if auth in town.reject_access:
                        return self._send(401, {"error": "ExpiredToken"})
                    return self._send(200, {"token": "svc"})
                if method == "listNotifications":
                    return self._send(200, {"notifications": [
                        {"reason": "mention", "uri": "at://did:plc:a/town.delve.feed.post/3mxpaaaaaaa2f",
                         "cid": "bafyx", "isRead": False, "indexedAt": "2026-10-12T18:57:26.238Z",
                         "author": {"did": "did:plc:a", "handle": "selene.delve.town"},
                         "record": {"text": "Bot or not?", "facets": [{"did": "did:plc:zzz"}]}},
                        {"reason": "reply", "uri": "at://did:plc:b/town.delve.feed.post/3mxpbbbbbbb2f",
                         "isRead": False, "indexedAt": "2026-10-12T19:04:00.000Z",
                         "author": {"did": "did:plc:b", "handle": "carly.delve.town"},
                         "record": {"text": "quick answer under your post"}},
                        {"reason": "like", "uri": "at://did:plc:c/town.delve.feed.like/3mxpccccccc2f",
                         "isRead": True, "indexedAt": "2026-10-12T19:05:00.000Z",
                         "author": {"did": "did:plc:c", "handle": "dee.delve.town"}, "record": {}}]})
                if method == "getUnreadCount":
                    return self._send(200, {"count": 2})
                return self._send(501, {"error": "MethodNotImplemented"})

            do_GET = do_POST = _route

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def count(self, method):
        return self.calls.count(method)


class DirectReader(unittest.TestCase):
    """DT_MONITOR_READER=xrpc (the default): one login, kept. at_mcp logged in on every start, and a
    five-minute tick spent 288 of the account's 300 daily logins (live, 2026-10-17)."""

    def setUp(self):
        self.town = FakeTown()
        self.tmp = tempfile.mkdtemp()
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w") as f:
            f.write("AT_MCP_APP_PASSWORD=%s\n" % FakeTown.PASSWORD)
        self.cache = os.path.join(self.tmp, "monitor-session.json")
        self.dt_cache = os.path.join(self.tmp, "dt-session.json")

    def tearDown(self):
        import shutil
        self.town.close()
        shutil.rmtree(self.tmp)

    def tick(self, gate="digest", extra=None):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("AT_MCP_", "DT_")):
                del env[k]
        env.update({
            "DT_PDS": self.town.url, "DT_APPVIEW": self.town.url,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "DELVETOWN_ENV_FILE": self.env_file, "AT_MCP_HANDLE": "test.delve.town",
            "DT_MONITOR_SESSION": self.cache, "DT_SESSION_CACHE": self.dt_cache,
            "DT_PUBLISH_LOG": os.path.join(self.tmp, "publish-log.jsonl"),
            "AT_MCP_WAKE_STATE": os.path.join(self.tmp, "wake.json"), "AT_MCP_WAKE_SETTLE_S": "0",
            "AT_MCP_MONITOR_GATE": gate,
            # a reader that fell back to the binary would fail loudly rather than pass by accident
            "AT_MCP_BIN": "/nonexistent/at_mcp",
        })
        env.update(extra or {})
        proc = subprocess.run([sys.executable, MONITOR], env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.stderr, "", "nothing on stderr: the scheduler merges it into the hash")
        return proc.stdout

    def expire(self, *keys):
        import json
        with open(self.cache) as f:
            c = json.load(f)
        for k in keys:
            c[k] = _jwt(time.time() - 10)
        with open(self.cache, "w") as f:
            json.dump(c, f)

    def test_reads_the_town_in_the_same_shape_as_at_mcp(self):
        out = self.tick()
        lines = out.strip().splitlines()
        self.assertRegex(lines[0], GATE)
        self.assertEqual(lines[0].split()[0], "unread=2")
        self.assertTrue(lines[1].startswith("18:57Z mention @selene.delve.town 3mxpaaaaaaa2f: Bot or not?"),
                        lines[1])
        self.assertTrue(lines[2].startswith("19:04Z reply @carly.delve.town 3mxpbbbbbbb2f:"), lines[2])
        self.assertNotIn("did:plc:", out)

    def test_one_login_serves_every_later_tick(self):
        first = self.tick()
        for _ in range(4):
            self.assertEqual(self.tick(), first, "same town, same output: no wake")
        self.assertEqual(self.town.count("createSession"), 1)
        self.assertEqual(self.town.count("refreshSession"), 0)

    def test_an_expired_access_token_is_refreshed_not_logged_in_again(self):
        self.tick()
        self.expire("accessJwt")
        self.tick()
        self.assertEqual(self.town.count("createSession"), 1)
        self.assertEqual(self.town.count("refreshSession"), 1)

    def test_a_token_the_server_rejects_early_is_refreshed_once(self):
        import json
        self.tick()
        with open(self.cache) as f:
            self.town.reject_access.add(json.load(f)["accessJwt"])
        out = self.tick()
        self.assertTrue(out.startswith("unread=2 "), out)
        self.assertEqual(self.town.count("createSession"), 1)
        self.assertEqual(self.town.count("refreshSession"), 1)

    def test_logs_in_only_when_both_tokens_are_gone(self):
        self.tick()
        self.expire("accessJwt", "refreshJwt")
        self.tick()
        self.assertEqual(self.town.count("createSession"), 2)

    def test_a_rate_limited_login_is_not_asked_again_until_it_lifts(self):
        self.town.login = 429
        first = self.tick()
        self.assertIn("brief: unavailable (rate-limited)", first)
        self.assertEqual(self.tick(), first, "the same failure: no second wake")
        self.assertEqual(self.town.count("createSession"), 1, "the lockout is remembered locally")

    def test_a_wrong_password_is_not_retried_every_tick(self):
        with open(self.env_file, "w") as f:
            f.write("AT_MCP_APP_PASSWORD=wrong-wrong-wrong\n")
        first = self.tick()
        self.assertIn("brief: unavailable (login refused)", first)
        self.tick()
        self.assertEqual(self.town.count("createSession"), 1)

    def test_dt_pys_lockout_stops_the_monitor_logging_in(self):
        """The 300 a day is the account's: a login the monitor spends is one dt.py cannot."""
        import json
        with open(self.dt_cache, "w") as f:
            json.dump({"login_locked_until": time.time() + 900}, f)
        out = self.tick()
        self.assertIn("rate-limited", out)
        self.assertEqual(self.town.count("createSession"), 0)

    def test_the_password_is_never_printed_or_cached(self):
        out = self.tick()
        self.assertNotIn(FakeTown.PASSWORD, out)
        with open(self.cache) as f:
            cached = f.read()
        # the fake echoes the password in its login answer: only the token fields may be kept
        self.assertNotIn(FakeTown.PASSWORD, cached)
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o600)

    def test_no_password_means_no_login_attempt(self):
        with open(self.env_file, "w") as f:
            f.write("")
        self.assertIn("brief: unavailable (no password)", self.tick())
        self.assertEqual(self.town.count("createSession"), 0)

    def test_a_town_that_does_not_answer_is_one_fixed_line(self):
        self.town.close()
        out = self.tick(extra={"DT_PDS": "http://127.0.0.1:9", "DT_APPVIEW": "http://127.0.0.1:9"})
        self.assertEqual(out.strip(), "unread=? digest=unavailable\nbrief: unavailable (no answer)")
        self.town = FakeTown()      # tearDown closes it

    def test_the_wake_gate_through_the_direct_reader(self):
        first = self.tick("wake")
        self.assertTrue(first.startswith("wake=2 mention=1 reply=1 id="), first)
        self.assertIn("also: 1 like", first)
        self.assertEqual(self.tick("wake"), first, "nothing new: byte-identical, so no second wake")
        self.assertEqual(self.town.count("createSession"), 1)


if __name__ == "__main__":
    unittest.main()
