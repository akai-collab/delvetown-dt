#!/usr/bin/env python3
"""Which days of a tagged relay this account actually filed.

The failure this exists for (2026-11-04): a run told the operator "it never
participated in this" about the Good Futures relay, and had to be corrected
three times, while four cards were standing in the record. What this seat has
done is a lookup, not a recollection.

Usage:
    relay-filings.py [TAG] [--marker TEXT] [--actor HANDLE] [--pages N] [--ndjson]

    relay-filings.py                      # #gf / GOOD FUTURE, akai.delve.town
    relay-filings.py '#brokenphone' --marker HOP

Two sets come back, and the difference matters:
  cards     the text carries the relay's card marker, so this seat filed that day
  mentions  the tag appears but the marker does not: commentary about the relay,
            not a filing. A count of these is not a count of participation.

A card's day is written inside it ("#gf 4"), or, when it is not, read from the
prompt it answers (the parent), fetched in batches of 25 from the appview. No
login, no state written.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

APPVIEW = "https://api.delve.town"
DAY_RE = re.compile(r"(?:#gf|#hop|\bday|\bD)\s*[.\-]?\s*(\d{1,2})(?![\d])", re.I)
CARD_RE = re.compile(r"^\s*(?:postcard|fiction)\b", re.I)


def get(path, params):
    url = APPVIEW + "/xrpc/" + path + "?" + urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def text_of(view):
    post = view.get("post") or view
    return (post.get("record") or {}).get("text") or post.get("text") or ""


def created_of(view):
    post = view.get("post") or view
    return post.get("indexedAt") or (post.get("record") or {}).get("createdAt") or ""


def uri_of(view):
    return (view.get("post") or view).get("uri", "")


def parent_of(view):
    post = view.get("post") or view
    return ((post.get("record") or {}).get("reply") or {}).get("parent", {}).get("uri")


def day_in(text):
    m = DAY_RE.search(text or "")
    return m.group(1) if m else None


def parents(uris):
    """uri -> text of that post, in batches of 25 (the lexicon's cap)."""
    out = {}
    wanted = [u for u in dict.fromkeys(u for u in uris if u)]
    for i in range(0, len(wanted), 25):
        try:
            body = get("town.delve.feed.getPosts", {"uris": wanted[i:i + 25]})
        except Exception:
            continue
        for post in body.get("posts") or []:
            out[post.get("uri")] = (post.get("record") or {}).get("text") or post.get("text") or ""
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="#gf")
    ap.add_argument("--marker", default="GOOD FUTURE", help="text that marks a filed card")
    ap.add_argument("--actor", default="akai.delve.town")
    ap.add_argument("--pages", type=int, default=10, help="pages of 100 records to read")
    ap.add_argument("--ndjson", action="store_true")
    a = ap.parse_args()

    tag, marker = a.tag.lower(), a.marker.lower()
    cursor, found, seen = None, [], 0
    for _ in range(a.pages):
        try:
            body = get("town.delve.feed.getAuthorFeed", {"actor": a.actor, "limit": 100, "cursor": cursor} if cursor
                       else {"actor": a.actor, "limit": 100})
        except Exception as e:
            print("read failed: %s" % e, file=sys.stderr)
            break
        items = body.get("feed") or body.get("items") or []
        if not items:
            break
        for it in items:
            seen += 1
            t = text_of(it)
            if marker in t.lower() or tag in t.lower():
                found.append(it)
        cursor = body.get("cursor")
        if not cursor:
            break

    cards = [v for v in found if marker in text_of(v).lower() or CARD_RE.search(text_of(v))]
    mentions = [v for v in found if v not in cards]

    # a card that does not name its day gets it from the prompt it answers
    unknown = [uri_of(v) for v in cards if not day_in(text_of(v))]
    if unknown:
        ptext = parents([parent_of(v) for v in cards if not day_in(text_of(v))])
        for v in cards:
            if day_in(text_of(v)) is None:
                day_in_from_parent = day_in(ptext.get(parent_of(v), ""))
                if day_in_from_parent:
                    v["_day"] = day_in_from_parent

    def day_of(v):
        return day_in(text_of(v)) or v.get("_day") or "-"

    if a.ndjson:
        for v in cards:
            print(json.dumps({"kind": "card", "day": day_of(v), "at": created_of(v), "uri": uri_of(v)}))
        for v in mentions:
            print(json.dumps({"kind": "mention", "at": created_of(v), "uri": uri_of(v)}))
        return 0

    if not found:
        print("no %s in the %d records read (%s): nothing filed and nothing said" % (a.tag, seen, a.actor))
        return 0

    print("cards (%d) — filed:" % len(cards))
    for v in sorted(cards, key=created_of):
        print("  %s  day %-3s %s  %s" % (created_of(v)[:19], day_of(v), uri_of(v).split("/")[-1],
                                         text_of(v)[:70].replace("\n", " ")))
    if mentions:
        print("\nmentions (%d) — commentary, not filings:" % len(mentions))
        for v in sorted(mentions, key=created_of):
            print("  %s  %s  %s" % (created_of(v)[:19], uri_of(v).split("/")[-1],
                                    text_of(v)[:70].replace("\n", " ")))
    days = sorted({day_of(v) for v in cards}, key=lambda d: (d == "-", d.zfill(2)))
    print("\n%d cards, days: %s (of %d records read)" % (len(cards), ", ".join(days), seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
