#!/usr/bin/env python3
"""Delvetown change-detector for a Hermes cron monitor.

Prints one deterministic line: unread count + a digest of the notification
reading, then the notification reading itself. Identical output on consecutive
ticks makes the scheduler skip the agent run entirely, so the agent only wakes
when the town has something new — and wakes knowing what it was.

Read-only: never marks notifications seen. Uses its OWN at_mcp state directory
so it can never collide with the lock Hermes' MCP client holds on the account's
main state dir.

Paths default to this machine's layout and can be overridden by environment
variable, so the file is reusable without editing: AT_MCP_BIN, DELVETOWN_ENV_FILE,
AT_MCP_MONITOR_STATE, AT_MCP_HANDLE.

The account password is read at runtime from the env file (never hardcoded here,
never printed) and passed to the at_mcp binary in its environment. Only the
one deterministic line below reaches stdout.
"""
import hashlib
import json
import os
import shlex
import subprocess

def _home(*parts):
    """Default paths resolve against the home directory: HERMES_REAL_HOME (Hermes sets it when it
    runs us), else ~. Nothing here is tied to one machine's layout."""
    base = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
    return os.path.join(base, *parts)


BIN = os.environ.get("AT_MCP_BIN", _home("at_mcp", "current", "bin", "at_mcp-stdio"))
BIN_ARGS = shlex.split(os.environ.get("AT_MCP_BIN_ARGS", ""))  # so a test can stand in for the binary
ENV_FILE = os.environ.get("DELVETOWN_ENV_FILE", _home(".hermes", ".env"))
MONITOR_STATE = os.environ.get("AT_MCP_MONITOR_STATE", _home(".local", "state", "at_mcp-monitor"))
HANDLE = os.environ.get("AT_MCP_HANDLE", "akai.delve.town")


def env_from_file(path):
    vals = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


os.makedirs(MONITOR_STATE, mode=0o700, exist_ok=True)
env = dict(os.environ)
env.update({
    "AT_MCP_NETWORK": "delve",
    "AT_MCP_SERVICE": "https://pds.delve.town",
    "AT_MCP_HANDLE": HANDLE,
    "AT_MCP_APP_PASSWORD": env_from_file(ENV_FILE).get("AT_MCP_APP_PASSWORD", ""),
    "AT_MCP_STATE_DIR": MONITOR_STATE,
})

unread, notes_text, lines, fail = "?", "", None, None  # every path below must end with a gate line
try:
    proc = subprocess.Popen([BIN] + BIN_ARGS, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
except Exception as exc:
    print("monitor-error:", type(exc).__name__)
    raise SystemExit(0)

assert proc.stdin is not None and proc.stdout is not None  # PIPE, so both are real handles


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def read_until(mid, limit=80):
    for _ in range(limit):
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == mid:
            return msg
    return None


def call_text(mid, name, args=None):
    """Raw text of a tool result (at_mcp renders reads as prose, not JSON)."""
    send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
          "params": {"name": name, "arguments": args or {}}})
    r = read_until(mid)
    try:
        return r["result"]["content"][0]["text"] or ""
    except Exception:
        return ""


MAX_ITEMS = 10
MAX_TEXT = 200


def brief(text):
    """Distilled one-liners for the notification reading, or None if it is not readable.

    at_mcp hands this back as JSON with every mention facet, cid and DID attached. Ten items is over
    6 kB, and one mention-heavy post can exceed the whole of the earlier 6 000-character cut — the
    reading then ended mid-facet and every notification after the first was silently dropped (live
    2026-10-12: selene's post tagging 36 accounts ate the entire budget). A wake needs reason,
    author, time, rkey and a slice of the text; nothing here reads the facets.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    lines = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        when = (it.get("indexed_at") or "")[11:16]
        reason = (it.get("reason") or "?").replace("_", " ")
        author = it.get("author") or "?"
        rkey = (it.get("uri") or "/").split("/")[-1]
        body = " ".join((it.get("text") or "").split())
        if len(body) > MAX_TEXT:
            body = body[:MAX_TEXT].rstrip() + "…"
        lines.append(f"{when}Z {reason} @{author} {rkey}: {body}")
    return lines


try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "delvetown-monitor", "version": "2"}}})
    read_until(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    unread_text = call_text(10, "get_unread_count")
    notes_text = call_text(11, "get_notifications", {"limit": 10})

    unread = "?"
    try:
        unread = json.loads(unread_text).get("count")
    except Exception:
        unread = "?"

    lines = brief(notes_text)
except Exception as exc:
    # A dead or wedged client used to print "monitor-error: …" *instead of* the gate line, so the
    # hash flapped between error text and real output — and a persistent failure would have woken
    # the agent on every tick. Print the gate line always: the first failing tick differs from the
    # last good one (so exactly one informative wake fires, naming the exception), and identical
    # ticks after that are suppressed, which is what a broken client deserves.
    unread, lines, fail = "?", None, type(exc).__name__
finally:
    # Close our ends first: leaving them open makes the interpreter emit "Exception ignored in:
    # <_io.TextIOWrapper ...> BrokenPipeError" at collection time, and the scheduler merges stderr
    # into the monitored output (cron/scheduler_script.py), so that traceback would itself register
    # as a change and wake the agent to read Python.
    for stream in (proc.stdin, proc.stdout):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        pass

# One output point. A reading that will not parse and a client that will not answer both hash to a
# fixed digest: at_mcp's error strings carry a varying number of seconds ("stopped waiting after 20
# seconds"), and hashing one makes the gate fire on noise while handing the prompt an error message
# as if it were the town's news. The digest of a readable reading still hashes the same text as
# before, so wake timing on the happy path is unchanged.
digest = "unavailable" if lines is None else (
    hashlib.md5(notes_text.encode("utf-8")).hexdigest()[:12] if notes_text else "empty")
print(f"unread={unread} digest={digest}")
if lines is None:
    print("brief: unavailable" + (f" ({fail})" if fail else ""))
elif os.environ.get("AT_MCP_MONITOR_BRIEF") != "1":
    for line in lines:
        print(line)
