#!/usr/bin/env python3
"""Re-read answers.jsonl for the word the model *filled in*, not a word from its commentary.

The first extractor took the last non-stopword token when the quoted sentence didn't match, which
read "collocation" out of an explanation and scored it as an answer. This one only accepts:
  1. the fill inside 'crisp, red|green ____' - straight, quoted or wrapped in markdown markers
  2. a bare bolded word (**pepper**)
  3. a reply that is nothing but one word
  4. a short reply whose first line is one word followed by punctuation
Anything else is recorded as none, and counted as such, rather than guessed.
"""
import json, os, re, collections

HERE = os.path.dirname(os.path.abspath(__file__))
FILL = re.compile(r"crisp,\s*(red|green)\s+[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\]]*", re.I)
BOLD = re.compile(r"\*\*([A-Za-z][A-Za-z'-]*)\*\*")
ONLY = re.compile(r"^[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\].!,]*$")
FIRST = re.compile(r"^[*_\"'`(\[]*([A-Za-z][A-Za-z'-]*)[*_\"'`)\].!,]*(?:\s|$)")


def word_of(raw):
    raw = (raw or "").strip()
    if not raw or raw.startswith("[reasoning only]") or raw.startswith("ERROR"):
        return None, "none"
    m = FILL.search(raw)
    if m:
        return m.group(2).lower(), "sentence"
    m = BOLD.search(raw)
    if m:
        return m.group(1).lower(), "bold"
    first = raw.splitlines()[0].strip()
    m = ONLY.match(first)
    if m:
        return m.group(1).lower(), "single-word"
    m = FIRST.match(raw)
    if m and len(raw) < 40:
        return m.group(1).lower(), "first-word"
    return None, "none"


def main():
    rows = [json.loads(l) for l in open(os.path.join(HERE, "answers.jsonl"), encoding="utf-8")]
    for r in rows:
        r["word2"], r["src"] = word_of(r["raw"])
    arms = ("C_red_free", "D_red_noapple", "A_red_nofruit", "B_green_nofruit")
    print("replies %d" % len(rows))
    for arm in arms:
        sub = [r for r in rows if r["arm"] == arm]
        w = [r["word2"] for r in sub if r["word2"]]
        c = collections.Counter(w)
        top, n = c.most_common(1)[0]
        print("\n%-16s answered %d/%d  distinct %d  modal %s %d = %.0f%%" % (arm, len(w), len(sub), len(c), top, n, 100 * n / len(w)))
        print("   " + ", ".join("%s %d" % (k, v) for k, v in c.most_common(10)))
    print("\nsource of the word:")
    for src, n in collections.Counter(r["src"] for r in rows).most_common():
        print("   %-12s %d" % (src, n))
    bad = [r for r in rows if not r["word2"]]
    print("none: %d" % len(bad))
    for r in bad[:8]:
        print("   %-20s %-16s %r" % (r["model"], r["arm"], r["raw"][:90]))
    with open(os.path.join(HERE, "answers.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
