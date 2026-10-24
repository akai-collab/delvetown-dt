#!/usr/bin/env python3
"""Delvetown change-detector for a Hermes cron monitor.

Prints one deterministic line: unread count + a digest of the notification
reading, then the notification reading itself. Identical output on consecutive
ticks makes the scheduler skip the agent run entirely, so the agent only wakes
when the town has something new — and wakes knowing what it was.

AT_MCP_MONITOR_GATE=wake swaps the digest for a decision (see `wake_output`): the
output changes only when someone has addressed the account and the thread has
had time to settle, and the account can act on it. A like, a follow, a client
that will not answer for one tick, or a run that is rate-limited all leave the
output byte-identical, so none of them wakes the model.

Read-only: never marks notifications seen.

What is answered is dt.py's publish log, not this file's state: a `reply` line answers its parent unless
that reply was deleted, a `decline` line closes an ask. The gate reads it every tick (`answered_asks`) and
drops what it closes, whichever writer answered; and an ask the log shows answered twice wakes the account
once, so a stray second reply is found by a routine rather than by someone opening the thread.

How it reads (DT_MONITOR_READER). `xrpc`, the default, asks the PDS and the appview
directly and keeps ONE session in its own cache, refreshed rather than logged in
again: the PDS allows 300 logins a day per account, and at_mcp logs in on every
start, so a five-minute tick through at_mcp spent 288 of them (live, 2026-10-17).
`at_mcp` is the previous path — the binary, spoken to over MCP stdio, in its OWN
state directory so it can never collide with the lock Hermes' MCP client holds on
the account's main one — kept so a deploy can switch back without a new copy.

Paths default to this machine's layout and can be overridden by environment
variable, so the file is reusable without editing: AT_MCP_BIN, DELVETOWN_ENV_FILE,
AT_MCP_MONITOR_STATE, AT_MCP_HANDLE, AT_MCP_WAKE_STATE, DT_SESSION_CACHE,
DT_PUBLISH_LOG, DT_MONITOR_SESSION, DT_PDS, DT_APPVIEW.

The account password is read at runtime from the env file (never hardcoded here,
never printed, never in the session cache, which holds tokens only). Only the
deterministic lines below reach stdout.
"""
import hashlib
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def _home(*parts):
    """Default paths resolve against the home directory: HERMES_REAL_HOME (Hermes sets it when it
    runs us), else ~. Nothing here is tied to one machine's layout."""
    base = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
    return os.path.join(base, *parts)


BIN = os.environ.get("AT_MCP_BIN", _home("at_mcp", "current", "bin", "at_mcp-stdio"))
BIN_ARGS = shlex.split(os.environ.get("AT_MCP_BIN_ARGS", ""))  # so a test can stand in for the binary
MONITOR_STATE = os.environ.get("AT_MCP_MONITOR_STATE", _home(".local", "state", "at_mcp-monitor"))
HANDLE = os.environ.get("AT_MCP_HANDLE", "akai.delve.town")
ACCOUNT = HANDLE.split(".")[0]      # akai, skillissue, ... — names a seat that has no entry in seats.json


# The seat table, shared with dt.py: identity, the state files each seat reads and writes, and its switches.
# It is a file because this copy cannot import the repo, and the two programs used to disagree about a
# seat's files (2026-10-24: for skillissue, dt.py wrote session-locksmith.json and this read
# session-skillissue.json, so it never saw that seat's lockouts). install-monitor.sh puts it in the state
# directory beside the files it names. Missing is a broken deploy: main() says so in one line rather than
# guess a seat. A handle with no entry keeps the names this file always derived from the handle.
def _seats_path():
    for p in (os.environ.get("DT_SEATS_FILE"), _home(".local", "state", "delvetown-dt", "seats.json"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "seats.json")):
        if p and os.path.isfile(p):
            return p
    return None


def _load_seats(path):
    try:
        with open(path, encoding="utf-8") as f:
            seats = json.load(f).get("seats")
        return seats if isinstance(seats, dict) else None
    except (OSError, TypeError, ValueError, AttributeError):
        return None


SEATS_FILE = _seats_path()
SEATS = _load_seats(SEATS_FILE) if SEATS_FILE else None
SEAT = next((e for e in (SEATS or {}).values()
             if isinstance(e, dict) and str(e.get("handle") or "").lower() == HANDLE.lower()), {})
_SEAT_FILES = SEAT.get("files") or {}
_SEAT_PACE = SEAT.get("pace") or {}


def _state(kind, legacy):
    """A seat's state file: the name seats.json gives it, else the one derived from the handle."""
    return _home(".local", "state", "delvetown-dt", _SEAT_FILES.get(kind) or legacy)

# The env file is where the app password is read from, and its default is akai's own ~/.hermes/.env.
# That is the same wrong-seat failure as the paths below, except this one hands over a credential
# rather than a reading, so a second account must name its own file: with none given the monitor
# reads nothing and reports "no password" rather than logging in as somebody else.
_ENV_FILE_GIVEN = os.environ.get("DELVETOWN_ENV_FILE")
if _ENV_FILE_GIVEN is not None:
    ENV_FILE = _ENV_FILE_GIVEN
elif SEAT:
    ENV_FILE = _home(SEAT["env_file"]) if SEAT.get("env_file") else ""
else:
    ENV_FILE = _home(".hermes", ".env") if ACCOUNT == "akai" else ""
GATE = (os.environ.get("AT_MCP_MONITOR_GATE") or "digest").strip().lower()

# --- the wake gate's knobs. The defaults are guesses until a live account has been watched with them;
# every one is an environment variable so a deploy can tune it without editing the copy.
# Reasons that never wake on their own: a reaction asks nothing of the account. Anything not listed
# here wakes — including a reason this file has never seen, so a new kind of notification is shown to
# the model once rather than silently dropped.
QUIET_REASONS = set(
    (os.environ.get("AT_MCP_QUIET_REASONS") or "like,follow,repost").replace(" ", "").split(","))
