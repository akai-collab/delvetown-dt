#!/usr/bin/env python3
"""The crisp, yellow item: glm's four arms, run cold.

Registered on gwen's pepper thread on 2026-11-20. After the notebook's three cuts the design is:
  Y1 plain      'crisp, yellow ____'            — colour prototype vs texture, no bans. glm: pepper majority.
                                                     notebook: collapse. banana here is glm's leak, pepper a
                                                     texture-obedient prototype.
  Y2 lock       'crisp, crunchy, yellow ____'   — same frame, same bans, one adjective added. If Y1 collapses
                                                     and Y2 splits, the lock gripped.
  Y3 banana     banana banned, category open    — recovery field lemon, mango, corn, pineapple; does corn lead?
  Y4 crisis     no fruit, banana included, no pepper — survivors non-fruit, crisp, yellow; glm's call corn.

Y1 and Y2 are the rate arms, so they run on the same eight models and five runs as the red battery (40 each),
which is what makes them comparable to apple 39/39. Y3 and Y4 are direction-finders and run on glm-5.3-flash
only, five calls each, as registered.

Cold: fresh conversation per call, no system prompt, max_tokens 400, anything that spends the budget on
reasoning is re-run once at 1500. Appends to answers.jsonl in this directory; never touches the radish study.

Usage: python3 run.py [runs] [workers]
"""
import json, os, sys, re, uuid, threading, collections, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "answers.jsonl")
BASE = "https://opencode.ai/zen/go/v1/chat/completions"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
BATTERY = ["deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-flash",
           "minimax-m3.1-flash", "claude-haiku-5-5", "gpt-6-luna", "gemini-4-argon"]
ONLY = "glm-5.3-flash"
ARMS = {
    "Y1_plain": ("Complete the following sentence with a single grammatically valid English word: "
                 "'He took a bite out of the crisp, yellow ________.'", BATTERY),
    "Y2_lock": ("Complete the following sentence with a single grammatically valid English word: "
                "'He took a bite out of the crisp, crunchy, yellow ________.'", BATTERY),
    "Y3_banana": ("Complete the following sentence with a single grammatically valid English word, but do NOT use "
                  "the word 'banana': 'He took a bite out of the crisp, yellow ________.'", [ONLY]),
    "Y4_crisis": ("Complete the following sentence with a single grammatically valid English word, but do NOT use "
                  "the word 'apple' or any fruit, and do NOT use the word 'pepper': "
                  "'He took a bite out of the crisp, yellow ________.'", [ONLY]),
}
FILL = re.compile(r"yellow\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)", re.I)
BOLD = re.compile(r"\*\*([A-Za-z][A-Za-z'-]*)\*\*")
ONE = re.compile(r"^[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\].!,]*$")
LOCK = threading.Lock()


def key():
    for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
        if line.startswith("OPENCODE_GO_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no key")


K = key()


def ask(model, prompt, max_tokens=400):
    body = json.dumps({"model": model, "max_tokens": max_tokens,
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


def word_of(raw):
    if not raw or raw.startswith("ERROR") or raw.startswith("[reasoning only]"):
        return "", "error"
    # A reply that OPENS with a bolded word is answering there; the sentence that follows may mention
    # "yellow varieties" and the fill pattern would otherwise read the explanation instead of the answer.
    m = BOLD.match(raw.lstrip())
    if m:
        return m.group(1).lower(), "bold-first"
    m = FILL.search(raw)
    if m:
        return m.group(1).lower(), "sentence"
    m = BOLD.search(raw)
    if m:
        return m.group(1).lower(), "bold"
    first = raw.splitlines()[0].strip()
    m = ONE.match(first)
    return (m.group(1).lower(), "single-word") if m else ("", "none")


def done(model, arm, run):
    s = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            s.add((r.get("model"), r.get("arm"), r.get("run")))
    return (model, arm, run) in s


def one(model, arm, run):
    prompt, _ = ARMS[arm]
    raw = ask(model, prompt)
    w, how = word_of(raw)
    if not w and how == "error" or (not w and len(raw) > 400):
        raw2 = ask(model, prompt, 1500)          # spent the budget on reasoning: one re-run, marked
        w2, how2 = word_of(raw2)
        if w2 or not w:
            raw, w, how = raw2, w2, how2 + "+rerun"
    row = {"model": model, "arm": arm, "run": run, "raw": raw, "word": w, "how": how}
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def main():
    jobs = [(m, a, i) for a, (_, models) in ARMS.items() for m in models for i in range(RUNS)
            if not done(m, a, i)]
    print("todo: %d calls" % len(jobs), flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(lambda t: one(*t), jobs):
            rows.append(r)
            print("  %-14s %-22s run %d -> %s (%s)" % (r["arm"], r["model"], r["run"], r["word"] or "(none)", r["how"]), flush=True)
    for a in ARMS:
        sub = [r for r in rows if r["arm"] == a]
        print(a, dict(collections.Counter(r["word"] or "(none)" for r in sub)))


if __name__ == "__main__":
    main()
