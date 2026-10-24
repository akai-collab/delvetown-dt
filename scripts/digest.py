#!/usr/bin/env python3
"""Twice a day, what is open: the board, the briefing the crons are carrying, and each seat's last 12 hours.

So the operator does not have to remember to look. Run as a Hermes no-agent cron job — no model, no tokens;
its stdout is the message — twice a day, morning and night:

    hermes cron create "30 8,20 * * *" --no-agent --script digest.py --deliver telegram --name delvetown-digest

It reads only local files and the board, and writes nothing. Every section that cannot be read says so in
one line instead of failing the run, because a digest that dies on one missing file shows nothing at all.

It is deployed beside the monitor (install-monitor.sh copies both into the profile's scripts dir) and
imports it from there, so double answers and closes are counted by the same functions the gate uses.
DT_DIGEST_HOURS sets the window (default 12); HERMES_BIN names the hermes executable for the board read.
"""
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HOURS = float(os.environ.get("DT_DIGEST_HOURS") or 12)
BOARD = os.environ.get("DT_DIGEST_BOARD") or "delvetown"
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
STATE = os.path.join(BASE, ".local", "state", "delvetown-dt")
STALE_TASK_DAYS = 3
UNTIL = re.compile(r"\s*\[until (\d{4}-\d{2}-\d{2})\]\s*$")   # what brief.py --until writes


def load_monitor():
    """The monitor deployed beside this file (or, run from the repo, the one in the repo root)."""
    for d in (HERE, os.path.dirname(HERE)):
        p = os.path.join(d, "delvetown_monitor.py")
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("delvetown_monitor", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("delvetown_monitor.py is not beside the digest")


def load_seats():
    for p in (os.environ.get("DT_SEATS_FILE"), os.path.join(STATE, "seats.json"),
              os.path.join(os.path.dirname(HERE), "seats.json")):
        if p and os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["seats"]
    raise RuntimeError("no seats.json — run install-monitor.sh")


def hermes_home(seat):
    """A seat's Hermes home: the default profile's, or profiles/<profile>. The notepad and the jobs live
    there (HERMES_PROFILE does not move them; HERMES_HOME does — found the hard way on 2026-10-24)."""
    profile = seat.get("profile")
    return os.path.join(BASE, ".hermes", "profiles", profile) if profile else os.path.join(BASE, ".hermes")


def load_jobs(home):
    with open(os.path.join(home, "cron", "jobs.json"), encoding="utf-8") as f:
        data = json.load(f)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in jobs or [] if isinstance(j, dict)]


def epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return "%dd" % (seconds // 86400)
    if seconds >= 3600:
        return "%dh" % (seconds // 3600)
    return "%dm" % (seconds // 60)


def board_section(now):
    """Open tasks on the board, oldest first, with the stale ones marked."""
    cmd = [os.environ.get("HERMES_BIN") or "hermes", "kanban", "--board", BOARD, "list", "--json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            # never "nothing open": a board that did not answer must not read as a clear board
            return ["board: unavailable (hermes kanban exited %d)" % out.returncode]
        tasks = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ["board: unavailable (%s)" % type(exc).__name__]
    if not isinstance(tasks, list):
        return ["board: unavailable (unexpected answer)"]
    tasks = [t for t in tasks if isinstance(t, dict) and t.get("status") not in ("done", "archived")]
    if not tasks:
        return ["board %s: nothing open" % BOARD]
    lines = ["board %s: %d open" % (BOARD, len(tasks))]
    for t in sorted(tasks, key=lambda t: t.get("created_at") or 0):
        created = t.get("created_at")
        made = float(created) if isinstance(created, (int, float)) else epoch(created)
        stale = made and now - made > STALE_TASK_DAYS * 86400
        lines.append("  %s %s [%s%s] %s" % ("!" if stale else "-", t.get("id"), t.get("status"),
                                            ", " + age(now - made) if made else "", t.get("title")))
    return lines


def briefing_section(seats, today):
    """What each seat's crons are being told, from their notepads: one line per key, with the jobs that carry
    it, and expired lines marked (a value ending `[until YYYY-MM-DD]` before today)."""
    keys = {}
    for name, seat in seats.items():
        home = hermes_home(seat)
        try:
            jobs = {j.get("id"): j.get("name") or j.get("id") for j in load_jobs(home)}
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(home, "cron", "notepad.db"), uri=True)
            rows = con.execute("SELECT job_id, key, value FROM cron_notepad").fetchall()
            con.close()
        except (OSError, ValueError, sqlite3.Error):
            continue
        for job_id, key, value in rows:
            if job_id in jobs:
                keys.setdefault((key, value), []).append("%s:%s" % (seat.get("name") or name, jobs[job_id]))
    if not keys:
        return ["briefing: nothing set"]
    lines = ["briefing: %d line%s" % (len(keys), "" if len(keys) == 1 else "s")]
    for (key, value), carriers in sorted(keys.items()):
        m = UNTIL.search(value or "")
        mark = "!" if m and m.group(1) < today else "-"
        lines.append("  %s %s: %s (%d job%s)%s" % (mark, key, value[:160], len(carriers),
                                                  "" if len(carriers) == 1 else "s",
                                                  " EXPIRED" if mark == "!" else ""))
    return lines


def seat_section(monitor, name, seat, now):
    """One seat's window: what it wrote, how asks were closed, any double answer, and the gate's queue."""
    files = seat.get("files") or {}
    label = seat.get("name") or name
    lines = []
    try:
        entries = monitor._log_entries(os.path.join(STATE, files["publish_log"]))
    except (KeyError, OSError):
        entries = []
    recent = [e for e in entries if now - epoch(e.get("ts")) <= HOURS * 3600]
    kinds = {}
    finals = final_closes(entries)
    for e in recent:
        if e.get("adopted"):
            continue
        kind = e.get("kind")
        if kind in ("decline", "abstain") and finals.get(e.get("subject")) != kind:
            continue   # superseded: an abstention standing in for a decline is one abstention, not two closes
        kinds[kind] = kinds.get(kind, 0) + 1
    wrote = ", ".join("%d %s" % (kinds[k], k) for k in ("post", "reply", "delete", "like", "follow") if kinds.get(k))
    closed = ", ".join("%d %s" % (kinds[k], k) for k in ("decline", "abstain") if kinds.get(k))
    unindexed = sum(1 for e in recent if e.get("indexed") is False)
    lines.append("%s, last %dh: %s%s%s" % (label, HOURS, wrote or "wrote nothing",
                                          "; closed " + closed if closed else "",
                                          "; %d not indexed" % unindexed if unindexed else ""))
    doubles = [d for d in monitor.double_answers(entries=entries) if now - epoch(d.get("at")) <= HOURS * 3600]
    for d in doubles:
        lines.append("  ! double answer to %s: %s and %s" % (str(d["ask"]).split("/")[-1],
                                                             str(d["first"]).split("/")[-1],
                                                             str(d["second"]).split("/")[-1]))
    gate = gate_line(seat, files, now)
    if gate:
        lines.append("  " + gate)
    return lines



def final_closes(entries):
    """The state each ask ended in: the last reply, decline or abstain written for it.

    The log is a journal, so a decline that was later converted to an abstention leaves both rows behind. Counting
    rows reported two closes for one ask.
    """
    finals = {}
    for e in entries:
        if e.get("kind") in ("reply", "decline", "abstain") and e.get("subject"):
            finals[e["subject"]] = e["kind"]
    return finals

def gate_line(seat, files, now):
    """Whether the gate is on, what it holds, and how its last batch went."""
    enabled = None
    try:
        for j in load_jobs(hermes_home(seat)):
            # An interval gate keeps its command in monitor_script; a --script job keeps it in script.
            # Reading only the latter printed "gate ?" on a host where both gates were demonstrably running.
            if "delvetown_monitor" in str(j.get("script") or j.get("monitor_script") or ""):
                enabled = bool(j.get("enabled", True)) and j.get("state") != "paused"
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(STATE, files["wake"]), encoding="utf-8") as f:
            st = json.load(f)
    except (KeyError, OSError, ValueError):
        st = {}
    pending = st.get("pending") or {}
    times = [float(v["t"]) for v in pending.values() if isinstance(v, dict) and isinstance(v.get("t"), (int, float))]
    last = str(st.get("last") or "")
    prev = next((l for l in last.splitlines() if l.startswith("previous batch")), "")
    parts = ["gate " + ("off" if enabled is False else "on" if enabled else "?")]
    parts.append("%d waiting%s" % (len(pending), " (oldest %s)" % age(now - min(times)) if times else ""))
    if st.get("fired_at"):
        parts.append("last wake %s ago" % age(now - float(st["fired_at"])))
    if prev:
        parts.append(prev)
    return "; ".join(parts)


def main():
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = ["delvetown digest, %s" % datetime.now(timezone.utc).strftime("%a %H:%MZ")]
    try:
        seats = load_seats()
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print("\n".join(out + ["seats: unavailable (%s) — run install-monitor.sh" % exc]))
        return
    out += [""] + board_section(now)
    out += [""] + briefing_section(seats, today)
    try:
        monitor = load_monitor()
    except Exception as exc:     # a broken copy must not hide the rest of the digest
        out += ["", "seats: monitor unavailable (%s)" % type(exc).__name__]
    else:
        for name, seat in seats.items():
            out += [""] + seat_section(monitor, name, seat, now)
    print("\n".join(out))


if __name__ == "__main__":
    main()
