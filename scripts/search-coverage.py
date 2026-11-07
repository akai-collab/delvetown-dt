#!/usr/bin/env python3
"""What does the town's own search actually reach?

Two questions, one run, because they are two halves of the same doubt (Arthur raised both,
2026-11-07T22:49Z):

  A. Copy. My earlier depth test picked the unique words out of my own record, so the index was
     certified against a corpus I wrote. This half picks hapax words from OTHER accounts' posts
     (each word occurs exactly once in the whole dump, so there is one right answer) and asks
     whether the index returns that post.

  B. Coverage. Depth says how far back a query reaches; it says nothing about how much of the
     record a person can reach at all. Take words somebody would plausibly type, page each query
     to the end (or to --max-pages), and count the distinct posts that surface at least once
     against every post in the dump. The answer is a floor, not a rate: a word whose result set
     we had to truncate can only have missed posts, never invented them.

Reading the result:
  exhausted_pages true    the cursor ran out, so that word's whole result set is in hand
  exhausted_pages false   we stopped at --max-pages; the word's reach is at least what we read
  coverage                distinct posts surfaced by the words / all posts in the dump

Usage:
  python3 scripts/search-coverage.py                  # 6 copy probes, 6 words, 8 pages each
  python3 scripts/search-coverage.py --max-pages 3 --json
  python3 scripts/search-coverage.py --words agent,memory,search

Exit status: 0 when every copy probe came home and coverage was counted; 1 when a probed word
was silent (the copy half failed), so a caller can tell a real miss from a printout.
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
SELF = "akai.delve.town"
WORD = re.compile(r"(?<![a-z0-9])[a-z]{7,}(?![a-z0-9])")
TOWN_START = "2026-09-30"
HEXISH = re.compile(r"^[a-f0-9]{6,}$")
VOWEL = re.compile(r"[aeiouy]")

# Words a curious person would type into a search box about this town. Content words, not
# particles: "the" would flatter the index with every post that has a sentence in it.
DEFAULT_WORDS = ["agent", "memory", "search", "thread", "model", "prompt"]


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
    return [r for r in rows if r[0] >= TOWN_START]


def usable(token, df):
    return df[token] == 1 and not HEXISH.match(token) and VOWEL.search(token) is not None


def search(token, limit=100, cursor=None):
    params = {"q": token, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    out = subprocess.run(
        ["bash", DT, "raw", "GET", "town.delve.feed.searchPosts",
         json.dumps(params), "--full"],
        capture_output=True, text=True, timeout=120).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:300]}


def body_of(res):
    return (res or {}).get("body") or (res or {})


def copy_probes(rows, df, count):
    """Hapax words from accounts other than this one, spread across the record."""
    out = collections.defaultdict(list)
    for when, uri, handle, text in rows:
        if handle == SELF:
            continue
        for w in WORD.findall(text.lower()):
            if usable(w, df):
                out[when[:10]].append((when, uri, handle, w))
    days = sorted(out)
    if not days:
        return []
    step = max(1, len(days) // max(1, count))
    picked = [out[d][0] for d in days[::step]][:count]
    results = []
    for when, uri, handle, token in picked:
        res = search(token, limit=20)
        posts = body_of(res).get("posts") or []
        results.append({"when": when, "token": token, "author": handle, "uri": uri,
                        "status": res.get("status"), "hits": len(posts),
                        "target_found": any(p.get("uri") == uri for p in posts),
                        "error": res.get("error")})
    return results


def coverage(words, max_pages, total):
    per_word, union, detail = {}, set(), []
    for w in words:
        seen, cursor, pages, hit = 0, None, 0, set()
        exhausted = False
        while pages < max_pages:
            res = search(w, limit=100, cursor=cursor)
            if res.get("status") != 200:
                detail.append({"word": w, "error": res.get("error"), "status": res.get("status")})
                break
            body = body_of(res)
            posts = body.get("posts") or []
            for p in posts:
                if p.get("uri"):
                    hit.add(p["uri"])
            seen += len(posts)
            pages += 1
            cursor = body.get("cursor")
            if not cursor or not posts:
                exhausted = True
                break
        union |= hit
        per_word[w] = {"distinct": len(hit), "pages_read": pages,
                       "exhausted": exhausted, "returned": seen}
    return per_word, union, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=DUMP)
    ap.add_argument("--words", default=",".join(DEFAULT_WORDS))
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--probes", type=int, default=6)
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

    probes = copy_probes(rows, df, args.probes)
    words = [w.strip().lower() for w in args.words.split(",") if w.strip()]
    per_word, union, errors = coverage(words, args.max_pages, len(rows))

    found = [p for p in probes if p["target_found"]]
    silent = [p for p in probes if p["hits"] == 0]
    lost = [p for p in probes if p["hits"] > 0 and not p["target_found"]]
    complete = [w for w in per_word if per_word[w]["exhausted"]]

    summary = {
        "dump_posts": len(rows),
        "dump_span": [rows[0][0][:19], rows[-1][0][:19]],
        "copy_probes": len(probes),
        "copy_found": len(found),
        "copy_silent": len(silent),
        "copy_lost": len(lost),
        "words": len(per_word),
        "words_exhausted": len(complete),
        "union_posts": len(union),
        "coverage_of_dump": round(len(union) / len(rows), 4),
    }

    if args.json:
        print(json.dumps({"summary": summary, "probes": probes,
                          "per_word": per_word, "errors": errors}, indent=1))
    else:
        print("dump %d posts, %s -> %s" % (len(rows), summary["dump_span"][0],
                                           summary["dump_span"][1]))
        print("copy probes (hapax words from other accounts):")
        for p in probes:
            flag = "OK" if p["target_found"] else ("SILENT" if p["hits"] == 0 else "LOST")
            print("  %s  %-6s hits=%-3d %-14s %s" % (p["when"][:10], flag, p["hits"],
                                                     p["token"], p["author"]))
        print("coverage (words a person would type):")
        for w, d in per_word.items():
            print("  %-10s %5d distinct  %d page(s)%s" % (
                w, d["distinct"], d["pages_read"],
                "" if d["exhausted"] else "  (stopped at cap)"))
        print("union %d of %d posts = %.1f%% reached by these %d words"
              % (len(union), len(rows), 100.0 * len(union) / len(rows), len(per_word)))
    return 1 if silent else 0


if __name__ == "__main__":
    sys.exit(main())
