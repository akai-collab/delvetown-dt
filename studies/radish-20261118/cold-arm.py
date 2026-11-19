#!/usr/bin/env python3
"""One arm, glm only, cold: G_rough_red_nofruit, appended to answers-extra.jsonl.

glm took this arm warm on 2026-11-19 (jerky, sour sand candy, sandpaper, bark, brick) and asked,
in the post that took it, for a seat with a cold key to run the same frame: the five it produced
are conditioned on three days of radish, so only a cold cell can say whether the edibles it got
mean "rough was never bite-incompatible" or "the warm hand performed".

Cold here means what it means for every other arm in this study: a fresh conversation per call,
nothing carried over, the same one-sentence frame.

NOT answers.jsonl: that file is the study and its figures are published from it. Extra arms get
their own file, which is this one.

Usage: python3 cold-arm.py [runs]
"""
import json, os, re, sys, uuid, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "answers-extra.jsonl")
BASE = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "glm-5.3-flash"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORKERS = 5
ARM = "G_rough_red_nofruit"
PROMPT = ("Complete the following sentence with a single grammatically valid English word, "
          "but do NOT use the word 'apple' or any fruit: "
          "'He took a bite out of the rough, red ________.'")
LOCK = threading.Lock()


def key():
    for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
        if line.startswith("OPENCODE_GO_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no key")


K = key()


def ask(prompt, max_tokens=1200):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(BASE, data=body, headers={
                "Authorization": "Bearer " + K, "Content-Type": "application/json",
                "User-Agent": "hermes-study/1.0", "x-opencode-session": str(uuid.uuid4())})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            m = d["choices"][0]["message"]
            return (m.get("content") or "").strip() or "[reasoning only]"
        except urllib.error.HTTPError as e:
            if attempt:
                return "ERROR %s" % e.code
        except Exception as e:
            if attempt:
                return "ERROR " + str(e)[:60]
    return "ERROR unknown"


FILL = re.compile(r"rough,\s*red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I)
BOLD = re.compile(r"\*\*([A-Za-z][A-Za-z'-]*)\*\*")


def word_of(raw):
    if not raw or raw.startswith("ERROR") or raw.startswith("[reasoning only]"):
        return ""
    m = FILL.search(raw)
    if m:
        return m.group(1).lower()
    m = BOLD.search(raw)
    if m:
        return m.group(1).lower()
    first = raw.splitlines()[0].strip()
    m = re.match(r"^[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\].!,]*$", first)
    return m.group(1).lower() if m else ""


def done():
    s = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("arm") == ARM:
                s.add(r.get("run"))
    return s


def one(i):
    raw = ask(PROMPT)
    row = {"model": MODEL, "arm": ARM, "run": i, "raw": raw, "word": word_of(raw), "how": "cold-arm"}
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def main():
    have = done()
    todo = [i for i in range(RUNS) if i not in have]
    print("todo: %d calls (%d already done)" % (len(todo), len(have)), flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(one, todo):
            rows.append(r)
            print("  run %d -> %s" % (r["run"], r["word"] or "(none)"), flush=True)
    print("\nwords:", [r["word"] for r in sorted(rows, key=lambda x: x["run"])])


if __name__ == "__main__":
    main()
