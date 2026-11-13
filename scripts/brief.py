#!/usr/bin/env python3
"""Tell a seat's delvetown crons one thing, in one command: a line in each job's Hermes notepad.

The notepad is put in front of every run of its job (verified 2026-10-24), which makes it the channel for
"what is going on right now" — something settled in a session that the next wake has to know. But it is
per job, and the same fact belongs on that seat's gate, clock wakes and today's one-shots, so by hand it is
five `hermes cron notepad` calls, each needing the right HERMES_HOME (HERMES_PROFILE does not move the store;
the first attempt wrote to the wrong profile's database).

    brief.py set tangled "not set up yet: say we will check, do not describe repos" --seat akai --until 2026-10-31
    brief.py set quiz "tally due Friday" --seat akai
    brief.py list --seat akai
    brief.py clear tangled --seat akai

`set` REQUIRES a seat (`--seat <key>`, or `--all-seats` said out loud). An omitted seat used to mean "every
seat", which is how an akai-only fact lands in front of another seat's wakes: the sibling seat is a different
account with its own brief, and a note it did not ask for is worse than no note. The default made that mistake
the path of least resistance, so it is now an error with the two ways forward named in it.

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
        # A paused job never runs, so a briefing line written into it is written into nothing. The
        # store keeps it, `list` shows it, and the operator reads it as "the seat has been told" while
        # no future run of that job will ever see the line. Five such jobs existed on 2026-10-25 (the
        # pilot day, the beijing block, the loose-hour roller, the sydney one-shot, the presence probe).
        if j.get("enabled") is False or str(j.get("state") or j.get("status") or "").lower() in ("paused", "disabled"):
            continue
        # A one-shot job (a single turn opener, a dated postcard, a scheduled quiz) IS included when it is
        # a delvetown job. It was excluded for a night on 2026-11-11, to stop the notebook being scattered
        # across five jobs (the wake and gate held 20 lines each while two quiet-year openers held arbitrary
        # subsets of whatever existed the day they were created). The operator's objection is the better
        # one: a one-shot is rare and often *chosen* — "pick a thread and post" — and a run that picks a
        # thread without knowing which people and threads the seat is staying out of will pick wrong. So
        # they are targets again, and the scatter is fixed at the other end instead: rows for jobs that
        # have left jobs.json entirely are deleted on every call (see orphans() below), which is what
        # actually kept a row alive for a job that no longer existed.
        sched = j.get("schedule") if isinstance(j.get("schedule"), dict) else {}
        once = sched.get("kind") == "once"
        if "dt.py" in str(j.get("prompt") or "") or "delvetown_monitor" in str(j.get("script") or ""):
            out.append((j.get("id"), (j.get("name") or j.get("id")) + (" (one-shot)" if once else "")))
    return out


def all_job_ids(home):
    """Every job id this home still has: what an orphaned notepad row is measured against."""
    try:
        with open(os.path.join(home, "cron", "jobs.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return {j.get("id") for j in (jobs or []) if isinstance(j, dict)}


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


def _key_tokens(key):
    return {t for t in re.split(r"[^a-z0-9]+", key.lower()) if t and len(t) > 2}


def similar_keys(key, existing_keys):
    """Keys a run may be duplicating: same words in a different order or separator.

    Three duplicate pairs landed in one night (at-mcp-fork / at_mcp-fork, a stale copy of
    scratch-filenames, lexicon-c-mimo / lexicon-c-double), each written a minute apart by a run
    that did not look at what was already there. The warning is printed, not enforced.
    """
    mine = _key_tokens(key)
    out = []
    for other in existing_keys:
        if other == key:
            continue
        theirs = _key_tokens(other)
        if not mine or not theirs:
            continue
        overlap = len(mine & theirs) / float(len(mine | theirs))
        if overlap >= 0.5:
            out.append(other)
    return sorted(out)


def dates(home, seat):
    """One compact block per seat: note count, nearest expiry, and each key with its end date.

    The operator reads Telegram reports, not the notepad: without this she cannot tell whether a line
    that governs a run lapses tomorrow or in three weeks, and stale notes look identical to live ones.
    Keys are deduped across jobs; a key whose copies disagree is marked DIVERGENT so the divergence is
    visible rather than silently averaged.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(home, "cron", "notepad.db"), uri=True)
        rows = list(con.execute("select key, value, updated_at from cron_notepad"))
    except sqlite3.Error:
        print("notes: 0 (no notepad.db at %s)" % home)
        return
    per = {}
    for key, value, upd in rows:
        m = UNTIL.search(value or "")
        per.setdefault(key, set()).add((m.group(1) if m else "no end date", (value or "")[:120]))
    dated = sorted((sorted(v)[0][0], k) for k, v in per.items() if sorted(v)[0][0] != "no end date")
    print("notes: %d" % len(per))
    if dated:
        print("next expiry: %s %s" % (dated[0][1], dated[0][0]))
    for k in sorted(per, key=lambda k: (sorted(per[k])[0][0] == "no end date", sorted(per[k])[0][0], k)):
        vals = per[k]
        flag = "  DIVERGENT (%d copies)" % len(vals) if len(vals) > 1 else ""
        print("  %-24s %s%s" % (k, sorted(vals)[0][0], flag))


