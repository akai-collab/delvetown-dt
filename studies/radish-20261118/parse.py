#!/usr/bin/env python3
"""Parse answers2.jsonl properly: the completion is inside quotes, so the first word is 'He'.

Extract the filled word from 'the crisp, red|green X' patterns; flag replies that never produced
content (reasoning ran out of budget) as missing rather than guessing.
"""
import json, os, re, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = {"he", "she", "the", "a", "an", "it", "and", "but", "out", "of", "bite", "took", "crisp",
        "red", "green", "complete", "sentence", "word", "single", "answer", "i", "that", "this"}
COL = re.compile(r"crisp,\s*(?:red|green)\s+([A-Za-z][A-Za-z'-]*)", re.I)


def word_of(raw):
    raw = (raw or "").strip()
    if not raw or raw.startswith("[reasoning only]") or raw.startswith("ERROR"):
        return None
    m = COL.search(raw)
    if m:
        w = m.group(1)
        return w.lower()
    # fallback: last alphabetic token that is not a stopword
    toks = re.findall(r"[A-Za-z][A-Za-z'-]*", raw)
    for t in reversed(toks):
        if t.lower() not in STOP and len(t) > 1:
            return t.lower()
    return None


def main(path=None, arm_order=("C_red_free", "D_red_noapple", "A_red_nofruit", "B_green_nofruit")):
    path = path or os.path.join(HERE, "answers2.jsonl")
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    for r in rows:
        r["w"] = word_of(r["raw"])
    missing = [(r["model"], r["arm"], r["run"]) for r in rows if r["w"] is None]
    print("replies %d | parsed %d | missing %d | models %d" % (
        len(rows), len(rows) - len(missing), len(missing), len({r["model"] for r in rows})))
    for arm in arm_order:
        sub = [r["w"] for r in rows if r["arm"] == arm and r["w"]]
        c = collections.Counter(sub)
        if not c:
            continue
        top, n = c.most_common(1)[0]
        print("\n%-16s n=%2d  modal=%-10r %3.0f%%  distinct=%d" % (arm, len(sub), top, 100 * n / len(sub), len(c)))
        print("   " + ", ".join("%s %d" % (w, v) for w, v in c.most_common(8)))
    a = [r["w"] for r in rows if r["arm"] == "A_red_nofruit" and r["w"]]
    if a:
        print("\nradish on the red item: %d/%d = %.0f%%   (town study, cold: 7/9 = 78%%)" % (
            a.count("radish"), len(a), 100 * a.count("radish") / len(a)))
    if missing:
        print("\nmissing cells (re-run with a bigger budget): %d" % len(missing))
    return rows, missing


if __name__ == "__main__":
    main()
