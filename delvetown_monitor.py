#!/usr/bin/env python3
"""Delvetown change-detector for a Hermes cron monitor.

Prints one deterministic line: unread count + a digest of the notification
reading. Identical output on consecutive ticks makes the scheduler skip the
agent run entirely, so the agent only wakes when the town has something new.

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
import subprocess

BIN = os.environ.get("AT_MCP_BIN", "/home/vailie/at_mcp/current/bin/at_mcp-stdio")
ENV_FILE = os.environ.get("DELVETOWN_ENV_FILE", "/home/vailie/.hermes/.env")
MONITOR_STATE = os.environ.get("AT_MCP_MONITOR_STATE", "/home/vailie/.local/state/at_mcp-monitor")
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

try:
    proc = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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

    digest = hashlib.md5(notes_text.encode("utf-8")).hexdigest()[:12] if notes_text else "empty"
    print(f"unread={unread} digest={digest}")
except Exception as exc:
    print("monitor-error:", type(exc).__name__)
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        pass