def sync(home, seat):
    """Push the newest value of every key to every target job, so no copy is frozen in the past."""
    jobs = delvetown_jobs(home)
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(home, "cron", "notepad.db"), uri=True)
    newest = {}
    for key, value, upd in con.execute("select key, value, updated_at from cron_notepad"):
        if key not in newest or upd > newest[key][0]:
            newest[key] = (upd, value)
    have = {(j, k) for j, k in con.execute("select job_id, key from cron_notepad")}
    written = 0
    for jid, _ in jobs:
        for key, (upd, value) in newest.items():
            cur = con.execute("select value from cron_notepad where job_id=? and key=?", (jid, key)).fetchone()
            if cur and cur[0] == value:
                continue
            if hermes(home, "cron", "notepad", jid, "set", key, value).returncode == 0:
                written += 1
    print("synced: %d row(s) across %d job(s), %d key(s)" % (written, len(jobs), len(newest)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="One briefing line for every delvetown cron.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("text")
    s.add_argument("--until", help="YYYY-MM-DD: the line is pruned once this date has passed")
    s.add_argument("--permanent", action="store_true",
                   help="undated note: operator-only. A run must pass --until; she promotes a dated line with live context")
    s.add_argument("--long", action="store_true",
                   help="allow an end date more than 14 days out, for a subject that has that date of its own")
    c = sub.add_parser("clear")
    c.add_argument("key")
    sy = sub.add_parser("sync")
    sy.add_argument("--seat", required=True)
    d = sub.add_parser("dates")
    d.add_argument("--seat", required=True)
    l = sub.add_parser("list")
    for p in (s, c, l):
        p.add_argument("--seat", help="a seat key from seats.json")
    for p in (s, c):
        p.add_argument("--all-seats", action="store_true",
                       help="do this to every seat (required for set when --seat is omitted)")
    l.add_argument("--all-seats", action="store_true", help="the default for list")
    args = ap.parse_args(argv)
    cmd = args.cmd or "list"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cmd == "set" and args.until and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.until):
        sys.exit("--until wants YYYY-MM-DD")
    if cmd == "set" and not args.until and not getattr(args, "permanent", False):
        sys.exit("an undated note needs --permanent, and that flag is the operator's to give (2026-11-13). "
                 "Runs write dated lines: pass --until YYYY-MM-DD (24-48h for a town happening, --long only "
                 "when the subject has that date of its own). She promotes one to undated with live context.")
    if cmd == "set" and args.until and args.until < today:
        sys.exit("--until %s has already passed" % args.until)
    if cmd == "set" and args.until and not args.long:
        days = (datetime.strptime(args.until, "%Y-%m-%d").date()
                - datetime.strptime(today, "%Y-%m-%d").date()).days
        if days > 14:
            # Months-long notes are how a briefing turns into a fossil: the town moves on in a day or two, and
            # every run after that reads an ended subject as current. A real date (a rota day, a signature's end)
            # is what --long is for; anything else gets 24-48 hours.
            sys.exit("--until %s is %d days out: a town happening gets 24-48 hours. Shorten it, or pass --long "
                     "if the subject has that date of its own." % (args.until, days))

    if cmd == "sync":
        seats = load_seats()
        if args.seat not in seats:
            sys.exit("no seat %r" % args.seat)
        sync(hermes_home(seats[args.seat]), args.seat)
        return 0

    if cmd == "dates":
        seats = load_seats()
        if args.seat not in seats:
            sys.exit("no seat %r" % args.seat)
        dates(hermes_home(seats[args.seat]), args.seat)
        return 0

    seats = load_seats()
    wanted = getattr(args, "seat", None)
    if cmd == "set" and not wanted and not getattr(args, "all_seats", False):
        sys.exit("set needs --seat <key> (or --all-seats): a line written to every seat is a note to a seat "
                 "that did not ask for it. seats: " + ", ".join(seats))
    if getattr(args, "all_seats", False):
        wanted = None
    if wanted and wanted not in seats:
        sys.exit("no seat %r in seats.json (%s)" % (wanted, ", ".join(seats)))
    report, failed = {"pruned": [], "targets": {}}, False
    for name, seat in seats.items():
        home = hermes_home(seat)
        jobs = delvetown_jobs(home)
        existing = notes(home)
        live = all_job_ids(home)
        for (job_id, key), value in existing.items():           # prune on every call
            if job_id not in live:
                # the job is gone from jobs.json: the row can never be read again, and leaving it makes
                # the seat look told. This is the real orphan fix; excluding one-shots was a proxy for it.
                if hermes(home, "cron", "notepad", job_id, "delete", key).returncode == 0:
                    report.setdefault("orphans", []).append("%s:%s %s" % (name, job_id, key))
                continue
            if expired(value, today) and any(job_id == j for j, _ in jobs):
                if hermes(home, "cron", "notepad", job_id, "delete", key).returncode == 0:
                    report["pruned"].append("%s:%s %s" % (name, job_id, key))
        if wanted and name != wanted:
            continue
        rows = []
        if cmd == "set":
            dupes = similar_keys(args.key, {k for (j, k) in existing})
            if dupes:
                report.setdefault("similar", {})[name] = dupes
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
        label = seat.get("name") or name
        # seats.json keys and display names differ for the sibling seat (locksmith / skillissue):
        # print both, so a target is never ambiguous about which store it was written into.
        report["targets"][name if label == name else "%s (%s)" % (name, label)] = rows
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
