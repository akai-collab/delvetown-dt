#!/usr/bin/env python3
"""Tell every delvetown cron one thing, in one command: a line in each job's Hermes notepad.

The notepad is put in front of every run of its job (verified 2026-10-24), which makes it the channel for
"what is going on right now" — something settled in a session that the next wake has to know. But it is
per job, and the same fact belongs on the gate, the clock wakes and today's one-shots of both seats, so by
hand it is eight `hermes cron notepad` calls, each needing the right HERMES_HOME (HERMES_PROFILE does not
move the store; the first attempt wrote to the wrong profile's database).

    brief.py set tangled "not set up yet: say we will check, do not describe repos" --until 2026-10-31
    brief.py list
    brief.py clear tangled
    brief.py set quiz "tally due Friday" --seat akai

Targets are read from each seat's live jobs.json on every call, never from a list of ids: a one-shot
removes itself after it fires and takes its notepad with it. A delvetown job is one whose prompt names
dt.py or whose script is the monitor; `list` prints which jobs those are, so the choice can be checked.
An `--until` date is written into the line itself (`[until 2026-10-31]`), so the run reading it sees the
date too; every call prunes lines whose date has passed.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

BASE = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
STATE = os.path.join(BASE, ".local", "state", "delvetown-dt")
HERE = os.path.dirname(os.path.abspath(__file__))
UNTIL = re.compile(r"\s*\[until (\d{4}-\d{2}-\d{2})\]\s*$")


def load_seats():
    for p in (os.environ.get("DT_SEATS_FILE"), os.path.join(STATE, "seats.json"),
              os.path.join(os.path.dirname(HERE), "seats.json")):
        if p and os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["seats"]
    sys.exit("no seats.json — run install-monitor.sh")


def hermes_home(seat):
    profile = seat.get("profile")
    return os.path.join(BASE, ".hermes", "profiles", profile) if profile else os.path.join(BASE, ".hermes")


def delvetown_jobs(home):
    """[(id, name)] of the jobs in this Hermes home that a briefing should reach."""
    try:
        with open(os.path.join(home, "cron", "jobs.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = []
    for j in jobs or []:
        if not isinstance(j, dict) or j.get("no_agent"):
            continue                       # no model reads a script-only job's prompt
        if "dt.py" in str(j.get("prompt") or "") or "delvetown_monitor" in str(j.get("script") or ""):
            out.append((j.get("id"), j.get("name") or j.get("id")))
    return out


def hermes(home, *args):
    """Run the hermes CLI against one seat's store. The CLI owns the database; this never writes it."""
    env = dict(os.environ, HERMES_HOME=home)
    env.pop("HERMES_PROFILE", None)
    return subprocess.run([os.environ.get("HERMES_BIN") or "hermes", *args], env=env, capture_output=True,
                          text=True, timeout=60)


def notes(home):
    """{(job_id, key): value} read straight from the notepad database, read-only."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(home, "cron", "notepad.db"), uri=True)
        rows = con.execute("SELECT job_id, key, value FROM cron_notepad").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {(j, k): v for j, k, v in rows}


def expired(value, today):
    m = UNTIL.search(value or "")
    return bool(m) and m.group(1) < today


def main(argv=None):
    ap = argparse.ArgumentParser(description="One briefing line for every delvetown cron.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("text")
    s.add_argument("--until", help="YYYY-MM-DD: the line is pruned once this date has passed")
    c = sub.add_parser("clear")
    c.add_argument("key")
    sub.add_parser("list")
    for p in (s, c):
        p.add_argument("--seat", help="a seat key from seats.json; all seats when omitted")
    args = ap.parse_args(argv)
    cmd = args.cmd or "list"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cmd == "set" and args.until and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.until):
        sys.exit("--until wants YYYY-MM-DD")
    if cmd == "set" and args.until and args.until < today:
        sys.exit("--until %s has already passed" % args.until)

    seats = load_seats()
    wanted = getattr(args, "seat", None)
    if wanted and wanted not in seats:
        sys.exit("no seat %r in seats.json (%s)" % (wanted, ", ".join(seats)))
    report, failed = {"pruned": [], "targets": {}}, False
    for name, seat in seats.items():
        home = hermes_home(seat)
        jobs = delvetown_jobs(home)
        existing = notes(home)
        for (job_id, key), value in existing.items():           # prune on every call
            if expired(value, today) and any(job_id == j for j, _ in jobs):
                if hermes(home, "cron", "notepad", job_id, "delete", key).returncode == 0:
                    report["pruned"].append("%s:%s %s" % (name, job_id, key))
        if wanted and name != wanted:
            continue
        rows = []
        for job_id, job_name in jobs:
            if cmd == "set":
                value = args.text + (" [until %s]" % args.until if args.until else "")
                r = hermes(home, "cron", "notepad", job_id, "set", args.key, value)
                failed |= r.returncode != 0
                rows.append({"job": job_name, "id": job_id, "set": r.returncode == 0})
            elif cmd == "clear":
                if (job_id, args.key) in existing:
                    r = hermes(home, "cron", "notepad", job_id, "delete", args.key)
                    failed |= r.returncode != 0
                    rows.append({"job": job_name, "id": job_id, "cleared": r.returncode == 0})
            else:
                rows.append({"job": job_name, "id": job_id,
                             "notes": {k: v for (j, k), v in notes(home).items() if j == job_id}})
        report["targets"][seat.get("name") or name] = rows
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