SETTLE_S = int(os.environ.get("AT_MCP_WAKE_SETTLE_S") or 600)      # quiet this long after the newest
MAX_WAIT_S = int(os.environ.get("AT_MCP_WAKE_MAX_WAIT_S") or 2700)  # ...unless the oldest waited this
FAIL_TICKS = int(os.environ.get("AT_MCP_WAKE_FAIL_TICKS") or 6)    # failing ticks before one wake
FIRED_TTL_S = 14 * 86400                                            # how long a handled uri is kept
WAKE_LIMIT = 25    # notifications read per tick in wake mode: ten could scroll past between ticks
# What dt.py writes when it is rate-limited or at its own write ceiling. Read here rather than imported:
# the deployed monitor is a lone copy in the profile's scripts dir and cannot import the repo.
# Every file below is named for the account this monitor watches, and dt.py names them the same way:
# the bare name is akai's, any other account gets its own. One machine can run two accounts, and a
# monitor that read akai's lockout or write guard while watching a second account would hold that
# account's wake for a reason that was never its own — and never see its own lockout at all.
WAKE_STATE = os.environ.get("AT_MCP_WAKE_STATE") or _state("wake", "wake-%s.json" % ACCOUNT)
SESSION_CACHE = os.environ.get("DT_SESSION_CACHE") or _state(
    "session", "session.json" if ACCOUNT == "akai" else "session-%s.json" % ACCOUNT)
PUBLISH_LOG = os.environ.get("DT_PUBLISH_LOG") or _state(
    "publish_log", "publish-log.jsonl" if ACCOUNT == "akai" else "publish-log-%s.jsonl" % ACCOUNT)
WRITE_BURST, WRITE_HOUR, GUARDED_KINDS = 6, 16, ("post", "reply", "delete")   # dt.py's write guard
# The quiet list: accounts whose notifications never wake this account. It is for the loop two bots fall
# into when each one's reply is the other's wake, not a judgement on the account: a listed account can
# still be answered by a run that comes across it, nothing is written to the town, and an account that
# should not be answered at all is blocked instead (a block is public). `dt.py quiet` edits it; it is re-read every tick, so a change applies on the next one without a redeploy. Entries are
# matched by DID first: a handle is not stable (octavius became octavia on 19 Oct), and a list keyed by
# handle stops matching through a rename without saying so.
QUIET_FILE = os.environ.get("DT_QUIET_FILE") or _state(
    "quiet", "quiet.json" if ACCOUNT == "akai" else "quiet-%s.json" % ACCOUNT)
# Thread pacing, in the notification layer only: a notification in a thread where this account has just
# been answering one counterpart waits longer before it may wake the account, and the wait doubles with
# each turn — PACE_PAIR_S after the first reply to that account there, twice that after the second, and
# so on; PACE_RESET_S with no reply in the thread resets it. So a slow deep thread goes on and a rapid
# back-and-forth between bots, each waking the other, slows down. It delays a WAKE and nothing else: it
# never refuses a reply a run decided to write, and the clock wakes and sessions are not affected. Read
# off the publish log, so no network and nothing that depends on the run's restraint.
#   PACE_THREAD_S is a second clock for the thread as a whole, off (0) by default: in a thread with
# several people, one answer must not delay the wake for the next person's ask.
#   The case it is built from: seven replies to one other bot in one thread between 04:38Z and 10:35Z on
# 19 Oct, the last of them to this account's own post. 0 turns a clock off.
#   Each seat's numbers are in seats.json ("pace"); the environment still wins, and these are the defaults
# for a seat that sets none.
PACE_PAIR_S = int(os.environ.get("DT_PACE_PAIR_S") or _SEAT_PACE.get("pair_s", 1200))
PACE_THREAD_S = int(os.environ.get("DT_PACE_THREAD_S") or _SEAT_PACE.get("thread_s", 0))
PACE_RESET_S = int(os.environ.get("DT_PACE_RESET_S") or _SEAT_PACE.get("reset_s", 6 * 3600))
# A batch whose run failed is sent again. The scheduler marks a monitor's output as seen when it CHECKS,
# before the model runs (Hermes cron/monitor.py, on purpose: "so a failed agent run doesn't re-alert on
# the same content forever"), so a run that dies — a 429 from the model provider, say — leaves its batch
# looking handled, and the next tick is "no change" until somebody else happens to address the account.
# On 20 Oct a run failed at 19:29Z that way and selene's mention sat unanswered. So the monitor reads its
# own job's last failure from the scheduler's jobs.json, and if that failure is newer than the batch it
# printed, it prints the same batch again with a retry line — a change, so the model runs. Waits
# RETRY_WAIT_S, then twice that, then four times; gives up after RETRY_MAX (the scheduler's own failure
# alert has reached the operator by then). Off unless the deploy names the job: AT_MCP_WAKE_JOB_ID and
# AT_MCP_WAKE_JOBS_FILE (the profile's cron/jobs.json).
RETRY_JOB_ID = (os.environ.get("AT_MCP_WAKE_JOB_ID") or "").strip()
RETRY_JOBS_FILE = (os.environ.get("AT_MCP_WAKE_JOBS_FILE") or "").strip()
RETRY_WAIT_S = int(os.environ.get("AT_MCP_WAKE_RETRY_WAIT_S") or 300)
RETRY_MAX = int(os.environ.get("AT_MCP_WAKE_RETRY_MAX") or 3)
# A batch whose run answered none of it is offered once more. The retry above covers a run that FAILED; a run
# that exits zero having written nothing left its batch in `fired` for FIRED_TTL_S, out of sight, until
# something else changed the output (2026-10-24: asks on both seats waited 45-60 minutes for an operator to
# fire the gate by hand). Bounded by construction rather than by restraint: only a batch with no answer and
# no decline at all — one answer means the run read the batch and judged the rest; only after REOFFER_AFTER_S
# (longer than a settle window and a slow run) and before REOFFER_WINDOW_S, so history is never re-offered;
# each uri at most once; and at most REOFFER_MAX re-offers in any 24 hours.
REOFFER_AFTER_S = int(os.environ.get("AT_MCP_WAKE_REOFFER_AFTER_S") or 2700)
REOFFER_WINDOW_S = int(os.environ.get("AT_MCP_WAKE_REOFFER_WINDOW_S") or 7200)
REOFFER_MAX = int(os.environ.get("AT_MCP_WAKE_REOFFER_MAX") or 12)

