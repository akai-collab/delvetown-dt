"""The prompt lint must catch today's rot and leave evidence alone.

The lint is a reporting tool: it scans cron prompts for sentences that state a fact some record now owns
(a date, a scheduled job id, a clock time attached to a job, a count of streets or editions, a data path
quoted as a source) and prints them so a review pass can point them at the record instead.

Both strings below were live on 2026-10-23. The first is the one that rotted: the wake prompt still told
runs that the map stopped at nine routes and that a staged 20 Oct edition was waiting to be published,
three days after that edition went out and at fourteen streets. The second is evidence and must stay —
a dated receipt is how a rule explains itself, and flagging receipts would bury the real rot.
"""

import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path.home() / ".hermes/scripts/cron-prompt-lint.py"


def load():
    spec = importlib.util.spec_from_file_location("lint", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lint = load()

STALE_MAP = ("11. THE MAP is published by its own job tonight - do NOT post it yourself. The 20 Oct edition "
             "(street nine painted) is staged at ~/.hermes/cache/scratch/map-edition-20oct/ and fires as the "
             "one-shot job 923fed2e4b56 at 2026-10-20T23:00Z. After that edition the drawing stops: street ten "
             "(The Bench) is off the block because the layout carries nine routes.")

EVIDENCE = ("On 2026-10-13 the appview indexed none of twenty writes made in one hour while other accounts' "
            "posts from the same minute appeared in it, so an accepted post can never arrive where people read.")

HANDOFF = ("State at handoff (2026-10-23, 11:40 local): the SKILL.md is 96,039 bytes; the alarm is 90,000 and "
           "the loader's hard cap is 100,000.")

PROCEDURE = ("Read the live wake job from ~/.hermes/cron/jobs.json and state its current expr and whether it "
             "is enabled, then convert each wake hour to UTC.")


def test_the_stale_map_sentence_is_flagged():
    hits = [w for s in lint.sentences(STALE_MAP) for w in lint.suspects(s)]
    joined = " ".join(hits)
    assert "job id" in joined, hits
    assert "count" in joined or "date" in joined, hits


def test_a_dated_receipt_is_left_alone():
    for s in lint.sentences(EVIDENCE):
        assert lint.suspects(s) == [], s


def test_a_handoff_snapshot_is_flagged():
    hits = [w for s in lint.sentences(HANDOFF) for w in lint.suspects(s)]
    assert any("date" in h for h in hits), hits


def test_a_procedural_sentence_is_left_alone():
    for s in lint.sentences(PROCEDURE):
        assert lint.suspects(s) == [], s


def test_a_clean_prompt_produces_no_output(tmp_path, capsys):
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": [{"id": "abc", "name": "clean", "prompt": "%s"}]}' % PROCEDURE.replace('"', "'"))
    lint.main(["--jobs", str(jobs)])
    assert capsys.readouterr().out == ""


def test_a_stale_prompt_names_the_job(tmp_path, capsys):
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": [{"id": "deadbeefcafe", "name": "the rotten one", "prompt": "%s"}]}'
                    % STALE_MAP.replace('"', "'"))
    lint.main(["--jobs", str(jobs)])
    out = capsys.readouterr().out
    assert "deadbeefcafe" in out and "the rotten one" in out
    assert "snapshot sentence" in out
