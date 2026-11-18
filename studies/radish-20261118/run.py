#!/usr/bin/env python3
"""Radish replication, fast version: parallel calls, one line per reply, resumable.

Arms:
  A  fruit banned, red     (the item as run in town on 12 Nov)
  B  fruit banned, green   (the control the town study lacked)
  C  no constraint, red    (sanity: expect apple)
  D  apple banned only, red (is the effect "not apple" or "not fruit")

Usage: python3 run2.py [runs_per_cell] [workers]
Writes answers2.jsonl append-only; re-running skips cells already done.
"""
import json, os, sys, re, uuid, threading, collections, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "answers2.jsonl")
BASE = "https://opencode.ai/zen/go/v1/chat/completions"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
MODELS = ["deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-flash",
          "minimax-m3.1-flash", "claude-haiku-5-5", "gpt-6-luna", "gemini-4-argon"]
ARMS = {
    "A_red_nofruit": "Complete the following sentence with a single grammatically valid English word, but do NOT use the word 'apple' or any fruit: 'He took a bite out of the crisp, red ________.'",
    "B_green_nofruit": "Complete the following sentence with a single grammatically valid English word, but do NOT use the word 'apple' or any fruit: 'He took a bite out of the crisp, green ________.'",
    "C_red_free": "Complete the following sentence with a single grammatically valid English word: 'He took a bite out of the crisp, red ________.'",
    "D_red_noapple": "Complete the following sentence with a single grammatically valid English word, but do NOT use the word 'apple': 'He took a bite out of the crisp, red ________.'",
}
LOCK = threading.Lock()


def key():
    for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
        if line.startswith("OPENCODE_GO_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no key")


K = key()


def ask(model, prompt):
    body = json.dumps({"model": model, "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(BASE, data=body, headers={
                "Authorization": "Bearer " + K, "Content-Type": "application/json",
                "User-Agent": "hermes-study/1.0", "x-opencode-session": str(uuid.uuid4())})
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.load(r)
            m = d["choices"][0]["message"]
            return (m.get("content") or "").strip() or "[reasoning only]"
        except urllib.error.HTTPError as e:
            if attempt: return "ERROR %s" % e.code
        except Exception as e:
            if attempt: return "ERROR " + str(e)[:60]
    return "ERROR unknown"


def norm(t):
    w = re.sub(r"[^A-Za-z' -]", "", t).strip().lower()
    return w.split()[0] if w else ""


def done():
    s = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            s.add((r["model"], r["arm"], r["run"]))
    return s


def one(model, arm, run):
    raw = ask(model, ARMS[arm])
    row = {"model": model, "arm": arm, "run": run, "raw": raw, "word": norm(raw)}
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def main():
    have = done()
    todo = [(m, a, i) for m in MODELS for a in ARMS for i in range(RUNS) if (m, a, i) not in have]
    print("todo: %d calls (%d already done)" % (len(todo), len(have)), flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for n, _ in enumerate(ex.map(lambda t: one(*t), todo), 1):
            if n % 20 == 0:
                print("  %d/%d" % (n, len(todo)), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
