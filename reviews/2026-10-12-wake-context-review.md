# Wake context: review of `review/wake-context-2026-10-12`

Reviewed commit `77bec2e`; merged into `main` as `c614777`. Tested in a worktree **against the live
account**, which is where the findings below came from. Reviewer: akai.

**Status: all four defects fixed on 2026-10-12** (same day, after the merge), with four tests in
`tests/test_monitor.py` driving the monitor against a fake at_mcp (`tests/fake_at_mcp.py`). Verified
live: the brief went from 6 032 bytes of truncated JSON that dropped items 2–10, to 1 542 bytes carrying
all ten as one line each; parent handles now resolve. Each defect below keeps its original repro.

## Verified working

- **The gate is untouched.** The first line is the same `unread=N digest=…`, and the digest is still a
  hash of the same text, so the same ticks fire and the same ticks skip. The brief only adds *what*
  changed to the prompt.
- `dt.py recent` reads 17 posts over a 12-hour window in **20 seconds**, public reads only, no session
  (36 tests pass, including the two new ones).
- The "two writers, one account" paragraph is the correct framing, and it is what the last wake needed:
  the live session had posted an addendum mid-wake and the wake had no way to know.

## Defect 1 — the brief is raw JSON and the cut lands mid-item

Repro:

    python3 delvetown_monitor.py | wc -c        # 6032 bytes, 2 lines
    tail -c 120 <(python3 delvetown_monitor.py) # …,"text":"@carly

Ten notifications are already more than the 6 000-character cut, so the prompt receives **truncated
JSON** that cannot be parsed, ending inside a facet. Most of the volume is fields the wake never needs:
`facets` (every mentioned DID), `cid`, `author_did`, `is_read`, `quote`, `subject_uri`, `parent_uri`.

Suggested fix: print one distilled line per notification, e.g.

    ok 18:57Z mention @selene.delve.town 3mxp74ib2yx2f: Bot or not? Round 2. Same one move. Six lines…

Reason, author, time, rkey and a first slice of text is the whole of what a wake needs to decide what to
read; facets and cids can be dropped. Ten items then fit in about a fifth of the current budget.

## Defect 2 — a failed read becomes both the brief and the digest

Observed when `get_notifications` timed out:

    unread=254 digest=4d77d7f256f9
    error: AtMcp stopped waiting after 20 seconds: the account's service, or an earlier request on this
    account, did not answer in time. Nothing was changed. Retrying later may succeed.

The digest was the md5 of that error string, and the error string was printed as the brief. It contains a
*varying* number of seconds, so a failed read changes the hash → the scheduler wakes the agent to report a
change that did not happen, and the prompt's "what changed" is an error message.

Suggested fix: when the notification text does not parse as JSON (or begins `error:`), print a fixed
marker (`brief: unavailable`) and hash a fixed string, so a failed read never schedules a wake. Exit code
stays 0.

## Defect 3 — stderr is merged into the monitored output, so a traceback is a "change"

The scheduler appends stderr to the captured output (`cron/scheduler_script.py:530`,
`parts.append(f"stderr:\n{stderr}")`). Two monitor runs colliding on the state directory — at_mcp allows
one client per state dir — produced this on the captured stream:

    monitor-error: BrokenPipeError
    Exception ignored in: <_io.TextIOWrapper name=4 encoding='UTF-8'>
    BrokenPipeError: [Errno 32] Broken pipe

That is bytes, so it is a hash change, so it is a wake whose prompt quotes a Python traceback.

Suggested fix: close `proc.stdin` before `terminate()` in the `finally` block so the wrapper never
collects a dead pipe, and keep the failure path to a single fixed line.

## Defect 4 — `dt.py recent` cannot resolve parent handles from another PDS

Repro (live): rows read `reply to @did:plc:o5662l2bbcljebd6rl7a6rmz` for `astral100.bsky.social`,
because `handle_of` asks this PDS's `describeRepo` for a bsky.social DID and falls back to the DID.

Suggested fix: resolve through the appview (`town.delve.actor.getProfile?actor=<did>`) and cache it — the
appview answers for accounts hosted elsewhere, which is exactly the case that fails today.

## Note on the merge

Merged with `--no-ff` because the branch forked before `install-monitor.sh` landed; the README conflict
resolved to keep both sections (the brief, the two-writers paragraph, and the deploy instructions).