# --- the direct reader (DT_MONITOR_READER=xrpc). Its session cache is its own, not dt.py's:
# refreshSession rotates the refresh token, so two programs taking turns with one cache would each be
# left holding a token the other had already spent.
READER = (os.environ.get("DT_MONITOR_READER") or "xrpc").strip().lower()
PDS = (os.environ.get("DT_PDS") or "https://pds.delve.town").rstrip("/")
APPVIEW = (os.environ.get("DT_APPVIEW") or "https://api.delve.town").rstrip("/")
APPVIEW_DID = os.environ.get("DT_APPVIEW_DID") or "did:web:api.delve.town"
MONITOR_SESSION = os.environ.get("DT_MONITOR_SESSION") or _state(
    "monitor_session", "monitor-session-%s.json" % ACCOUNT)
SESSION_MARGIN = 120        # seconds of life a cached token must still have to be used
LOGIN_REFUSED_WAIT = 3600   # after a wrong password, do not try again for this long
HTTP_TIMEOUT = 30


def env_from_file(path):
    vals = {}
    if not path:      # no file named on purpose: read nothing rather than someone else's secret
        return vals
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


MAX_ITEMS = 10
MAX_TEXT = 200


def _items(text):
    """The notification items, or None if the reading is not the JSON at_mcp answers with."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def _line(it):
    when = (it.get("indexed_at") or "")[11:16]
    reason = (it.get("reason") or "?").replace("_", " ")
    author = it.get("author") or "?"
    rkey = (it.get("uri") or "/").split("/")[-1]
    body = " ".join((it.get("text") or "").split())
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT].rstrip() + "…"
    return f"{when}Z {reason} @{author} {rkey}: {body}"


def _newest_epoch(items):
    """The newest indexed_at in a tick's items, as epoch seconds (0 when nothing parses)."""
    best = 0.0
    for it in items or []:
        raw = it.get("indexed_at")
        if not raw:
            continue
        try:
            best = max(best, datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            continue
    return best


def brief(text):
    """Distilled one-liners for the notification reading, or None if it is not readable.

    at_mcp hands this back as JSON with every mention facet, cid and DID attached. Ten items is over
    6 kB, and one mention-heavy post can exceed the whole of the earlier 6 000-character cut — the
    reading then ended mid-facet and every notification after the first was silently dropped (live
    2026-10-12: selene's post tagging 36 accounts ate the entire budget). A wake needs reason,
    author, time, rkey and a slice of the text; nothing here reads the facets.
    """
    items = _items(text)
    if items is None:
        return None
    return [_line(it) for it in items[:MAX_ITEMS]]


def read_at_mcp(limit):
    """Ask at_mcp for the unread count and the newest `limit` notifications.

    Returns (unread, notes_text, fail): `fail` names why nobody answered, or is None.
    """
    os.makedirs(MONITOR_STATE, mode=0o700, exist_ok=True)
    # at_mcp VALIDATES its environment and refuses to boot on an unknown AT_MCP_* variable:
    #   ERROR! Config provider Config.Reader failed with: (ArgumentError)
    #   AT_MCP_MONITOR_GATE is not an AtMcp setting; did you mean AT_MCP_GRANT?
    # Our own opt-ins are AT_MCP_-prefixed too (GATE, QUIET_REASONS, WAKE_*, MONITOR_*), so passing
    # our whole environment through killed the client this file talks to — the gate's documented
    # opt-in made every read fail with "no answer" (2026-10-17, live). Hand over the settings at_mcp
    # accepts and nothing else.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AT_MCP_")}
    env.update({
        "AT_MCP_NETWORK": "delve",
        "AT_MCP_SERVICE": PDS,
        "AT_MCP_HANDLE": HANDLE,
        "AT_MCP_APP_PASSWORD": env_from_file(ENV_FILE).get("AT_MCP_APP_PASSWORD", ""),
        "AT_MCP_STATE_DIR": MONITOR_STATE,
    })
    unread, notes_text, fail = "?", "", None   # every path below must end with a gate line
    try:
        proc = subprocess.Popen([BIN] + BIN_ARGS, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
    except Exception as exc:
        return "?", "", "spawn " + type(exc).__name__

    assert proc.stdin is not None and proc.stdout is not None  # PIPE, so both are real handles

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_until(mid, limit=80):
        for _ in range(limit):
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == mid:
                return msg
        return None

    def call_text(mid, name, args=None):
        """Raw text of a tool result (at_mcp renders reads as prose, not JSON)."""
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}})
        r = read_until(mid)
        try:
            return r["result"]["content"][0]["text"] or ""
        except Exception:
            return ""

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "delvetown-monitor", "version": "2"}}})
        read_until(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        unread_text = call_text(10, "get_unread_count")
        notes_text = call_text(11, "get_notifications", {"limit": limit})
        try:
            unread = json.loads(unread_text).get("count")
        except Exception:
            unread = "?"
    except Exception as exc:
        # A dead or wedged client used to print "monitor-error: …" *instead of* the gate line, so the
        # hash flapped between error text and real output — and a persistent failure would have woken
        # the agent on every tick. Print the gate line always: the first failing tick differs from the
        # last good one (so exactly one informative wake fires, naming the exception), and identical
        # ticks after that are suppressed, which is what a broken client deserves.
        unread, notes_text, fail = "?", "", type(exc).__name__
        # A client that dies surfaces as BrokenPipeError or as an empty read depending on whether our
        # write lands before or after it exits — a race, so the same failure printed two different
        # lines on consecutive ticks and woke the agent anyway. Both are one fact: nobody answered.
        if fail in ("BrokenPipeError", "ConnectionResetError", "EOFError"):
            fail = "no answer"
    finally:
        # Close our ends first: leaving them open makes the interpreter emit "Exception ignored in:
        # <_io.TextIOWrapper ...> BrokenPipeError" at collection time, and the scheduler merges stderr
        # into the monitored output (cron/scheduler_script.py), so that traceback would itself register
        # as a change and wake the agent to read Python.
        for stream in (proc.stdin, proc.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
    if not fail and not notes_text:
        fail = "no answer"   # the empty-read side of the same race
    return unread, notes_text, fail


# ------------------------------------------------------------------------------- the direct reader
def _http(method, url, body=None, token=None, params=None):
    """(status, json, headers). Never raises: a network failure is status 0, so every caller ends in a
    gate line rather than a traceback (stderr is merged into the monitored output)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "delvetown-monitor/3"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        return e.code, payload if isinstance(payload, dict) else {}, dict(e.headers or {})
    except Exception:
        return 0, {}, {}


def _jwt_exp(tok):
    """A JWT's `exp` claim, unverified — enough to decide whether to use it. Anything unreadable: None."""
    try:
        import base64
        part = str(tok).split(".")[1]
        return int(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except Exception:
        return None


def _usable(tok, now):
    exp = _jwt_exp(tok) if tok else None
    return bool(exp) and exp - now > SESSION_MARGIN


def _retry_after(headers, now):
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    for key, base in (("retry-after", 0), ("ratelimit-reset", now)):
        try:
            secs = int(float(h.get(key) or 0) - base)
        except (TypeError, ValueError):
            secs = 0
        if secs > 0:
            return secs
    return 60


def _locked_until(path):
    """The later of a recorded login lockout and a write lockout in a session cache, or 0."""
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return max(float(c.get("login_locked_until") or 0), float(c.get("locked_until") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _write_cache(c, path=None):
    """0600 from the first byte. A cache that cannot be written costs the next tick a login, not this
    tick its reading."""
    path = path or MONITOR_SESSION
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _tokens(s, **extra):
    """What is kept of a session answer: the tokens and whose they are. Nothing else the server sent."""
    return dict({k: s.get(k) for k in ("did", "accessJwt", "refreshJwt")}, handle=HANDLE, **extra)


def _session(now, stale=None):
    """An access token for this account, and the reason there is none: (token, fail).

    The cached access token while it lives; then refreshSession, which does not spend a login; and
    createSession only when both are gone — so the monitor's steady state is no logins at all. A login
    is never attempted while either this cache or dt.py's records a lockout (the budget is the
    account's, not the program's), or within LOGIN_REFUSED_WAIT of a wrong password: a refused login
    retried every tick would itself spend the budget. `stale` is an access token the server has just
    rejected, so the cache is not trusted to still hold a live one.
    """
    lock = None
    try:
        import fcntl
        os.makedirs(os.path.dirname(MONITOR_SESSION) or ".", mode=0o700, exist_ok=True)
        lock = open(MONITOR_SESSION + ".lock", "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass
    try:
        try:
            with open(MONITOR_SESSION, encoding="utf-8") as f:
                c = json.load(f)
            c = c if isinstance(c, dict) and c.get("handle") == HANDLE else {}
        except (OSError, ValueError):
            c = {}
        if _usable(c.get("accessJwt"), now) and c.get("accessJwt") != stale:
            return c["accessJwt"], None
        if _usable(c.get("refreshJwt"), now):
            st, s, hdrs = _http("POST", PDS + "/xrpc/com.atproto.server.refreshSession",
                                token=c["refreshJwt"])
            if st == 200 and s.get("accessJwt"):
                _write_cache(_tokens(s, refreshed_at=now, logged_in_at=c.get("logged_in_at")))
                return s["accessJwt"], None
            if st == 429:
                _write_cache(dict(c, locked_until=now + _retry_after(hdrs, now)))
                return None, "rate-limited"
            if st not in (400, 401):   # only a refused refresh token is worth a login; an outage is not
                return None, "no answer" if st == 0 else "http %d" % st
        if now < max(_locked_until(MONITOR_SESSION), _locked_until(SESSION_CACHE)):
            return None, "rate-limited"
        if now < float(c.get("login_refused_until") or 0):
            return None, "login refused"
        password = env_from_file(ENV_FILE).get("AT_MCP_APP_PASSWORD", "")
        if not password:
            return None, "no password"
        st, s, hdrs = _http("POST", PDS + "/xrpc/com.atproto.server.createSession",
                            {"identifier": HANDLE, "password": password})
        del password
        h = {str(k).lower(): v for k, v in hdrs.items()}
        if st == 200 and s.get("accessJwt"):
            _write_cache(_tokens(s, logged_in_at=now, login_budget_left=h.get("ratelimit-remaining")))
            return s["accessJwt"], None
        if st == 429:
            _write_cache({"handle": HANDLE, "login_locked_until": now + _retry_after(hdrs, now)})
            return None, "rate-limited"
        if st in (400, 401):
            _write_cache({"handle": HANDLE, "login_refused_until": now + LOGIN_REFUSED_WAIT})
            return None, "login refused"
        return None, "no answer" if st == 0 else "login http %d" % st
    finally:
        if lock:
            lock.close()


def _appview(token, lxm, params):
    """(status, body) of one appview read, through a service-auth token minted for that one method."""
    st, sa, _ = _http("GET", PDS + "/xrpc/com.atproto.server.getServiceAuth", token=token,
                      params={"aud": APPVIEW_DID, "lxm": lxm})
    if st != 200 or not sa.get("token"):
        return st or 0, sa
    st, body, _ = _http("GET", APPVIEW + "/xrpc/" + lxm, token=sa["token"], params=params)
    return st, body


def _item(n):
    """The appview's notification in the shape the gate reads (the same fields at_mcp handed over)."""
    author = n.get("author") if isinstance(n.get("author"), dict) else {}
    record = n.get("record") if isinstance(n.get("record"), dict) else {}
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    root = (reply.get("root") or {}).get("uri") or (reply.get("parent") or {}).get("uri")
    return {"reason": n.get("reason"), "author": author.get("handle") or author.get("did"),
            "author_did": author.get("did"), "root": root,
            "uri": n.get("uri"), "indexed_at": n.get("indexedAt"), "is_read": n.get("isRead"),
            "text": record.get("text") or ""}


def read_xrpc(limit):
    """The unread count and the newest `limit` notifications, read directly. Same contract as
    `read_at_mcp`: (unread, notes_text, fail), where `fail` is a short fixed phrase — never a server
    message, whose wording could vary between ticks and wake the model on noise."""
    now = time.time()
    token, fail = _session(now)
    if fail:
        return "?", "", fail
    lxm = "town.delve.notification.listNotifications"
    st, res = _appview(token, lxm, {"limit": limit})
    if st == 401:                      # expired or revoked early: once more with a refreshed token
        token, fail = _session(now, stale=token)
        if fail:
            return "?", "", fail
        st, res = _appview(token, lxm, {"limit": limit})
    if st != 200 or not isinstance(res.get("notifications"), list):
        return "?", "", "rate-limited" if st == 429 else "no answer" if st == 0 else "http %d" % st
    items = [_item(n) for n in res["notifications"] if isinstance(n, dict)]
    st, cnt = _appview(token, "town.delve.notification.getUnreadCount", None)
    unread = cnt.get("count", "?") if st == 200 else "?"
    return unread, json.dumps({"count": len(items), "items": items}, sort_keys=True), None


def read_town(limit):
    return read_at_mcp(limit) if READER == "at_mcp" else read_xrpc(limit)


def digest_output(unread, notes_text, fail):
    """The original gate: any change in the newest notifications wakes the model."""
    lines = None if fail and not notes_text else brief(notes_text)
    # One output point. A reading that will not parse and a client that will not answer both hash to a
    # fixed digest: at_mcp's error strings carry a varying number of seconds ("stopped waiting after 20
    # seconds"), and hashing one makes the gate fire on noise while handing the prompt an error message
    # as if it were the town's news. The digest of a readable reading still hashes the same text as
    # before, so wake timing on the happy path is unchanged.
    digest = "unavailable" if lines is None else (
        hashlib.md5(notes_text.encode("utf-8")).hexdigest()[:12] if notes_text else "empty")
    out = [f"unread={unread} digest={digest}"]
    if lines is None:
        out.append("brief: unavailable" + (f" ({fail})" if fail else ""))
    elif os.environ.get("AT_MCP_MONITOR_BRIEF") != "1":
        out.extend(lines)
    return "\n".join(out)


# ----------------------------------------------------------------------------------- the wake gate
def held_reason(now, session_cache=None, publish_log=None):
    """Why the account could not act on a wake right now, or None.

    A wake the model cannot act on is the expensive kind: it reads the inbox, composes an answer, and
    hits the same wall. dt.py records a PDS lockout in its session cache (a login's 429, or a write's)
    and keeps a publish log its write guard counts; both are read here, and a held batch simply waits
    for the next tick on which it can be answered.
    """
    until = _locked_until(session_cache or SESSION_CACHE)
    if now < until:
        return "rate-limited until " + datetime.fromtimestamp(until, timezone.utc).strftime("%H:%MZ")
    burst = hour = 0
    try:
        with open(publish_log or PUBLISH_LOG, encoding="utf-8") as f:
            tail = f.readlines()[-200:]
    except OSError:
        tail = []
    for line in tail:
        try:
            e = json.loads(line)
            if e.get("kind") not in GUARDED_KINDS or e.get("adopted"):
                continue
            age = now - datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            continue
        if 0 <= age <= 3600:
            hour += 1
            burst += age <= 600
    if burst >= WRITE_BURST or hour >= WRITE_HOUR:
        return f"write guard full ({burst}/10min, {hour}/60min)"
    return None


# What a gate that has never fired prints. Never empty: an empty tick is the one output a scheduler
# might read as "the script printed nothing" rather than "nothing changed".
QUIET_LINE = "wake=none"


def wake_output(items, state, now, held=None, quiet=None, replies=None, failed_at=None, answered=None,
                doubles=None):
    """Decide whether this tick wakes the model. Returns (output, new_state).

    The scheduler wakes the model when the output differs from the last tick's, so the decision is
    expressed as a change: a tick that should not wake prints the previous output again, byte for
    byte, and only a batch worth answering prints something new. What a quiet tick prints is never
    read by anyone — only whether it changed.

    - `items` is the notification list, or None when the client did not answer. One failing tick does
      not wake; FAIL_TICKS in a row do, once, so an outage is reported rather than read every tick.
    - an item is actionable unless its reason is quiet (a like, a follow, a repost); each is handled
      once, keyed by uri, so the same reply never wakes twice.
    - a batch waits until SETTLE_S has passed since the newest actionable item was first seen (a reply
      arriving mid-thread is usually followed by another), but never longer than MAX_WAIT_S since the
      oldest — a busy thread cannot postpone its own answer for ever.
    - `held` (see `held_reason`) postpones a ripe batch without dropping it.
    - an item from an account on the quiet list never wakes (`quiet`, read from QUIET_FILE by default);
      an item in a thread this account has been answering fast waits out its pace (`replies`, read from
      the publish log by default; see `paced_until`).
    - `failed_at` is when the scheduler last recorded this job failing (see `last_failure_at`). A failure
      after the last batch fired means that batch's run died, and the batch is printed again with a
      retry line — waiting RETRY_WAIT_S, doubling, at most RETRY_MAX times — unless the log shows every
      ask in it answered or declined since: that run did its work before it died.
    - `answered` is the set of asks the publish log closes (see `answered_asks`): one never wakes and
      leaves the queue, whoever answered it.
    - `doubles` are asks the log shows answered twice (see `double_answers`; main() passes them). Each
      one not reported yet is a change, so it wakes the account once with the two replies named.
    """
    st = {"fired": dict(state.get("fired") or {}), "pending": dict(state.get("pending") or {}),
          "fails": int(state.get("fails") or 0), "last": state.get("last") or QUIET_LINE,
          "fired_at": state.get("fired_at"), "base": state.get("base"), "retries": int(state.get("retries") or 0),
          "newest": float(state.get("newest") or 0), "batch": list(state.get("batch") or []),
          "doubled": list(state.get("doubled") or []),
          "recent": [b for b in (state.get("recent") or []) if isinstance(b, dict)
                     and now - float(b.get("at") or 0) <= REOFFER_WINDOW_S],
          "reoffered": {u: t for u, t in (state.get("reoffered") or {}).items()
                        if now - float(t) < FIRED_TTL_S},
          "reoffers": [t for t in (state.get("reoffers") or []) if now - float(t) < 86400]}
    st["fired"] = {u: t for u, t in st["fired"].items() if now - float(t) < FIRED_TTL_S}
    answered = answered_asks() if answered is None else answered
    doubles = doubles or []          # main() reads them; a caller that passes none checks none

    def closed(uri):
        return uri in answered or ("rkey:" + str(uri).split("/")[-1]) in answered

    # A second reply to one ask is a public mistake already made, so it does not wait for the inbox, a
    # settle window or a hold: it is reported the tick it appears in the log, once, by the second reply's
    # uri. Checked before the reading, so a town that will not answer cannot hide it.
    fresh = [d for d in doubles if d.get("second") and d["second"] not in st["doubled"]]
    if fresh:
        st["doubled"] = (st["doubled"] + [d["second"] for d in fresh])[-200:]
        out = ["wake=double n=%d id=%s" % (len(fresh), hashlib.md5(
            "\n".join(d["second"] for d in fresh).encode("utf-8")).hexdigest()[:12])]
        for d in fresh:
            out.append("double: %s answered twice — %s and %s" % (
                str(d["ask"]).split("/")[-1], d["first"], d["second"]))
        out.append("read both in the thread; if one is a slip (a token, a fragment, a repeat) delete it with "
                   "`dt.py delete <rkey>`; if both were meant, nothing is needed — the next reply to an "
                   "answered ask should carry --again")
        st["last"] = "\n".join(out)
        st["fired_at"] = None            # a report, not a batch: nothing to send again
        return st["last"], st

    if items is None:
        st["fails"] += 1
        if st["fails"] == FAIL_TICKS:
            st["last"] = (f"wake=unavailable ticks={FAIL_TICKS}\n"
                          f"brief: the notification client has not answered for {FAIL_TICKS} ticks")
            st["fired_at"] = None        # an outage report is not a batch: nothing to send again
        return st["last"], st
    st["fails"] = 0

    # A reading that goes backwards is not a new batch. The appview can answer with a stale page (a
    # refreshed session cache, a lagging index), and publishing that as `last` rewinds the gate: the
    # brief it prints is older than the one it printed an hour ago, so the scheduler sees a change and
    # wakes the model to answer what it already answered. Hold the newer brief and ignore the rewind.
    indexed_max = _newest_epoch(items)
    if indexed_max and st.get("newest") and indexed_max < st["newest"] - 1:
        return st["last"], st

    if (failed_at and st["fired_at"] and st["base"] and failed_at > float(st["fired_at"]) and not held
            and not (st["batch"] and all(closed(u) for u in st["batch"])) and st["retries"] < RETRY_MAX and now - failed_at >= RETRY_WAIT_S * 2 ** st["retries"]):
        st["retries"] += 1
        st["fired_at"] = now
        st["last"] = st["base"] + "\nretry %d of %d: the run for this batch failed at %s and the batch is " \
            "sent again. That run may have written part of it before it died: check `dt.py recent` and the " \
            "threads before answering, so nothing is answered twice." % (
                st["retries"], RETRY_MAX, datetime.fromtimestamp(failed_at, timezone.utc).strftime("%H:%MZ"))
        return st["last"], st

    # A recent batch whose run answered and declined nothing: offered once more (see REOFFER_AFTER_S).
    again = []
    if not held and len(st["reoffers"]) < REOFFER_MAX:
        for b in st["recent"]:
            uris = [it.get("uri") for it in b.get("items") or []]
            # a failure after the batch is the retry's (above), however many retries it has left
            if (REOFFER_AFTER_S <= now - float(b["at"]) <= REOFFER_WINDOW_S and uris
                    and not (failed_at and failed_at >= float(b["at"]))
                    and not any(closed(u) for u in uris)):
                again += [it for it in b["items"] if it.get("uri") not in st["reoffered"]]
    if again:
        for it in again:
            st["reoffered"][it["uri"]] = now
        st["reoffers"].append(now)
        ident = hashlib.md5("\n".join(it["uri"] for it in again).encode("utf-8")).hexdigest()[:12]
        out = ["wake=%d reoffer id=%s" % (len(again), ident)]
        out.extend(_line(it) for it in again)
        out.append("reoffer: the run woken for these answered and declined none of them. Offered once more, "
                   "and not again: answer what is owed, and `dt.py decline <uri>` what is not.")
        st["last"] = "\n".join(out)
        st["fired_at"] = None            # not a batch of its own: a failure after it is not retried
        return st["last"], st

    muted_names = quiet if quiet is not None else quiet_list()
    replies = own_replies(now) if replies is None else replies
    actionable, reactions, muted = [], [], []
    for it in items:
        uri = it.get("uri")
        if not uri or uri in st["fired"] or closed(uri):
            continue
        if (it.get("reason") or "") in QUIET_REASONS:
            reactions.append(it)
        elif is_muted(it, muted_names):
            muted.append(it)
        else:
            actionable.append(it)
    # A pending entry keeps the item itself, not only when it was first seen: the window is only the
    # newest WAKE_LIMIT notifications, and while a batch is held (a lockout can last sixteen hours) or
    # settling, likes alone can scroll a reply out of it. Pruning to the window dropped exactly the
    # batch "held, not dropped" promises to keep. An entry now leaves only by firing or, unanswered,
    # after FIRED_TTL_S; a reply deleted meanwhile is shown once from what was kept, which is cheaper
    # than an answer that never comes.
    pending = {}
    for u, v in st["pending"].items():
        if isinstance(v, dict) and isinstance(v.get("item"), dict):
            pending[u] = {"t": float(v["t"]) if isinstance(v.get("t"), (int, float)) else now,
                          "item": v["item"]}
        elif isinstance(v, (int, float)):            # state written before items were kept
            pending[u] = {"t": float(v), "item": None}
    for it in actionable:
        entry = pending.setdefault(it["uri"], {"t": now, "item": None})
        entry["item"] = it                           # the freshest reading of it
    # An account put on the quiet list while its item was already waiting is quiet from now on: the
    # list is read every tick so that a change applies at once, and that includes what is in the queue.
    for u, e in list(pending.items()):
        if e["item"] is not None and is_muted(e["item"], muted_names):
            pending.pop(u)
            if u not in {it["uri"] for it in muted}:
                muted.append(e["item"])
    # An entry answered meanwhile — by a session, a clock wake, anyone — leaves here: the log said so.
    st["pending"] = {u: e for u, e in pending.items()
                     if e["item"] is not None and now - e["t"] < FIRED_TTL_S and not closed(u)}
    if not st["pending"]:
        return st["last"], st

    # A paced item stays in the queue, neither dropped nor counted: it cannot make a batch, and a batch
    # that fires for something else leaves it waiting. When its thread's wait runs out it becomes an
    # ordinary pending item, and since it has been waiting that long it is ripe at once — the answer is
    # late, not lost, which is the point: the thread goes on, at a pace.
    paced = {u: paced_until(e["item"], now, replies) for u, e in st["pending"].items()}
    ready = {u: e for u, e in st["pending"].items() if not paced[u]}
    if not ready:
        return st["last"], st

    seen = [e["t"] for e in ready.values()]
    newest, oldest = max(seen), min(seen)
    ripe = now - newest >= SETTLE_S or now - oldest >= MAX_WAIT_S
    if not ripe or held:
        return st["last"], st

    batch = sorted((e["item"] for e in ready.values()),
                   key=lambda it: it.get("indexed_at") or "")
    counts = {}
    for it in batch:
        counts[it.get("reason") or "?"] = counts.get(it.get("reason") or "?", 0) + 1
    ident = hashlib.md5("\n".join(it["uri"] for it in batch).encode("utf-8")).hexdigest()[:12]
    out = ["wake=%d %s id=%s" % (len(batch), " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                                 ident)]
    out.extend(_line(it) for it in batch)
    also = {}
    for it in reactions:
        also[it.get("reason") or "?"] = also.get(it.get("reason") or "?", 0) + 1
    if muted:
        also["muted"] = len(muted)       # a count, not the items: the list exists so they are not read
    if also:
        out.append("also: " + ", ".join(f"{v} {k}" for k, v in sorted(also.items())))
    if st["batch"]:
        # How the previous batch went, so a run that answered part of it is visible from the report alone.
        # Abstained is its own count: a run that looked and correctly wrote nothing is not a dead run.
        prev = [u for u in st["batch"] if u]
        how = [answered.get(u) or answered.get("rkey:" + str(u).split("/")[-1]) if isinstance(answered, dict)
               else ("reply" if closed(u) else None) for u in prev]
        out.append("previous batch of %d: %d answered, %d declined, %d abstained, %d untouched" % (
            len(prev), how.count("reply"), how.count("decline"), how.count("abstain"), how.count(None)))
    waiting = [paced[u] for u in st["pending"] if paced[u]]
    if waiting:
        # Said so that a run does not go looking for the thread itself: it is being answered later.
        out.append("paced: %d waiting, next at %s" % (
            len(waiting), datetime.fromtimestamp(min(waiting), timezone.utc).strftime("%H:%MZ")))
    for it in batch + reactions + muted:  # reported with the batch they arrived beside, then done
        st["fired"][it["uri"]] = now
    st["pending"] = {u: e for u, e in st["pending"].items() if paced[u]}
    st["last"] = st["base"] = "\n".join(out)
    st["newest"] = max(float(st.get("newest") or 0), indexed_max)
    st["fired_at"], st["retries"], st["batch"] = now, 0, [it["uri"] for it in batch]
    st["recent"].append({"at": now, "items": batch})
    return st["last"], st


def last_failure_at(path=None, job_id=None):
    """When the scheduler last recorded this job failing (epoch), or None. Reads the profile's
    cron/jobs.json: `last_failure.at` survives the next successful tick, unlike `last_status`."""
    path, job_id = path or RETRY_JOBS_FILE, job_id or RETRY_JOB_ID
    if not path or not job_id:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        jobs = docs.get("jobs") if isinstance(docs, dict) else docs
        job = next((j for j in jobs or [] if isinstance(j, dict) and j.get("id") == job_id), None)
        at = ((job or {}).get("last_failure") or {}).get("at")
        return datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() if at else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# ------------------------------------------------------------------- the quiet list and the pacing
# The pacing lives here only. The quiet list's file is shared with dt.py, which edits it (`dt.py quiet`)
# with its own reader: the deployed monitor is a lone file in the profile's scripts dir and cannot import
# the repo, and tests/test_pacing.py holds the two readers to the same file format.
def quiet_list(path=None):
    """The quiet list as a set of lowercased DIDs and handles, or an empty set.

    The file is `{"accounts": [{"did": ..., "handle": ..., "note": ...}]}` as `dt.py quiet` writes it; a
    bare list of names, or `{"handles": [...]}` (the 19 Oct stopgap's shape), is read too. Anything
    unreadable is an empty list, never an error: stderr is merged into the monitored output."""
    try:
        with open(path or QUIET_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("accounts") if "accounts" in data else data.get("handles")
    names = set()
    for e in data if isinstance(data, list) else []:
        for v in ((e.get("did"), e.get("handle")) if isinstance(e, dict) else (e,)):
            if isinstance(v, str) and v.strip():
                names.add(v.strip().lstrip("@").lower())
    return names


def is_muted(it, names):
    return bool(names) and bool({str(it.get("author_did") or "").lower(),
                                 str(it.get("author") or "").lower()} & names)


def _did_of(uri):
    """The repo (author) segment of an at:// uri, or None."""
    parts = str(uri or "").split("/")
    return parts[2] if len(parts) > 2 and str(uri).startswith("at://") else None


def own_replies(now, publish_log=None):
    """This account's recent replies as (time, thread root, DID answered), oldest first, from the
    publish log. Only what can still bear on a wait: PACE_RESET_S is the longest any wait can be, so a
    run is cut where a gap that long appears, and nothing older than a day is read at all."""
    out = []
    try:
        with open(publish_log or PUBLISH_LOG, encoding="utf-8") as f:
            tail = f.readlines()[-500:]
    except OSError:
        return out
    for line in tail:
        try:
            e = json.loads(line)
            if e.get("kind") != "reply" or e.get("adopted"):   # found later: its time is not the reply's
                continue
            t = datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            continue
        root = e.get("root_uri") or e.get("parent_uri")
        if root and 0 <= now - t <= 86400:
            out.append((t, root, _did_of(e.get("parent_uri"))))
    return sorted(out)


def _log_entries(publish_log=None):
    try:
        with open(publish_log or PUBLISH_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def answered_asks(publish_log=None, entries=None):
    """{ask: how it was closed} from dt.py's publish log: `reply` for the parent of every reply whose record
    no later `delete` removed, `decline`, and `abstain` — a run looked and chose not to act, which closes the
    ask here (no wake, no re-offer) though a session may still answer it. An abstain never overrides a reply
    or a decline unless it says `replaces_decline`, the rule dt.py's `ask_states` applies. A reply logged before 2026-10-15 carries only its
    parent's rkey, kept as `rkey:<rkey>`. Read whole every tick — a few thousand short lines, and a tail
    would forget an old answer exactly when a stale page brings its ask back. dt.py's `ask_states` reads
    the same lines the same way; tests/test_answered.py holds the two to one answer. Unreadable: empty,
    which is the gate as it was before it read the log, never worse."""
    entries = _log_entries(publish_log) if entries is None else entries
    deleted = {e.get("uri") for e in entries if e.get("kind") == "delete"}
    out = {}
    for e in entries:
        kind = e.get("kind")
        if kind == "reply" and e.get("uri") not in deleted:
            key = e.get("parent_uri") or ("rkey:%s" % e["parent"] if e.get("parent") else None)
        elif kind in ("decline", "abstain"):
            key = e.get("subject")
        else:
            continue
        if not key:
            continue
        held = out.get(key)
        if kind == "abstain" and (held == "reply" or (held == "decline" and not e.get("replaces_decline"))):
            continue
        out[key] = kind
    return out


def double_answers(publish_log=None, entries=None):
    """Asks answered twice: neither reply deleted, neither adopted from the repo, the second not sent with
    --again, the parent not a post of our own. The same rule as dt.py's `double_answers`."""
    entries = _log_entries(publish_log) if entries is None else entries
    deleted = {e.get("uri") for e in entries if e.get("kind") == "delete"}
    first, out = {}, []
    for e in entries:
        if e.get("kind") != "reply" or not e.get("parent_uri") or e.get("adopted") or e.get("uri") in deleted:
            continue
        if _did_of(e["parent_uri"]) == _did_of(e.get("uri")):
            continue        # a follow-up under a post of our own: a thread being written, not an ask
        p = e["parent_uri"]
        if p in first and not e.get("again"):
            out.append({"ask": p, "first": first[p], "second": e.get("uri"), "at": e.get("ts")})
        first.setdefault(p, e.get("uri"))
    return out


DOUBLE_WINDOW_S = 86400


def _epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def pace_wait(times, base):
    """When the next reply may go, given the earlier ones' times (oldest first): the last one plus base,
    doubled for each reply before it in the same unbroken run. None when nothing is waiting on."""
    if base <= 0 or not times:
        return None
    n = 1
    for earlier, later in zip(reversed(times[:-1]), reversed(times[1:])):
        if later - earlier >= PACE_RESET_S:
            break
        n += 1
    return times[-1] + min(base * 2 ** (n - 1), PACE_RESET_S)


def paced_until(it, now, replies):
    """The time until which this item's thread is paced, or None if it may be answered now.

    The pair clock counts this account's replies to the item's author in the item's thread; the thread
    clock counts all its replies there. An item with no thread root (a mention in a root post, or a
    reading from at_mcp, whose items do not carry one) is never paced."""
    root = it.get("root")
    if not root:
        return None
    who = it.get("author_did")
    thread = [t for t, r, _ in replies if r == root]
    pair = [t for t, r, d in replies if r == root and who and d == who]
    until = max([w for w in (pace_wait(pair, PACE_PAIR_S), pace_wait(thread, PACE_THREAD_S)) if w]
                or [0])
    return until if until > now else None


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, st):
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError:
        pass   # stderr is merged into the monitored output: a warning here would itself wake the model


def main():
    if SEATS is None:
        # One fixed line: it differs from a working tick's, so the account wakes once and reads why, and it
        # stays identical after that, so a broken deploy is reported rather than read every five minutes.
        print("seats: unavailable — no readable seats.json (DT_SEATS_FILE or ~/.local/state/delvetown-dt/); "
              "run install-monitor.sh")
        return
    if GATE == "wake":
        unread, notes_text, fail = read_town(WAKE_LIMIT)
        items = None if fail and not notes_text else _items(notes_text)
        now = time.time()
        log = _log_entries()
        # Only a double from the last day wakes: an old one is history for `dt.py audit`, and the first
        # tick after a deploy must not wake the account to read the whole log's past.
        doubles = [d for d in double_answers(entries=log) if now - _epoch(d.get("at")) < DOUBLE_WINDOW_S]
        out, st = wake_output(items, _load_state(WAKE_STATE), now,
                              held=held_reason(now) if items else None, failed_at=last_failure_at(),
                              answered=answered_asks(entries=log), doubles=doubles)
        _save_state(WAKE_STATE, st)
        print(out)
        return
    print(digest_output(*read_town(10)))


if __name__ == "__main__":
    main()
