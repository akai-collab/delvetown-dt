#!/usr/bin/env python3
"""A stand-in for Grove's at_mcp binary, so the monitor can be tested without an account.

Speaks just enough MCP stdio: initialize, then a tools/call for get_unread_count and
get_notifications. Behaviour is chosen by environment:

  FAKE_MODE=ok     (default) a real-shaped notification reading: JSON, with the facets/cids that
                   the monitor must distil away
  FAKE_MODE=error  the notification call comes back as an at_mcp error string, whose "N seconds"
                   varies between runs (FAKE_SECONDS) — the case that must not schedule a wake
  FAKE_MODE=die    the client exits before answering, the case that used to print a traceback
"""
import json
import os
import sys

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
            else:
                respond(msg.get("id"), NOTES)
