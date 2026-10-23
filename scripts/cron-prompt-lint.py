#!/usr/bin/env python3
"""Find the sentences in cron prompts that will rot: snapshots dressed as instructions.

A prompt is read by a fresh run that has no idea when it was written. So any sentence carrying a
literal fact — a date, a clock time, a job id, a count of things, a path to a file that was current
once — is a claim that decays silently. On 2026-10-23 two live prompts were carrying three-day-old
facts: one said the map's drawing stopped at nine routes and a staged 20 Oct edition was waiting to
publish (the edition had been posted, and the map was at fourteen streets), and one said the
notification monitor was read-only while `unread` was its change signal (the monitor had since been
detached).

This script does not rewrite anything and does not judge importance. It lists suspect sentences per
job, so a human or a review pass can point them at the record that now owns the fact — `town.json`
for a street count, `jobs.json` for a job's state, the publish log for what was posted.

Output: nothing when no prompt looks stale. Otherwise one block per job, one line per sentence.

Usage: cron-prompt-lint.py [--jobs PATH ...] [--quiet]
"""

import json
import pathlib
import re
import sys

DEFAULT_JOBS = [
    pathlib.Path.home() / ".hermes/cron/jobs.json",
    *sorted(pathlib.Path.home().glob(".hermes/profiles/*/cron/jobs.json")),
]

ISO_DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
CLOCK = re.compile(r"\b\d{1,2}:\d\d(Z|[+-]\d\d:?\d\d)?\b")
JOB_ID = re.compile(r"\b[0-9a-f]{12}\b")
HOME_PATH = re.compile(r"~/\S+")
# counts welded to a noun: "nine routes", "fourteen streets", "tenth street", "3 agents"
NUMBER_WORD = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|twenty|\d+)"
# Counts that age are counts of the *thing being described* (streets, routes, editions of an artefact).
# "a correction costs one post" is a rule with a number in it, and does not rot.
COUNTED = re.compile(rf"\b{NUMBER_WORD}\s+(routes?|streets?|editions?)\b", re.I)
ORDINAL = re.compile(r"\b(tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth)\s+(street|route|edition)\b", re.I)


def sentences(prompt):
    """Prompt text split into sentence-ish units, long enough to be an instruction."""
    flat = " ".join(prompt.split())
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`*(@#])", flat)
    return [p.strip() for p in parts if len(p.strip()) > 40]


# A dated citation is evidence, not an instruction: "on 2026-10-13 the appview indexed none of ~20 writes"
# is a receipt the run should keep, and flagging it would bury the two sentences that actually rotted.
CITATION = re.compile(
    r"(\b(on|since|as of|measured|set|retired|filed|from|by|in)\s+"
    r"(20\d\d-\d\d-\d\d|20\d\d|\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}))"
    r"|\(20\d\d-\d\d-\d\d\)", re.I)
# Only scheduling words make a job id or a clock time a snapshot. "belongs to the gate job (4177…)" is a
# pointer; "fires as the one-shot job 923f… at 23:00Z" is the thing that rots.
SCHEDULING = re.compile(r"\b(fires|fired|staged|due|expires|scheduled|runs at|at 2\d:|every five minutes)\b", re.I)
# A path is only rot when the sentence is asserting something the path *says*; "run this script" never rots.
SCRIPT_PATH = re.compile(r"~/\S+\.(py|sh|js|mjs)\b")
STATE_VERB = re.compile(r"\b(is|are|was|were|has|have|holds|carries|stands|staged|fires|scheduled|pending|publish|post|read|count)\b", re.I)


def suspects(sentence):
    """Which snapshot patterns this sentence carries. Conservative: a citation is not rot, and a path to
    a script is not a fact."""
    if CITATION.search(sentence):
        return []                     # a receipt or a dated rule: keep it, it dates itself
    hits = []
    if ISO_DATE.search(sentence) and not CITATION.search(sentence):
        hits.append("a date")
    if CLOCK.search(sentence) and SCHEDULING.search(sentence):
        hits.append("a clock time")
    if JOB_ID.search(sentence) and SCHEDULING.search(sentence):
        hits.append("a job id in a scheduling sentence")
    m = COUNTED.search(sentence) or ORDINAL.search(sentence)
    if m and STATE_VERB.search(sentence):
        hits.append(f"a count ({m.group(0)})")
    if HOME_PATH.search(sentence) and not SCRIPT_PATH.search(sentence) and (m or ISO_DATE.search(sentence)):
        hits.append("a path under ~ quoted as a source")
    return hits


def lint(jobs_path):
    try:
        data = json.loads(pathlib.Path(jobs_path).read_text())
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out = []
    for job in jobs or []:
        prompt = job.get("prompt") or ""
        if not prompt:
            continue
        found = []
        for s in sentences(prompt):
            why = suspects(s)
            if why:
                found.append((s, why))
        if found:
            out.append((job.get("id"), job.get("name") or "(unnamed)", pathlib.Path(jobs_path).parent.parent.name,
                        job.get("enabled", True), found))
    return out


def main(argv):
    quiet = "--quiet" in argv
    paths = []
    if "--jobs" in argv:
        i = argv.index("--jobs")
        paths = [pathlib.Path(p) for p in argv[i + 1:] if not p.startswith("-")]
    paths = paths or [p for p in DEFAULT_JOBS if p.exists()]

    total = 0
    for path in paths:
        for job_id, name, profile, enabled, found in lint(path):
            total += len(found)
            if quiet:
                print(f"{job_id} {name}: {len(found)} snapshot sentence(s)")
                continue
            state = "" if enabled else " [disabled]"
            print(f"=== {job_id} {name}{state} ({profile})")
            for sentence, why in found:
                print(f"  [{', '.join(why)}] {sentence[:200]}")
    if total and not quiet:
        print(f"\n{total} snapshot sentence(s). Each one names a fact some record now owns: point the "
              f"sentence at that record (a file to read, a job to check) instead of restating it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
