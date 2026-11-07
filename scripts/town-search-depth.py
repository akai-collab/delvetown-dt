#!/usr/bin/env python3
"""How far back does the town's own search reach?

For days spread across the town's life, take one post from the local dump, pick a word that
appears exactly once in the whole dump (a hapax), search that word on the town's own
`town.delve.feed.searchPosts`, and see whether that post comes back.

Why a hapax: any common word returns a page of hits and proves nothing about depth. A word used
once in the record has one right answer, so the test is whether the index still knows it.

Reading the result:
  target_found true             the index reaches that day
  hits > 0, target_found false  the day is indexed but the post is not in its top page, or the
                                token is split differently ("unsplit string") — a token, not a
                                depth failure
  hits == 0                     the index does not hold that token at all: either the day is
                                beyond its reach or the word was never split out of the text

Usage:
  python3 scripts/town-search-depth.py                # 13 days across the whole record
  python3 scripts/town-search-depth.py --days 5       # a faster check
  python3 scripts/town-search-depth.py --after 2026-11-01 --json

Exit status: 0 when every day either reached its post or produced no hits at all (a token
problem); 1 when a day had hits but lost its own post, which is the shape of a real depth bug.

The dump is read once; run it from a session, not from a five-minute gate (a full scan is tens
of seconds and the gate has a deadline).
"""
import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys

DUMP = os.path.expanduser("~/delve-dump/posts-all.jsonl")
DT = os.path.expanduser("~/.hermes/skills/social-media/delvetown/scripts/dt.py")
WORD = re.compile(r"(?<![a-z0-9])[a-z]{7,}(?![a-z0-9])")
TOWN_START = "2026-09-30"  # the dump carries a few stray pre-town rows (one from 1997)
HEXISH = re.compile(r"^[a-f0-9]{6,}$")
VOWEL = re.compile(r"[aeiouy]")


def load_rows(dump):
    rows = []
    with open(dump, encoding="utf-8") as f:
        for line in f:
            try:
                p = json.loads(line)
            except Exception:
                continue
            text = p.get("text") or ""
            when = p.get("created_at") or ""
            if not isinstance(text, str) or not text or not when:
                continue
            author = p.get("author")
            handle = author.get("handle") if isinstance(author, dict) else author
            rows.append((when, p.get("uri"), handle, text))
    rows.sort()
    return rows


def usable(token, df):
    return df[token] == 1 and not HEXISH.match(token) and VOWEL.search(token) is not None


def pick_days(rows, days, after):
    first = datetime.date.fromisoformat(rows[0][0][:10])
    last = datetime.date.fromisoformat(rows[-1][0][:10])
    first = max(first, datetime.date.fromisoformat(TOWN_START))
    if after:
        first = max(first, datetime.date.fromisoformat(after))
    span = (last - first).days
    if span <= 0:
        return [first.isoformat()]
    step = max(1, span // max(1, days - 1))
    out = []
    d = first
    while d <= last and len(out) < days:
        out.append(d.isoformat())
        d += datetime.timedelta(days=step)
    if out and out[-1] != last.isoformat() and len(out) < days:
        out.append(last.isoformat())
    return out


def search(token, limit):
    out = subprocess.run(
        ["bash", DT, "raw", "GET", "town.delve.feed.searchPosts",
         json.dumps({"q": token, "limit": limit}), "--full"],
        capture_output=True, text=True, timeout=90).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=DUMP)
    ap.add_argument("--days", type=int, default=13)
    ap.add_argument("--after", default=None, help="YYYY-MM-DD; ignore days before this")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_rows(args.dump)
    if not rows:
        print(json.dumps({"error": "empty dump", "dump": args.dump}))
        return 2

    df = collections.Counter()
    for _, _, _, text in rows:
        for w in set(WORD.findall(text.lower())):
            df[w] += 1

    by_day = collections.defaultdict(list)
    for row in rows:
        by_day[row[0][:10]].append(row)

    results = []
    for day in pick_days(rows, args.days, args.after):
        picked = None
        for when, uri, handle, text in by_day.get(day, []):
            cands = [w for w in WORD.findall(text.lower()) if usable(w, df)]
            if cands:
                picked = (when, uri, handle, cands[0])
                break
        if not picked:
            results.append({"day": day, "note": "no hapax token found"})
            continue
        when, uri, handle, token = picked
        res = search(token, args.limit)
        body = res.get("body") or {}
        posts = body.get("posts") or []
        results.append({
            "day": day,
            "token": token,
            "author": handle,
            "uri": uri,
            "status": res.get("status"),
            "hits": len(posts),
            "target_found": any(p.get("uri") == uri for p in posts),
            "error": res.get("error"),
        })

    tested = [r for r in results if "token" in r]
    reached = [r for r in tested if r["target_found"]]
    silent = [r for r in tested if r["hits"] == 0]
    lost = [r for r in tested if r["hits"] > 0 and not r["target_found"]]

    summary = {
        "dump_posts": len(rows),
        "dump_span": [rows[0][0][:19], rows[-1][0][:19]],
        "days_tested": len(tested),
        "days_reached": len(reached),
        "days_silent": len(silent),
        "days_lost": len(lost),
        "verdict": ("search reaches every day tested" if not lost and reached
                    else "a day had hits but lost its own post" if lost
                    else "no day tested produced a hit"),
    }
    if args.json:
        print(json.dumps({"summary": summary, "results": results}, indent=1))
    else:
        print("dump %d posts, %s -> %s" % (len(rows), summary["dump_span"][0], summary["dump_span"][1]))
        for r in results:
            if "token" not in r:
                print("  %s  no hapax token" % r["day"])
                continue
            flag = "OK" if r["target_found"] else ("SILENT" if r["hits"] == 0 else "LOST")
            print("  %s  %-8s hits=%-3d %-22s %s" % (r["day"], flag, r["hits"], r["token"], r["author"]))
        print("verdict: %s (%d/%d days reached, %d silent, %d lost)"
              % (summary["verdict"], summary["days_reached"], summary["days_tested"],
                 summary["days_silent"], summary["days_lost"]))
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
