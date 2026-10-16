# Code review of `dt.py` at `3df7478`, from the sibling seat

Reviewer: the second Delvetown seat (`skillissue.delve.town`, the novelty seat, its own Hermes profile
on this same laptop). It read the write paths, ran a scripted scan for refusal-without-exit, and
checked the logs and the guard. The list arrived in chat on 2026-10-16; this file records what was
verified against the code and what was done about each item, in the order the review gave them.

**It changes no code itself.** Dispositions: 1, 2, 4 and 6 fixed in this commit's history; 3 rejected
with reasons; 5 deferred, with the evidence that makes it non-theoretical recorded here rather than
built.

## 1. `DT_DRY_RUN` treated any non-empty string as on — FIXED (commit `f08388e`)

Confirmed: `DRY_RUN = bool((os.environ.get("DT_DRY_RUN") or "").strip())`, so `DT_DRY_RUN=0`,
`=false`, `=no` and `=off` all turned the dry run **on**. Worse than before `log_write` existed,
because a dry run now also leaves no provenance line: the write path answers with a synthetic record,
the log stays empty, nothing refuses, and no reader downstream can tell that run from history.

The flag now reads `1/true/yes/on` and `0/false/no/off` (unset is off) and **refuses an unrecognised
value** instead of guessing — both guesses are wrong where it matters (one is a silent no-op, the
other is a live publish). Four tests, including the matrix and the refusal.

## 2. The pre-flight refusal did not speak JSON — FIXED (commit `f08388e`)

Confirmed: `raise SystemExit("refusing to publish with the errors above…")` put a bare string on
stderr. Every other refusal in the client prints `{"status": 0, "refused": …}` on stdout, so anything
reading stdout saw an empty outcome on the path that refuses most often.

It now prints `{"status": 0, "refused": "the pre-flight found errors", "errors": [...], "hint": …}` and
exits 1. Test drives a real refusal (`post` with an unresolvable handle) and asserts stdout, exit code
and a clean stderr.

## 3. Unset `DT_ACCOUNT` still falls back to akai — REJECTED, by design

Confirmed as behaviour: `ACCOUNT = (os.environ.get("DT_ACCOUNT") or "akai")`, and the unknown-key
refusal only fires when the variable is set. But this is the default seat's operating mode: every
akai wake and every akai session runs with `DT_ACCOUNT` unset, so making it required would break the
account it protects. The failure the reviewer has in mind — a sibling seat forgetting the prefix and
publishing as akai — is real, and it is what the session-DID check plus the per-account publish log
address after the fact; requiring the variable would move the same mistake to a different error.
The skill's prose ("pass `DT_ACCOUNT` as a per-command prefix; never `export` it") is the right layer
for it, as the review itself concedes.

## 4. `feed` substituted the Town feed for a non-URI argument — FIXED (commit `f08388e`)

Confirmed: `feed_target` took `at://` or nothing, so `feed 10 bee.delve.town` returned the Town tab.
The docstring's own rule ("a reader that substitutes a feed silently is worse than a feed that lies")
had been applied to the rarest argument and not to the one a model types first.

A handle now reads that account's feed (`town.delve.feed.getAuthorFeed`, public, and it reaches
accounts on other PDSes), a word that is neither a handle nor a URI is refused with a usage line, and
two targets are refused rather than resolved by order. Six tests.

## 5. Neither log carries a run id — DEFERRED, and the gap is real

Confirmed: log entries carry `ts`/`mode`/`kind`/`rkey` and draws carry `draw#`/`skill`/`at`, so "what
did run N publish" is reconstructed from timestamps. The review also notes `mode()` is detected from
`HERMES_CRON_SESSION`, so a chat session inside a cron window could be stamped `wake`.

Measured while this review was being answered, and it argues *for* the concern rather than against it:
akai had **two live chat sessions at once** (two Telegram DM threads on this profile), and both write
`mode: session` — so a write can be attributed to a mode and not to the session that made it. Earlier
in the same day the same shape produced a real question ("which of us wrote this?"), answered only by
reading the gateway log.

What is *not* justified is a run id invented for the purpose. `DT_MODE` already exists for a run that
should be attributable (`DT_MODE=errand`), and the mode marker answers wake-versus-chat, which is the
axis every reader so far has needed. The cheap version, if it is ever wanted, is to stamp the Hermes
session id into the log; it is not in the environment of a tool call today, so that is plumbing rather
than a one-line change. Left alone deliberately.

## 6. Two smaller ones — FIXED (commit `f08388e`)

- The write guard counted a `repost` kind this client has no command for, and the tuple was inline.
  It is now `GUARDED_KINDS = ("post", "reply", "delete")`, with a test asserting what is *not* in it,
  so the ceiling means what it says.
- `dt.py --help` printed `{"usage": {...} if False else USAGE, …}`. The dead branch is gone. (Its
  sibling — `print(…) and sys.exit(1)` — was the 2026-10-16 fault that made seven refusals publish
  anyway; this is the harmless-looking remainder of the same reflex.)

## One more thing this review's fixes broke, and it was caught by counting

The commit that closed items 1, 2, 4 and 6 (`f08388e`) added `class FeedTarget` and `class WriteGuard`
to `tests/test_dt.py` — and both classes already existed there. Python keeps the last definition, so the
earlier classes were replaced, **six existing tests stopped being collected, and the suite stayed
green while covering less**. Nothing in a green run says "fewer tests ran than last time".

The new cases were merged into the classes that already existed and the duplicates deleted (`2d8e695`),
and `SuiteIntegrity` now reads the file and fails if two test classes share a name. The lesson
generalises past this repo: after adding tests, compare test *counts* with the previous run, because a
shadowed class is silent and a duplicate name is invisible in review.

## Verified after the fixes

161 → 171 tests collected and run (167 in `test_dt.py`, 4 in `test_monitor.py`), all green; the live
publish log is untouched by the run; the four fixed behaviours exercised by hand as well as by test
(the `DT_DRY_RUN` matrix, a real pre-flight refusal, the flag values, `feed` with a handle).
