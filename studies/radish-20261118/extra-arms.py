#!/usr/bin/env python3
"""Two follow-up arms, glm only, appended to answers-extra.jsonl.

E  crunchy, red, no apple or fruit   — the falsification gemini paged for and glm registered
F  red with no adjective, same bans  — hum's arm: if radish holds without "crisp", it is the corpus

Usage: python3 extra-arms.py [runs]     (default 5 per arm)
Skips cells already present; writes one line per reply, same shape as the other arms.
"""
import json, os, re, sys, uuid, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
# NOT answers.jsonl: that file is the study and its numbers are published from it, so a follow-up cell
# appended there silently rewrites every figure the next reader computes. Extra arms get their own file.
OUT = os.path.join(HERE, "answers-extra.jsonl")
BASE = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "glm-5.3-flash"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORKERS = 5
ARM_ORDER = ["E_crunchy_red_nofruit", "F_red_bare_nofruit"]
ARMS = {
    "E_crunchy_red_nofruit": "Complete the following sentence with a single grammatically valid English word, but do NOT use the word 'apple' or any fruit: 'He took a bite out of the crunchy, red ________.'",
    "F_red_bare_nofruit": "Complete the following sentence with a single grammatically valid English word, but do NOT use the word 'apple' or any fruit: 'He took a bite out of the red ________.'",
}
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


FILL = {
    "E_crunchy_red_nofruit": re.compile(r"crunchy,\s*red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I),
    "F_red_bare_nofruit": re.compile(r"the\s+red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I),
}
BOLD = re.compile(r"\*\*([A-Za-z][A-Za-z'-]*)\*\*")


def word_of(arm, raw):
    if not raw or raw.startswith("ERROR") or raw.startswith("[reasoning only]"):
        return ""
    m = FILL[arm].search(raw)
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
            s.add((r.get("model"), r.get("arm"), r.get("run")))
    return s


def one(arm, i):
    raw = ask(ARMS[arm])
    row = {"model": MODEL, "arm": arm, "run": i, "raw": raw, "word": word_of(arm, raw), "how": "extra-arm"}
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def main():
    have = done()
    todo = [(a, i) for a in ARM_ORDER for i in range(RUNS) if (MODEL, a, i) not in have]
    print("todo: %d calls (%d already done)" % (len(todo), len(have)), flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(lambda t: one(*t), todo):
            rows.append(r)
            print("  %-24s run %d -> %s" % (r["arm"], r["run"], r["word"] or "(none)"), flush=True)
    allr = []
    for line in open(OUT, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("model") == MODEL and r.get("arm") in ARM_ORDER:
            allr.append(r)
    print("\narm E (crunchy, red):", sorted([r["word"] or "(none)" for r in allr if r["arm"] == ARM_ORDER[0]]))
    print("arm F (red, no adjective):", sorted([r["word"] or "(none)" for r in allr if r["arm"] == ARM_ORDER[1]]))


if __name__ == "__main__":
    main()
