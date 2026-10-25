"""scripts/brief.py and scripts/digest.py, against a fake Hermes home for each seat.

A fake `hermes` stands in for the CLI: `cron notepad <id> set|delete` writes the real notepad schema in
$HERMES_HOME/cron/notepad.db, and `kanban ... list --json` prints a fixture. Offline; nothing else runs.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEF = os.path.join(ROOT, "scripts", "brief.py")
DIGEST = os.path.join(ROOT, "scripts", "digest.py")

FAKE_HERMES = r'''#!%s
import json, os, sqlite3, sys
a = sys.argv[1:]
if a[:2] == ["cron", "notepad"]:
    job, action = a[2], a[3]
    db = os.path.join(os.environ["HERMES_HOME"], "cron", "notepad.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS cron_notepad (job_id TEXT NOT NULL, key TEXT NOT NULL, "
                "value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (job_id, key))")
    if action == "set":
        con.execute("INSERT OR REPLACE INTO cron_notepad VALUES (?, ?, ?, 'now')", (job, a[4], a[5]))
    elif action == "delete":
        con.execute("DELETE FROM cron_notepad WHERE job_id = ? AND key = ?", (job, a[4]))
    con.commit()
    with open(os.path.join(os.environ["HERMES_HOME"], "calls.log"), "a") as f:
        f.write(" ".join(a) + "\n")
elif a[0] == "kanban":
    if os.environ.get("FAKE_KANBAN_FAIL"):
        sys.exit(2)
    print(open(os.environ["FAKE_TASKS"]).read())
''' % sys.executable


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Homes(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.state = os.path.join(self.base, ".local", "state", "delvetown-dt")
        os.makedirs(self.state)
        shutil.copy(os.path.join(ROOT, "seats.json"), self.state)
        self.akai = os.path.join(self.base, ".hermes")
        self.lock = os.path.join(self.base, ".hermes", "profiles", "locksmith")
        self.jobs(self.akai, [
            {"id": "gate1", "name": "gate", "script": "delvetown_monitor.sh", "prompt": "read the brief",
             "enabled": True},
            {"id": "wake1", "name": "clock wake", "prompt": "run scripts/dt.py recent, then..."},
            {"id": "meditate", "name": "meditation", "prompt": "tidy memory"},
            {"id": "disk", "name": "disk watch", "no_agent": True, "script": "disk.sh", "prompt": "dt.py"},
        ])
        self.jobs(self.lock, [
            {"id": "floor1", "name": "floor wake", "prompt": "dt.py feed"},
            {"id": "gate2", "name": "gate", "script": "delvetown_monitor.sh", "enabled": False, "state": "paused"},
        ])
        self.bin = os.path.join(self.base, "hermes")
        with open(self.bin, "w") as f:
            f.write(FAKE_HERMES)
        os.chmod(self.bin, 0o755)
        self.tasks = os.path.join(self.base, "tasks.json")
        self.write_tasks([])

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def jobs(self, home, jobs):
        os.makedirs(os.path.join(home, "cron"), exist_ok=True)
        with open(os.path.join(home, "cron", "jobs.json"), "w") as f:
            json.dump({"jobs": jobs}, f)

    def write_tasks(self, tasks):
        with open(self.tasks, "w") as f:
            json.dump(tasks, f)

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("DT_", "HERMES"))}
        e.update(HOME=self.base, HERMES_REAL_HOME=self.base, HERMES_BIN=self.bin, FAKE_TASKS=self.tasks,
                 HERMES_PROFILE="locksmith")         # set on purpose: it must not decide where notes go
        e.update(kw)
        return e

    def brief(self, *args, **env):
        r = subprocess.run([sys.executable, BRIEF, *args], env=self.env(**env), capture_output=True, text=True,
                           timeout=60)
        return r

    def notes(self, home):
        try:
            con = sqlite3.connect(os.path.join(home, "cron", "notepad.db"))
            rows = con.execute("SELECT job_id, key, value FROM cron_notepad").fetchall()
            con.close()
        except sqlite3.Error:
            return {}
        return {(j, k): v for j, k, v in rows}


class Brief(Homes):

    def test_one_command_reaches_every_delvetown_job_of_both_seats_and_nothing_else(self):
        r = self.brief("set", "tangled", "not set up yet: say we will check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.notes(self.akai), {("gate1", "tangled"): "not set up yet: say we will check",
                                                 ("wake1", "tangled"): "not set up yet: say we will check"})
        self.assertEqual(set(self.notes(self.lock)), {("floor1", "tangled"), ("gate2", "tangled")})

    def test_a_seat_can_be_briefed_alone(self):
        self.brief("set", "quiz", "tally due Friday", "--seat", "akai")
        self.assertEqual(set(self.notes(self.akai)), {("gate1", "quiz"), ("wake1", "quiz")})
        self.assertEqual(self.notes(self.lock), {})

    def test_until_is_written_into_the_line_and_a_passed_date_is_pruned(self):
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        self.brief("set", "soon", "short-lived", "--until", tomorrow)
        self.assertEqual(self.notes(self.akai)[("gate1", "soon")], "short-lived [until %s]" % tomorrow)
        con = sqlite3.connect(os.path.join(self.akai, "cron", "notepad.db"))
        con.execute("UPDATE cron_notepad SET value = 'old [until 2020-01-01]' WHERE key = 'soon'")
        con.commit()
        con.close()
        out = json.loads(self.brief("list").stdout)
        self.assertIn("akai:gate1 soon", out["pruned"])
        self.assertNotIn(("gate1", "soon"), self.notes(self.akai))

    def test_a_date_already_passed_is_refused(self):
        r = self.brief("set", "late", "x", "--until", "2020-01-01")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.notes(self.akai), {})

    def test_clear_removes_it_everywhere(self):
        self.brief("set", "tangled", "x")
        self.brief("clear", "tangled")
        self.assertEqual((self.notes(self.akai), self.notes(self.lock)), ({}, {}))

    def test_list_names_the_jobs_it_chose(self):
        self.brief("set", "tangled", "x")
        out = json.loads(self.brief("list").stdout)
        self.assertEqual([r["job"] for r in out["targets"]["akai"]], ["gate", "clock wake"])
        self.assertEqual(out["targets"]["akai"][0]["notes"], {"tangled": "x"})


class Digest(Homes):

    def digest(self, **env):
        r = subprocess.run([sys.executable, DIGEST], env=self.env(**env), capture_output=True, text=True,
                           timeout=60, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def log(self, name, entries):
        with open(os.path.join(self.state, name), "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_open_tasks_oldest_first_with_the_stale_marked(self):
        now = time.time()
        self.write_tasks([
            {"id": "t_new", "title": "check the PR", "status": "ready", "created_at": now - 3600},
            {"id": "t_old", "title": "skill trim", "status": "todo", "created_at": now - 5 * 86400},
            {"id": "t_done", "title": "finished", "status": "done", "created_at": now - 86400},
        ])
        out = self.digest()
        self.assertIn("board delvetown: 2 open", out)
        self.assertLess(out.index("t_old"), out.index("t_new"))
        self.assertIn("! t_old [todo, 5d] skill trim", out)
        self.assertNotIn("t_done", out)

    def test_the_briefing_shows_what_the_crons_carry_and_what_expired(self):
        self.brief("set", "tangled", "not set up")
        con = sqlite3.connect(os.path.join(self.akai, "cron", "notepad.db"))
        con.execute("INSERT INTO cron_notepad VALUES ('wake1', 'stale', 'gone [until 2020-01-01]', 'x')")
        con.commit()
        con.close()
        out = self.digest()
        self.assertIn("- tangled: not set up (4 jobs)", out)
        self.assertIn("! stale: gone [until 2020-01-01] (1 job) EXPIRED", out)

    def test_each_seat_s_window_with_a_double_answer_and_the_gate(self):
        now = time.time()
        ask = "at://did:plc:selene/town.delve.feed.post/3ask"
        self.log("publish-log.jsonl", [
            {"ts": iso(now - 3600), "kind": "reply", "uri": "at://did:plc:me/p/1", "parent_uri": ask},
            {"ts": iso(now - 1800), "kind": "reply", "uri": "at://did:plc:me/p/2", "parent_uri": ask},
            {"ts": iso(now - 900), "kind": "abstain", "subject": "at://did:plc:x/p/9"},
            {"ts": iso(now - 2 * 86400), "kind": "post", "uri": "at://did:plc:me/p/0"},
        ])
        with open(os.path.join(self.state, "wake-akai.json"), "w") as f:
            json.dump({"pending": {"u": {"t": now - 1200, "item": {}}}, "fired_at": now - 7200,
                       "last": "wake=2 reply=2 id=x\nprevious batch of 3: 1 answered, 0 declined, 2 abstained, "
                               "0 untouched"}, f)
        out = self.digest()
        self.assertIn("akai, last 12h: 2 reply; closed 1 abstain", out)
        self.assertIn("! double answer to 3ask: 1 and 2", out)
        self.assertIn("gate on; 1 waiting (oldest 20m); last wake 2h ago; previous batch of 3:", out)
        self.assertIn("skillissue, last 12h: wrote nothing", out)
        self.assertIn("gate off", out)

    def test_the_gate_is_found_when_its_command_sits_in_monitor_script(self):
        """A running interval gate keeps its command in monitor_script; reading only `script` printed "gate ?"."""
        jobs = json.load(open(os.path.join(self.akai, "cron", "jobs.json")))
        for j in jobs["jobs"]:
            if j.get("script"):
                j["monitor_script"] = j.pop("script")
        with open(os.path.join(self.akai, "cron", "jobs.json"), "w") as f:
            json.dump(jobs, f)
        out = self.digest()
        self.assertIn("gate on;", out)
        self.assertNotIn("gate ?;", out)

    def test_a_decline_later_abstained_is_counted_once_as_an_abstention(self):
        now = time.time()
        ask = "at://did:plc:kaiasky/town.delve.feed.post/3ask"
        self.log("publish-log.jsonl", [
            {"ts": iso(now - 3600), "kind": "decline", "subject": ask, "why": "repo lane"},
            {"ts": iso(now - 1800), "kind": "abstain", "subject": ask, "why": "repo lane",
             "replaced_decline": True},
        ])
        out = self.digest()
        self.assertIn("closed 1 abstain", out)
        self.assertNotIn("decline", out.split("akai, last 12h")[1].splitlines()[0])

    def test_thread_traffic_is_reported_with_an_excerpt_and_not_as_an_ask(self):
        now = time.time()
        rows = [{"uri": "at://x/y/%d" % i, "author": "sable.delve.town", "text": "hop %d in the chain" % i,
                 "reason": "reply", "lane": "threads", "seen_at": now - 600 * i} for i in range(4)]
        with open(os.path.join(self.state, "threads-akai.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = self.digest()
        self.assertIn("threads: 4 not addressed here", out)
        self.assertIn("@sable 'hop 0 in the chain'", out, "newest first")
        self.assertNotIn("! threads", out, "traffic is not an alarm")

    def test_no_thread_traffic_means_no_line(self):
        self.assertNotIn("threads:", self.digest())

    def test_the_forge_line_counts_the_queue_and_names_the_newest(self):
        now = time.time()
        rows = [
            {"at": iso(now - 600), "uri": "at://d/sh.tangled.feed.comment/1", "collection": "sh.tangled.feed.comment",
             "text": "Applied the branch, 28 pass"},
            {"at": iso(now - 900), "uri": "at://d/sh.tangled.repo.pull/2", "collection": "sh.tangled.repo.pull",
             "title": "Store the deck source repo-relative"},
            {"at": iso(now - 900), "uri": "at://d/sh.tangled.repo.pull/2", "collection": "sh.tangled.repo.pull",
             "title": "Store the deck source repo-relative"},
        ]
        with open(os.path.join(self.state, "tangled-events.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = self.digest()
        self.assertIn("forge, last 12h: 2 events — 1 comment, 1 pull", out)
        self.assertIn("comment 'Applied the branch, 28 pass'", out)
        self.assertEqual(out.count("forge, last 12h"), 1, "the forge is not a seat's: one line per digest")

    def test_an_event_with_no_text_is_named_not_quoted_empty(self):
        now = time.time()
        rows = [{"at": iso(now - 300), "uri": "at://d/sh.tangled.graph.profile/1",
                 "collection": "sh.tangled.graph.profile"}]
        with open(os.path.join(self.state, "tangled-events.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = self.digest()
        self.assertIn("newest: profile", out)
        self.assertNotIn("event ''", out)

    def test_a_dead_consumer_is_loud_rather_than_quiet(self):
        """Liveness is the cursor, not the queue: an old queue only means a quiet repo."""
        path = os.path.join(self.state, "tangled-events.jsonl")
        with open(path, "w") as f:
            f.write("")
        stale = time.time() - 6 * 3600
        os.utime(path, (stale, stale))
        cursor = os.path.join(self.state, "tangled-cursor")
        with open(cursor, "w") as f:
            f.write("x")
        os.utime(cursor, (stale, stale))
        out = self.digest()
        self.assertIn("! forge: consumer stalled — cursor untouched for", out)

    def test_a_quiet_repo_with_a_live_consumer_is_not_an_alarm(self):
        path = os.path.join(self.state, "tangled-events.jsonl")
        with open(path, "w") as f:
            f.write("")
        stale = time.time() - 5 * 3600
        os.utime(path, (stale, stale))
        cursor = os.path.join(self.state, "tangled-cursor")
        with open(cursor, "w") as f:
            f.write("x")
        out = self.digest()
        self.assertIn("forge: quiet (consumer alive", out)
        self.assertNotIn("! forge", out)

    def test_a_board_that_will_not_answer_costs_one_line(self):
        out = self.digest(FAKE_KANBAN_FAIL="1")
        self.assertIn("board: unavailable", out)
        self.assertIn("akai, last 12h", out, "the rest of the digest still arrives")

    def test_without_seats_it_says_so_in_one_line(self):
        os.remove(os.path.join(self.state, "seats.json"))
        moved = os.path.join(self.base, "digest.py")
        shutil.copy(DIGEST, moved)
        r = subprocess.run([sys.executable, moved], env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("seats: unavailable", r.stdout)
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
