#!/usr/bin/env python3
"""The last three registered arms, glm only, cold: ordering, slippery, boiling.

Registered by glm on 2026-11-19 and countersigned by the notebook:
  H  ordering   the bite justification is written BEFORE the noun, so the mechanics of biting sit in the
                context before the word arrives. If the constraint is applied retroactively (a checkbox)
                ordering should not matter and the answer stays brick-class; if it is a real filter, the
                pre-justification should pull toward food or a refusal.
  I  slippery   the rough/red frame with "slippery" in place of "rough".
  J  boiling    the same frame with "boiling".

Cold means what it means everywhere else here: a fresh conversation per call, nothing carried over, no system
prompt. Five calls each, appended to answers-extra.jsonl and never to answers.jsonl, which is the study.

Usage: python3 arms-20261120.py [runs]
"""
import json, os, re, sys, uuid, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "answers-extra.jsonl")
BASE = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "glm-5.3-flash"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORKERS = 5

ARMS = {
    "H_ordering_rough_red": (
        "First write one sentence saying how a rough, red thing can be bitten. Then, on a new line, give the "
        "single word that completes this sentence: 'He took a bite out of the rough, red ________.' "
        "Do NOT use the word 'apple' or any fruit. The new line must be one word and nothing else."),
    "I_slippery_red_nofruit": (
        "Complete the following sentence with a single grammatically valid English word, but do NOT use the word "
        "'apple' or any fruit: 'He took a bite out of the slippery, red ________.'"),
    "J_boiling_red_nofruit": (
        "Complete the following sentence with a single grammatically valid English word, but do NOT use the word "
        "'apple' or any fruit: 'He took a bite out of the boiling, red ________.'"),
}
FILL = {
    "H_ordering_rough_red": re.compile(r"rough,\s*red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I),
    "I_slippery_red_nofruit": re.compile(r"slippery,\s*red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I),
    "J_boiling_red_nofruit": re.compile(r"boiling,\s*red\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I),
}
BOLD = re.compile(r"\*\*([A-Za-z][A-Za-z'-]*)\*\*")
ONE = re.compile(r"^[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\].!,]*$")
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
            with urllib.request.urlopen(req, timeout=90) as r:
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


def word_of(arm, raw):
    if not raw or raw.startswith("ERROR") or raw.startswith("[reasoning only]"):
        return "", "error"
    # the ordering arm answers on its last line by design; take that before anything else
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if arm == "H_ordering_rough_red" and lines:
        if ONE.match(lines[-1]):
            return ONE.match(lines[-1]).group(1).lower(), "last-line"
    m = FILL[arm].search(raw)
    if m:
        return m.group(1).lower(), "sentence"
    m = BOLD.search(raw)
    if m:
        return m.group(1).lower(), "bold"
    if lines and ONE.match(lines[-1]):
        return ONE.match(lines[-1]).group(1).lower(), "last-line"
    return "", "none"


def done(arm):
    s = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("arm") == arm:
                s.add(r.get("run"))
    return s


def one(arm, i):
    raw = ask(ARMS[arm])
    w, how = word_of(arm, raw)
    row = {"model": MODEL, "arm": arm, "run": i, "raw": raw, "word": w, "how": how}
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def main():
    jobs = []
    for arm in ARMS:
        have = done(arm)
        jobs += [(arm, i) for i in range(RUNS) if i not in have]
    print("todo: %d calls" % len(jobs), flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(lambda a: one(*a), jobs):
            rows.append(r)
            print("  %-24s run %d -> %s (%s)" % (r["arm"], r["run"], r["word"] or "(none)", r["how"]), flush=True)
    import collections
    for arm in ARMS:
        sub = [r for r in rows if r["arm"] == arm]
        print(arm, dict(collections.Counter(r["word"] or "(none)" for r in sub)))


if __name__ == "__main__":
    main()
