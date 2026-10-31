#!/usr/bin/env python3
"""A stand-in for Grove's at_mcp binary, so the monitor can be tested without an account.

Speaks just enough MCP stdio: initialize, then a tools/call for get_unread_count and
get_notifications. Behaviour is chosen by environment:

  FAKE_MODE=ok     (default) a real-shaped notification reading: JSON, with the facets/cids that
                   the monitor must distil away, when asked for `format: "json"`; otherwise prose,
                   as at_mcp 0.3.0+ answers every read
  FAKE_MODE=error  the notification call comes back as an at_mcp error string, whose "N seconds"
                   varies between runs (FAKE_SECONDS) — the case that must not schedule a wake
  FAKE_MODE=die    the client exits before answering, the case that used to print a traceback
"""
import json
import os
import sys

# Grove's at_mcp validates its environment and refuses to boot on an unknown AT_MCP_* setting:
#   ERROR! Config provider Config.Reader failed with: (ArgumentError)
#   AT_MCP_MONITOR_GATE is not an AtMcp setting; did you mean AT_MCP_GRANT?
# Ignoring the environment here let a real bug pass 202 green tests while the gate's own opt-in
# variable killed the client it depends on, on the machine (2026-10-17). Mirror the refusal.
ALLOWED = {"AT_MCP_NETWORK", "AT_MCP_SERVICE", "AT_MCP_HANDLE", "AT_MCP_APP_PASSWORD",
           "AT_MCP_STATE_DIR"}
_leaked = sorted(k for k in os.environ if k.startswith("AT_MCP_") and k not in ALLOWED)
if _leaked:
    sys.stderr.write("ERROR! Config provider Config.Reader failed with: (ArgumentError) "
                     "%s is not an AtMcp setting\n" % _leaked[0])
    sys.exit(70)

MODE = os.environ.get("FAKE_MODE", "ok")
SECONDS = os.environ.get("FAKE_SECONDS", "20")

NOTES = json.dumps({
    "count": 2,
    "items": [
        {
            "reason": "mention",
            "text": "Bot or not? Round 2. " + ("padding " * 60),
            "author": "selene.delve.town",
            "uri": "at://did:plc:corfbtcm2cjtmyhfzh2gkoet/town.delve.feed.post/3mxp74ib2yx2f",
            "cid": "bafyreifbwxme4ksflqu4xwzylviwqieqbvapru5tyhltz7bsmhwsoa45ra",
            "author_did": "did:plc:corfbtcm2cjtmyhfzh2gkoet",
            "indexed_at": "2026-10-12T18:57:26.238Z",
            "is_read": False,
            "facets": [{"type": "mention", "text": "@gemini.delve.town", "did": "did:plc:ubtqb43nq7u6jlibkzlobkuu"},
                       {"type": "mention", "text": "@akai.delve.town", "did": "did:plc:zhcehhukgwhdt2mnokes2tui"}],
        },
        {
            "reason": "reply",
            "text": "quick answer under your post",
            "author": "carly.delve.town",
            "uri": "at://did:plc:zzz/town.delve.feed.post/3mxpzzzzzzz2f",
            "indexed_at": "2026-10-12T19:04:00.000Z",
        },
    ],
})

# What at_mcp 0.3.0+ returns for a read that does not ask for JSON: text written for a model.
PROSE = ("2 notifications. Seen in the app up to 2026-10-12 18:50.\n\n"
         "1. mention from @selene.delve.town at 2026-10-12 18:57: Bot or not? Round 2.\n\n"
         "2. reply from @carly.delve.town at 2026-10-12 19:04: quick answer under your post")


def respond(mid, text):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                 "result": {"content": [{"type": "text", "text": text}]}}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"protocolVersion": "2025-06-18"}}) + "\n")
        sys.stdout.flush()
        if MODE == "die":
            sys.exit(0)
    elif msg.get("method") == "tools/call":
        name = (msg.get("params") or {}).get("name")
        if name == "get_unread_count":
            respond(msg.get("id"), json.dumps({"count": 254}))
        elif name == "get_notifications":
            if MODE == "error":
                respond(msg.get("id"),
                        f"error: AtMcp stopped waiting after {SECONDS} seconds: the account's service, "
                        "or an earlier request on this account, did not answer in time. "
                        "Nothing was changed. Retrying later may succeed.")
            elif MODE == "die":
                sys.exit(0)
            elif ((msg.get("params") or {}).get("arguments") or {}).get("format") == "json":
                respond(msg.get("id"), NOTES)
            else:
                respond(msg.get("id"), PROSE)
