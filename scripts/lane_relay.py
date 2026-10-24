#!/usr/bin/env python3
"""Wake the session that owns a lane when something has arrived in it. No model of its own.

The Quiet Year work is a burst of town messages that only the Quiet Year session can answer: it is the one
that works in the repo, and a cron that cannot read the repo must not answer (it did once, from what the town
said about a repo it could not see). The gate therefore sets those items aside (delvetown_monitor.record_lane)
instead of waking a cron for them. Until 2026-10-24 they were dropped, and the operator was the courier —
watching the town by hand for hours and telling the session "check, check, check".

This closes the loop with Hermes' own machinery. Run as a no-agent cron job every few minutes:

    hermes cron create "every 5m" --no-agent --script lane_relay.py --name lane-relay

Each run reads each seat's lane outbox. When a lane holds items it has not handed on, and the burst has settled
(no new item for settle_s, or the oldest has waited max_wait_s), it files one task on the `lanes` board with
those items, subscribes the session's chat to it in notify+wake mode, and completes it: the completion wakes
that chat's live session for one real turn, with the items in it. The session then reads, works in the repo and
answers on its own; nothing in this loop asks the operator.

Silent when there is nothing to hand on (no-agent stdout is a message, so nothing prints). A failure prints one
line, at most once an hour, so a broken relay is reported without repeating itself every run.

    lane_relay.py                  one pass (what the cron runs)
    lane_relay.py status           what each lane holds and whether it can wake anyone
    lane_relay.py pause quietyear  hold the lane: items keep arriving in the outbox, nobody is woken
    lane_relay.py resume quietyear
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
STATE = os.path.join(BASE, ".local", "state", "delvetown-dt")
HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = os.environ.get("DT_LANE_BOARD") or "lanes"
ERROR_EVERY_S = 3600
MAX_ITEMS = 30          # in one wake; the rest go in the next


def load_seats():
    for p in (os.environ.get("DT_SEATS_FILE"), os.path.join(STATE, "seats.json"),
              os.path.join(os.path.dirname(HERE), "seats.json")):
        if p and os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["seats"]
    raise RuntimeError("no seats.json — run install-monitor.sh")


def path_for(seat, kind, legacy):
    return os.path.join(STATE, (seat.get("files") or {}).get(kind) or legacy)


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def hermes(*args):
    out = subprocess.run([os.environ.get("HERMES_BIN") or "hermes", *args], capture_output=True, text=True,
                         timeout=120)
    if out.returncode != 0:
        raise RuntimeError("hermes %s exited %d: %s" % (" ".join(args[:3]), out.returncode,
                                                         (out.stderr or out.stdout).strip()[:200]))
    return out.stdout


def when(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%MZ")
    except (TypeError, ValueError):
        return "?"


def item_line(it):
    author = it.get("author") or "?"
    rkey = str(it.get("uri") or "").split("/")[-1]
    did = str(it.get("uri") or "").split("/")[2] if str(it.get("uri") or "").startswith("at://") else ""
    body = " ".join(str(it.get("text") or "").split())
    if len(body) > 280:
        body = body[:280].rstrip() + "…"
    stamp = (it.get("indexed_at") or "")[11:16] or when(it.get("seen_at"))
    return "%sZ %s @%s %s: %s\n  %s" % (stamp.rstrip("Z"), it.get("reason") or "?", author, rkey, body,
                                         "https://delve.town/profile/%s/post/%s" % (did, rkey) if did else "")


def wake_text(lane, items):
    threads = len({it.get("root") or it.get("uri") for it in items})
    head = ("%s lane: %d new since %s, in %d thread%s. You were woken because these arrived; the crons do not "
            "answer this lane, and nobody else will. Read them, work in the repo where it matters, and answer in "
            "the town (dt.py reply … --in-quietyear) where an answer is owed. Nothing here needs the operator."
            % (lane, len(items), when(min(float(it.get("seen_at") or 0) for it in items)), threads,
               "" if threads == 1 else "s"))
    return head + "\n\n" + "\n".join(item_line(it) for it in items)


def due(items, conf, now):
    """A burst is handed on once it has settled, or once its oldest item has waited long enough."""
    seen = [float(it.get("seen_at") or 0) for it in items]
    return now - max(seen) >= float(conf.get("settle_s", 600)) or now - min(seen) >= float(conf.get("max_wait_s", 1800))


def wake(lane, conf, items):
    """File, subscribe, complete: the completion is what wakes the session."""
    target = conf.get("wake") or {}
    key = "lane-%s-%s" % (lane, hashlib.md5("\n".join(sorted(it["uri"] for it in items)).encode()).hexdigest()[:16])
    text = wake_text(lane, items)
    task = json.loads(hermes("kanban", "--board", BOARD, "create", "%s: %d new" % (lane, len(items)),
                             "--body", text, "--idempotency-key", key, "--created-by", "lane-relay", "--json"))
    sub = ["kanban", "--board", BOARD, "notify-subscribe", task["id"], "--platform", target["platform"],
           "--chat-id", str(target["chat_id"]), "--delivery-mode", target.get("mode") or "notify+wake"]
    if target.get("thread_id"):
        sub += ["--thread-id", str(target["thread_id"])]
    if target.get("chat_type"):
        sub += ["--chat-type", target["chat_type"]]
    hermes(*sub)
    hermes("kanban", "--board", BOARD, "complete", task["id"], "--summary", text)
    return task["id"]


def one_pass(now=None):
    now = time.time() if now is None else now
    printed = []
    for name, seat in load_seats().items():
        for lane, conf in (seat.get("lanes") or {}).items():
            if lane.startswith("_") or not isinstance(conf, dict):
                continue
            outbox = read_jsonl(path_for(seat, "lane_outbox", "lane-%s-%s.jsonl" % (lane, seat.get("name") or name)))
            st_path = path_for(seat, "lane_relay", "lane-relay-%s.json" % (seat.get("name") or name))
            st = load_state(st_path)
            lst = st.setdefault(lane, {})
            sent = lst.setdefault("sent", {})
            waiting = [it for it in outbox if it.get("lane", lane) == lane and it.get("uri") not in sent]
            target = conf.get("wake") or {}
            if not waiting or lst.get("paused") or not (target.get("platform") and target.get("chat_id")):
                continue        # nothing new, held on purpose, or nowhere to wake: status and the digest say so
            if not due(waiting, conf, now):
                continue
            batch = sorted(waiting, key=lambda it: float(it.get("seen_at") or 0))[:MAX_ITEMS]
            try:
                lst["last_task"] = wake(lane, conf, batch)
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                if now - float(lst.get("error_at") or 0) >= ERROR_EVERY_S:
                    lst["error_at"] = now
                    printed.append("lane relay: %s/%s could not wake its session: %s" % (seat.get("name") or name,
                                                                                          lane, exc))
                save_state(st_path, st)
                continue
            for it in batch:
                sent[it["uri"]] = now
            lst["woke_at"] = now
            lst.pop("error_at", None)
            lst["sent"] = {u: t for u, t in sent.items() if now - float(t) < 30 * 86400}
            save_state(st_path, st)
    if printed:
        print("\n".join(printed))


def status():
    out = {}
    for name, seat in load_seats().items():
        st = load_state(path_for(seat, "lane_relay", "lane-relay-%s.json" % (seat.get("name") or name)))
        for lane, conf in (seat.get("lanes") or {}).items():
            outbox = read_jsonl(path_for(seat, "lane_outbox", "lane-%s-%s.jsonl" % (lane, seat.get("name") or name)))
            sent = (st.get(lane) or {}).get("sent") or {}
            target = conf.get("wake") or {}
            out["%s/%s" % (seat.get("name") or name, lane)] = {
                "waiting": sum(1 for it in outbox if it.get("uri") not in sent),
                "handed_on": len(sent), "paused": bool((st.get(lane) or {}).get("paused")),
                "wakes": "%s:%s/%s" % (target.get("platform"), target.get("chat_id"), target.get("thread_id"))
                if target.get("chat_id") else "no target set in seats.json",
                "last_task": (st.get(lane) or {}).get("last_task")}
    print(json.dumps(out, indent=2))


def set_paused(lane, paused):
    for name, seat in load_seats().items():
        if lane in (seat.get("lanes") or {}):
            p = path_for(seat, "lane_relay", "lane-relay-%s.json" % (seat.get("name") or name))
            st = load_state(p)
            st.setdefault(lane, {})["paused"] = paused
            save_state(p, st)
            print("%s/%s %s" % (seat.get("name") or name, lane, "paused" if paused else "resumed"))


def main(argv):
    if argv[:1] == ["status"]:
        return status()
    if argv[:1] in (["pause"], ["resume"]) and len(argv) == 2:
        return set_paused(argv[1], argv[0] == "pause")
    if argv:
        sys.exit(__doc__)
    try:
        one_pass()
    except RuntimeError as exc:
        print("lane relay: %s" % exc)


if __name__ == "__main__":
    main(sys.argv[1:])
