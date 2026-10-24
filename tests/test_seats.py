"""seats.json: one table both programs read, so they cannot disagree about a seat.

On 2026-10-24 they did: for skillissue, dt.py wrote its session cache as session-locksmith.json (named for
the account key) while the monitor looked for session-skillissue.json (named for the handle), so the
monitor never saw that seat's lockouts. The cases below hold dt.py and the deployed monitor to the same
files for every seat, and check that a missing table is a loud failure rather than a guess.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
SEATS = os.path.join(ROOT, "seats.json")
CLEARED = ("DT_ACCOUNT", "DT_HANDLE", "DT_PUBLISH_LOG", "DT_SESSION_CACHE", "DT_QUIET_FILE", "DT_CLAIMS_FILE",
           "AT_MCP_WAKE_STATE", "DT_MONITOR_SESSION", "DT_SEATS_FILE", "AT_MCP_HANDLE", "DELVETOWN_ENV_FILE",
           "DT_PACE_PAIR_S", "DT_PACE_THREAD_S", "DT_PACE_RESET_S")


def seats():
    with open(SEATS, encoding="utf-8") as f:
        return json.load(f)["seats"]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items() if k not in CLEARED}
        e.update(HOME=self.home, HERMES_REAL_HOME=self.home, DT_DRY_RUN="1")
        e.update(kw)
        return e

    def py(self, script, path, **env):
        out = subprocess.run([sys.executable, "-c", script, path], env=self.env(**env), capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def dt_paths(self, key, path=DT):
        return self.py(
            "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('d',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(json.dumps({'publish_log':m.PUBLISH_LOG,'session':m.SESSION_CACHE,'quiet':m.QUIET_FILE,"
            "'claims':m.CLAIMS_FILE,'handle':m.HANDLE,'did':m.DID,'quietyear':m.QUIETYEAR_REFUSAL}))",
            path, DT_ACCOUNT=key)

    def monitor_paths(self, handle, path=MONITOR, **env):
        return self.py(
            "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(json.dumps({'publish_log':m.PUBLISH_LOG,'session':m.SESSION_CACHE,'quiet':m.QUIET_FILE,"
            "'wake':m.WAKE_STATE,'monitor_session':m.MONITOR_SESSION,'env_file':m.ENV_FILE,"
            "'pace':[m.PACE_PAIR_S,m.PACE_THREAD_S,m.PACE_RESET_S],'seated':bool(m.SEAT)}))",
            path, AT_MCP_HANDLE=handle, **env)


class OneTable(Base):

    def test_both_programs_name_the_same_files_for_every_seat(self):
        for key, seat in seats().items():
            d, m = self.dt_paths(key), self.monitor_paths(seat["handle"])
            for kind in ("publish_log", "session", "quiet"):
                self.assertEqual(d[kind], m[kind], "%s: %s differs" % (key, kind))
                self.assertTrue(d[kind].endswith("/" + seat["files"][kind]), d[kind])
            self.assertTrue(m["wake"].endswith("/" + seat["files"]["wake"]))
            self.assertTrue(m["monitor_session"].endswith("/" + seat["files"]["monitor_session"]))
            self.assertEqual((d["handle"], d["did"]), (seat["handle"], seat["did"]))

    def test_a_profile_with_its_own_home_does_not_split_the_state_directory(self):
        """A profile can run with its own HOME. Both programs must still resolve HERMES_REAL_HOME first, or
        dt.py writes one directory and the monitor reads another."""
        other = tempfile.mkdtemp(dir=self.home)
        for key, seat in seats().items():
            d = self.py(
                "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('d',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "print(json.dumps([m.PUBLISH_LOG,m.SESSION_CACHE,m.QUIET_FILE]))", DT, DT_ACCOUNT=key, HOME=other)
            m = self.py(
                "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "print(json.dumps([m.PUBLISH_LOG,m.SESSION_CACHE,m.QUIET_FILE]))", MONITOR,
                AT_MCP_HANDLE=seat["handle"], HOME=other)
            self.assertEqual(d, m, key)
            self.assertTrue(all(p.startswith(self.home + "/.local/") for p in d), d)

    def test_nothing_moves_on_disk(self):
        """The table records the names in use when it was written: akai's bare names, and skillissue's
        publish log as its wrapper already named it."""
        s = seats()
        self.assertEqual(s["akai"]["files"]["publish_log"], "publish-log.jsonl")
        self.assertEqual(s["akai"]["files"]["session"], "session.json")
        self.assertEqual(s["locksmith"]["files"]["publish_log"], "publish-log-locksmith.jsonl")
        self.assertEqual(s["locksmith"]["files"]["wake"], "wake-skillissue.json")

    def test_a_seat_s_pace_is_its_own_and_the_environment_still_wins(self):
        pace = seats()["akai"]["pace"]
        self.assertEqual(self.monitor_paths("akai.delve.town")["pace"],
                         [pace["pair_s"], pace["thread_s"], pace["reset_s"]])
        self.assertEqual(self.monitor_paths("akai.delve.town", DT_PACE_PAIR_S="60")["pace"][0], 60)

    def test_only_akai_reads_akai_s_env_file(self):
        self.assertTrue(self.monitor_paths("akai.delve.town")["env_file"].endswith("/.hermes/.env"))
        self.assertEqual(self.monitor_paths("skillissue.delve.town")["env_file"], "")

    def test_a_handle_with_no_entry_keeps_the_names_derived_from_it(self):
        m = self.monitor_paths("test.delve.town")
        self.assertFalse(m["seated"])
        self.assertTrue(m["wake"].endswith("/wake-test.json"))
        self.assertEqual(m["env_file"], "")

    def test_the_quiet_year_rule_is_a_seat_switch(self):
        self.assertTrue(self.dt_paths("akai")["quietyear"])
        alt = os.path.join(self.home, "seats.json")
        s = seats()
        s["akai"]["quietyear_refusal"] = False
        with open(alt, "w", encoding="utf-8") as f:
            json.dump({"seats": s}, f)
        self.assertFalse(self.py(
            "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('d',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.QUIETYEAR_REFUSAL))",
            DT, DT_ACCOUNT="akai", DT_SEATS_FILE=alt))


class Missing(Base):
    """No table anywhere is a broken deploy: say so, never guess a seat."""

    def lone_copy(self, name):
        d = tempfile.mkdtemp(dir=self.home)
        shutil.copy(os.path.join(ROOT, name), d)
        return os.path.join(d, name)

    def test_the_state_directory_copy_is_found(self):
        state = os.path.join(self.home, ".local", "state", "delvetown-dt")
        os.makedirs(state)
        shutil.copy(SEATS, state)
        self.assertTrue(self.monitor_paths("akai.delve.town", path=self.lone_copy("delvetown_monitor.py"))["seated"])

    def test_the_monitor_prints_one_fixed_line(self):
        copy = self.lone_copy("delvetown_monitor.py")
        outs = [subprocess.run([sys.executable, copy], env=self.env(AT_MCP_HANDLE="akai.delve.town"),
                               capture_output=True, text=True, timeout=60) for _ in range(2)]
        self.assertTrue(outs[0].stdout.startswith("seats: unavailable"), outs[0].stdout)
        self.assertEqual(len(outs[0].stdout.strip().splitlines()), 1)
        self.assertEqual(outs[0].stdout, outs[1].stdout, "the same line every tick: one wake, not one per tick")
        self.assertEqual(outs[0].stderr, "")

    def test_dt_py_refuses(self):
        copy = self.lone_copy("dt.py")
        out = subprocess.run([sys.executable, copy, "provenance"], env=self.env(), capture_output=True,
                             text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no readable seats.json", out.stdout)


if __name__ == "__main__":
    unittest.main()
