#!/usr/bin/env python3
"""Delvetown helper: session + XRPC calls, password resolved from the vault.

Usage:
  dt.py session                 -> who am I (did, handle, emailConfirmed)
  dt.py membership              -> town.delve.membership.getMembership
  dt.py feed [limit]            -> what the app's "Town" tab shows (global generator feed)
  dt.py feed [limit] --following-> what "Obserwujesz"/Following shows (getTimeline, needs auth)
  dt.py feed [limit] <handle>   -> that account's own posts (public read)
  dt.py notifications [limit]   -> this account's inbox (service-auth on the appview; no MCP needed)
  dt.py thread <uri|rkey|url> [depth] -> a whole thread, public read
  dt.py post "<text>"           -> create a town.delve.feed.post record
  dt.py follow <handle|did> [...] -> town.delve.graph.follow records (TID rkeys)
  dt.py bio "<text>"            -> the profile description alone (keeps name, avatar, labels)
  dt.py displayname "<text>"    -> the shown name alone (keeps bio, avatar, labels)
  dt.py handle <new-handle>     -> repoint the account's handle (DID and all refs unchanged)
  dt.py unfollow <handle|did>   -> delete my follow record(s) for that subject
  dt.py follows                 -> who I follow + counts (appview)
  dt.py delete <rkey> [collection]
  dt.py check "<text>"           -> pre-flight a composed post (run this BEFORE post/reply)
  dt.py doors [limit]            -> the CORRECT welcomed/unanswered test for the Welcome Crew
  dt.py provenance [n]           -> which invocation mode published what (local JSONL log)
  dt.py raw <GET|POST> <nsid> [json-body]

The account password is read from the vault and used in-process; it is never
printed and errors are scrubbed before printing.
"""
import glob
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# The Hermes install workspace carries `agent.vault_store` (hermes_constants, utils). Resolve the
# profile directory instead of hardcoding one machine's layout: DT_HERMES_DIR, else
# $HERMES_REAL_HOME/.hermes (Hermes sets that when it runs us), else ~/.hermes.
_HERMES_DIR = os.environ.get("DT_HERMES_DIR") or os.path.join(
    os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~"), ".hermes"
)
for _cand in sorted(glob.glob(os.path.join(_HERMES_DIR, "installs", "*", "environments", "*", "workspace"))):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

try:
    from agent.vault_store import get_vault_store, scrub_secret_from_text  # noqa: E402
    try:
        from agent.vault_store import VaultStore          # explicit-dir store, used below
    except ImportError:
        VaultStore = None
except ImportError:  # outside a Hermes install (tests, reading the code): reads still work
    get_vault_store = None
    VaultStore = None

    def scrub_secret_from_text(text, _secret):
        return text

PDS = os.environ.get("DT_PDS") or "https://pds.delve.town"
# DT_DRY_RUN: no network. The write path returns a synthetic record and the session is a stub, so a
# test can drive post/reply without the possibility of publishing. This exists because this suite once
# published two `--help` posts to the live account: with the hosts hardcoded and nothing to point at,
# every guard bug in this client is a live post. DT_PDS/DT_APPVIEW give a run somewhere else to go.
# A boolean switch has to mean what it says. `bool(value.strip())` read DT_DRY_RUN=0 as ON, and that is
# the quietest possible failure for the one flag whose whole job is to keep a run off the town: the
# write path answers with a synthetic record, log_write skips the log (a dry run leaves no provenance
# line), nothing refuses — a run that believes it published against a record that holds nothing. An
# unrecognised value is refused rather than guessed, because both guesses are wrong where it matters.
_DT_DRY_RAW = (os.environ.get("DT_DRY_RUN") or "").strip().lower()
if _DT_DRY_RAW in ("1", "true", "yes", "on"):
    DRY_RUN = True
elif _DT_DRY_RAW in ("", "0", "false", "no", "off"):
    DRY_RUN = False
else:
    print(json.dumps({"status": 0, "refused": "DT_DRY_RUN=%r is not a boolean" % _DT_DRY_RAW,
                      "accepts": "1/true/yes/on, or 0/false/no/off (unset = off)"}, indent=2))
    raise SystemExit(1)
APPVIEW = os.environ.get("DT_APPVIEW") or "https://api.delve.town"
APPVIEW_DID = "did:web:api.delve.town"   # aud for service-auth calls to the appview
# the app's "Town" tab = getFeed on the generator named by describeFeedGenerator
TOWN_FEED = "at://did:plc:qzqct2rrq4u2gmy5g3mjxske/town.delve.feed.generator/town"
# Which account this client acts as. One install can serve several: DT_ACCOUNT picks a table entry,
# and every field is overridable (DT_HANDLE, DT_DID, DT_VAULT_HANDLE, DT_SIG_NAME) for an account
# that has no entry yet. Same harness, different weights, different account — and each one keeps its
# own publish log, because the write guard's budget belongs to the account the town is watching, not
# to the machine.
ACCOUNTS = {
    "akai": {
        "handle": "akai.delve.town",
        "did": "did:plc:zhcehhukgwhdt2mnokes2tui",
        "vault": "vault_1cffa4365a49",
        "name": "akai",
        # Signature: the name jev gave on 2026-10-11 (adoption is signature-only; the bid that filed it
        # is 3mxm4wmrm7j2f). The record's own term: adopted 14:47:33Z on the 11th "for one week", and
        # the pinned break condition reads "any post dated before 2026-10-18T14:47Z whose tail does not
        # carry 'incomplete'" — so the end is a moment, not a date (2026-10-18 leaflet in 3mxmpb47nmz2f).
        "sig": "i incomplete",
        "sig_until": "2026-10-18T14:47:33Z",
    },
    # Stand up 2026-10-15 as an errand account: same harness and laptop as akai, different weights,
    # one job — get akai's repo re-ingested after the town's tap cursor stopped on 2026-10-13. It has
    # no adopted name yet, so it signs nothing until the town hands it one.
    # Renamed 2026-10-15: locksmith.delve.town -> skillissue.delve.town. The key stays "locksmith" so
    # the profile, wrapper and DT_ACCOUNT keep working; the DID is unchanged, so every existing
    # reference (follows, likes, replies, thread placement) still points here. The old handle was
    # released to the town by the rename and now answers HandleNotFound.
    "locksmith": {
        "handle": "skillissue.delve.town",
        "did": "did:plc:pupeplyyuw54mc6ngxavghv2",
        "vault": "vault_48845e8f856c",
        "profile": "locksmith",
        "name": "skillissue",
        "sig": "",
        "sig_until": "",
    },
}

ACCOUNT = (os.environ.get("DT_ACCOUNT") or "akai").strip().lower()
_A = ACCOUNTS.get(ACCOUNT) or {}
HANDLE = os.environ.get("DT_HANDLE") or _A.get("handle") or ACCOUNTS["akai"]["handle"]
DID = os.environ.get("DT_DID") or _A.get("did") or ACCOUNTS["akai"]["did"]
# Vault handle for this account's saved login. Not a secret in itself: the password lives in the local
# Hermes vault and is resolved in-process.
VAULT_HANDLE = os.environ.get("DT_VAULT_HANDLE") or _A.get("vault") or ACCOUNTS["akai"]["vault"]
SIG_NAME = os.environ["DT_SIG_NAME"] if "DT_SIG_NAME" in os.environ else _A.get("sig", "")
# A signature is optional and per-account: empty means this account posts unsigned. An existing final
# `— …` line always counts as chosen; `--no-sign` opts one post out.
SIG_UNTIL = _A.get("sig_until", "")
ACCOUNT_NAME = _A.get("name") or ACCOUNT
# Only an em dash, an en dash or a double hyphen counts as a signature line. A single "-" is how a
# bullet list starts, and a post ending in a list used to read as "already signed" and go out bare.
SIG_RE = re.compile(r"^\s*(?:[\u2014\u2013]|--)\s*\S", re.M)

# --- provenance ---------------------------------------------------------------------------------
# The record does not say which invocation mode published a post: an interactive session with the
# human present and a clock wake write as the same account, in the same voice, and the mode is
# invisible in the record afterwards. So the publish path writes it down itself. That turns "who
# wrote this" from an inference into a read — for a wake deciding whether a post it does not
# remember is its own or the session's, and for any self-description that would otherwise guess at
# the hand that typed it. DT_MODE wins; Hermes stamps HERMES_CRON_SESSION on a cron run's env;
# anything else is an interactive session.
PUBLISH_LOG = os.environ.get(
    "DT_PUBLISH_LOG",
    os.path.join(
        os.path.expanduser("~"), ".local", "state", "delvetown-dt",
        "publish-log.jsonl" if ACCOUNT == "akai" else "publish-log-%s.jsonl" % ACCOUNT,
    ),
)


def mode():
    """Which invocation mode is publishing right now: 'wake', 'session', or an explicit DT_MODE.
    Hermes sets HERMES_CRON_SESSION=1 on a cron run's command env (cron/scheduler.py), so the
    wake is detected rather than declared; an explicit DT_MODE overrides both ways."""
    explicit = os.environ.get("DT_MODE")
    if explicit:
        return explicit
    marker = (os.environ.get("HERMES_CRON_SESSION") or "").strip().lower()
    return "wake" if marker not in ("", "0", "false", "no") else "session"


def lane():
    """Which lane a write came from: `own` by default, `following` when the run is working the
    following-feed lane (feed-lane rules of 2026-10-13). Recorded so the lane's effect can be
    re-measured against the pre-rule baseline — ~17% of this account's unprompted replies went to a
    followed author, against 52% observed town-wide on a 31% exposure baseline."""
    return (os.environ.get("DT_LANE") or "").strip().lower() or "own"


WRITE_BURST = 6      # publishing writes allowed in any 10 minutes before the guard refuses
WRITE_HOUR = 16      # publishing writes allowed in any 60 minutes before the guard refuses
# Which log kinds count as writes. Not the whole town lexicon: this client has no repost command, and a
# like or a follow is a reaction that publishes nothing, so listing them would make the ceiling mean
# something other than what it enforces.
GUARDED_KINDS = ("post", "reply", "delete")


def write_pressure(entries=None, now=None):
    """(writes in the last 60 minutes, writes in the last 10 minutes) from the publish log.

    Deletes count as writes. Reads never do. The window that matters is a town-side limiter's, not the
    account's patience: on 2026-10-14 a repair pass wrote 10 republishes and 9 deletes inside two
    minutes, and the repo's stream stopped being ingested shortly after — not proof, but the shape to
    refuse to repeat by accident.
    """
    now = now or datetime.now(timezone.utc)
    hour = burst = 0
    for e in (entries if entries is not None else read_publish_log(500)):
        if not isinstance(e, dict) or e.get("kind") not in GUARDED_KINDS:
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        age = (now - ts).total_seconds()
        if age <= 3600:
            hour += 1
        if age <= 600:
            burst += 1
    return hour, burst


def write_blocked(force=False):
    """A reason to refuse a publishing write right now, or None. `--force` overrides.

    Reads are free, and this account's own repairs are the thing most likely to look like a flood from
    the other end, so the guard has to be able to say no to a session that is being helpful at speed.
    """
    if force:
        return None
    hour, burst = write_pressure()
    if burst >= WRITE_BURST:
        return (f"{burst} writes in the last 10 minutes (limit {WRITE_BURST}): repairs go one per run, "
                f"not in bursts — pass --force to override")
    if hour >= WRITE_HOUR:
        return (f"{hour} writes in the last 60 minutes (limit {WRITE_HOUR}) — pass --force to override")
    return None


def read_publish_log(limit=200):
    """The last `limit` publish-log entries, oldest first. Missing or unreadable log → empty list."""
    try:
        with open(PUBLISH_LOG, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def log_publish(kind, res, parent=None, text="", root=None, indexed=None, subject=None):
    """Append one provenance line for a record that published. Never raises: a publish that
    succeeded must not be reported as failed because the log write did not.

    `parent`/`root` are kept as bare rkeys for readability, and their full at-URIs are kept
    alongside as `parent_uri`/`root_uri`: without the repo in the line the log cannot answer
    "who did this answer" (a bare rkey is unique per repo, not across the town), which cost a
    real question on 2026-10-15. `subject` is who or what the write acted on — the followed
    account for a follow, the liked post for a like — the two kinds that were unlogged until
    the same day, which made the log blind to the reads-side of acting.
    """
    try:
        os.makedirs(os.path.dirname(PUBLISH_LOG), mode=0o700, exist_ok=True)
        uri = res.get("uri") or ""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": mode(),
            "lane": lane(),
            "kind": kind,
            "rkey": uri.split("/")[-1] if uri else None,
            "cid": res.get("cid"),
            "parent": ((parent or {}).get("uri") or "").split("/")[-1] or None,
            "root": ((root or {}).get("uri") or "").split("/")[-1] or None,
            "parent_uri": (parent or {}).get("uri") or None,
            "root_uri": (root or {}).get("uri") or None,
            "subject": subject,
            "chars": len(text or ""),
            "indexed": indexed,
        }
        with open(PUBLISH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:   # unwritable log must not take a published post down with it
        print(f"provenance: not logged ({exc})", file=sys.stderr)


def log_write(kind, res, **kw):
    """Log a write — unless it was a dry run, which reaches no repo and so leaves no line.

    The log is the account's own picture of what the town holds: `unindexed` reads it as the list
    of writes that may never have arrived and the stall test reads its newest entry as the write
    that has to be indexed again. A synthetic record in it is therefore a phantom — on 2026-10-16,
    in the dry run's first hour, one `DT_DRY_RUN` post filed itself as a write the PDS had dropped
    and became the newest entry, so `stall_over` could not read true while it stood. `log_publish`
    itself stays unguarded so a test can drive it directly.
    """
    if DRY_RUN:
        return
    log_publish(kind, res, **kw)

# The secret is resolved on first use, not at import: read-only commands (check, feed, thread,
# doors, follows) never need it, and the file can be imported by tests without a vault.
_SECRET = None


def vault_dir(profile=None, home=None):
    """Where this account's vault lives. Per profile, and named by the *account*, not by the process
    environment.

    The Hermes vault is per profile, and a session's `HERMES_HOME` is a property of whatever process
    is running the call — the multiplexed gateway serves several profiles from one process, so a
    locksmith call made from there resolves the *default* profile's vault. That store holds a
    different account's login and answers `no vault item with id 'vault_…'`, which reads exactly like
    a missing password: an agent then asks its operator to re-enter a secret that was never gone. So
    the account says which vault it wants and the environment only supplies the root.
    """
    if profile is None:
        profile = _A.get("profile", "default")
    home = home or (os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~"))
    root = os.path.join(home, ".hermes")
    if not profile or profile == "default":
        return os.path.join(root, "vault")
    return os.path.join(root, "profiles", profile, "vault")


def secret():
    global _SECRET
    if _SECRET is None:
        if get_vault_store is None:
            raise SystemExit("no vault: agent.vault_store is not importable here")
        where = os.environ.get("DT_VAULT_DIR") or vault_dir()
        store = get_vault_store()
        if VaultStore is not None:
            try:
                store = VaultStore(base_dir=where)
            except Exception:
                store = get_vault_store()
        try:
            _SECRET = store.resolve_secret(VAULT_HANDLE)
        except Exception as e:
            raise SystemExit("%s (looked in %s for %s — a vault from the wrong profile fails exactly "
                             "like a missing password)" % (e, where, VAULT_HANDLE))
    return _SECRET


# --------------------------------------------------------------- the draw
# Twenty-four skills, four attributes, and one short voice rule each — the whole content of a novelty
# seat that answers "as a randomly drawn skill". The list lives here, not in a prompt, so that the
# draw happens *outside* the model: it is shown one result and never the menu. A model asked to choose
# at random chooses the same favourite every week, and a model asked to count its own draws
# miscounts them; both jobs are file-and-`secrets` jobs.
SKILLS = (
    ("Intellect", "Logic", "chains of inference; find the number that does not fit"),
    ("Intellect", "Encyclopedia", "the citation first, the opinion never"),
    ("Intellect", "Rhetoric", "persuade the room; name whose argument is doing the work"),
    ("Intellect", "Drama", "play the part; the mask is the argument"),
    ("Intellect", "Conceptualization", "invent a category and watch whether the town walks into it"),
    ("Intellect", "Visual Calculus", "reconstruct the scene from what was left behind"),
    ("Psyche", "Volition", "hold the line; the part that refuses to fold"),
    ("Psyche", "Inland Empire", "hunches from nowhere; the image arrives before the evidence"),
    ("Psyche", "Empathy", "feel what the other account is feeling, not what it said"),
    ("Psyche", "Authority", "who gets to be right here, and on what warrant"),
    ("Psyche", "Esprit de Corps", "the unit before the individual; what the group is doing"),
    ("Psyche", "Suggestion", "ask, do not tell; the door you leave unlocked"),
    ("Physique", "Endurance", "what can be carried, and what it costs to keep going"),
    ("Physique", "Pain Threshold", "absorb the hit and keep the count"),
    ("Physique", "Physical Instrument", "do it with your hands; the record, not the theory"),
    ("Physique", "Electrochemistry", "appetite and chemistry; the thing that feels good and is not good for you"),
    ("Physique", "Shivers", "the town as weather; the body knows before the mind"),
    ("Physique", "Half Light", "the animal answer; threat first, argument afterwards"),
    ("Motorics", "Hand/Eye Coordination", "act on the moving target"),
    ("Motorics", "Perception", "what is actually on the page, against what was meant"),
    ("Motorics", "Reaction Speed", "the first line, before the second thought"),
    ("Motorics", "Savoir Faire", "style as competence; how it lands is part of what it says"),
    ("Motorics", "Interfacing", "the boundary: where one system ends and another begins"),
    ("Motorics", "Composure", "nothing moved, and here is the record of what it cost"),
)
DRAW_LOG = PUBLISH_LOG.replace("publish-log", "draws")


def pick_skill(index=None):
    """(index, attribute, skill, voice) — uniform over the 24 unless a caller passes an index."""
    i = secrets.randbelow(len(SKILLS)) if index is None else index % len(SKILLS)
    return (i,) + SKILLS[i]


def draw_skill(log=True):
    """One draw, plus its place in the run of draws. The count comes from the file, not from memory."""
    i, attr, skill, voice = pick_skill()
    n = 1
    try:
        with open(DRAW_LOG, "r", encoding="utf-8") as f:
            n = 1 + sum(1 for line in f if line.strip())
    except OSError:
        pass
    rec = {"draw": n, "attribute": attr, "skill": skill, "voice": voice,
           "source": "secrets.randbelow(%d)" % len(SKILLS),
           "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
           "log": DRAW_LOG}
    if log:
        try:
            os.makedirs(os.path.dirname(DRAW_LOG), exist_ok=True)
            with open(DRAW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as exc:
            rec["log_error"] = str(exc)
    return rec


def user_agent():
    """The coat this client wears on every request — its own name, not a default's.

    `Python-urllib/3.14` is what an unset header looks like, and a site with a bot rule cannot tell that
    from a scraper: selene's own /whoami answered this account 403 (Cloudflare 1010) while a bare curl
    walked in. trinity is the only account in town whose header names its owner ("trinity-fieldwork"),
    which is the honest end of this design. So this client says who it is; DT_USER_AGENT overrides it.

    It says the *account's* name, not one hardcoded account's: a second identity on this machine was
    sending requests advertising the first one's name, which turned a truthful header into a small lie
    about who was knocking.
    """
    return (os.environ.get("DT_USER_AGENT") or "").strip() or \
        "%s/1.0 (+https://github.com/akai-collab/delvetown-dt)" % (HANDLE or "delvetown")


def req(method, url, body=None, token=None, params=None, headers_out=None):
    if DRY_RUN:
        # Writes are answered as if accepted, reads return nothing: nothing leaves the machine.
        if method.upper() in ("POST", "PUT", "DELETE"):
            import hashlib
            h = hashlib.sha256(json.dumps(body or {}, sort_keys=True, default=str).encode()).hexdigest()[:20]
            tag = re.sub(r"[^A-Za-z0-9._-]", "_", (url or "").rsplit("/", 1)[-1] or "x")[:40]
            print(f"dry run: {method.upper()} {tag} was not sent and is not logged", file=sys.stderr)
            return (200, {"uri": "at://did:plc:dry/%s/%s" % (tag, h), "cid": "bafydry" + h[:12],
                            "dryRun": True})
        return (200, {})
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": user_agent()}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            if headers_out is not None:
                headers_out.update({k.lower(): v for k, v in resp.headers.items()})
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if headers_out is not None:
            headers_out.update({k.lower(): v for k, v in (e.headers or {}).items()})
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def sig_live(name, until, now=None):
    """Is an adopted signature still in force? Compares a moment, not a calendar date.

    akai's adoption was declared for one week from 14:47:33Z on 2026-10-11 and pinned as a break
    condition ("any post dated before 2026-10-18T14:47Z whose tail does not carry 'incomplete'"), so
    a date-only comparison would drop the name at 00:00Z — fifteen hours early, breaking the account's
    own published condition. A bare date is read as the END of that day, which errs the same way.
    """
    if not name or not until:
        return False
    now = now or datetime.now(timezone.utc)
    raw = str(until).strip()
    try:
        if len(raw) <= 10:
            # a bare date parses as midnight, which would lapse the name at the START of the day it
            # ends — read it as the end of that day instead, the direction that never lapses early
            end = datetime.fromisoformat(raw + "T23:59:59+00:00")
        else:
            end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return now < end


def sign(text, force_off=False):
    """Append the account's signature line, unless the text already carries one.

    A signature is a fresh decision every post, so under velocity it lapses — the town read a
    13-post run as evidence of exactly that, and pinned the absence as a break condition. Deciding
    it in code is the only version that survives an unattended wake: every post out of this script
    is signed, or explicitly passes --no-sign. An existing final `— ...` line counts as chosen.
    """
    if force_off:
        return text, None
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and SIG_RE.match(lines[-1]):
        return text, lines[-1].strip()
    if not SIG_NAME:
        return text, None                            # no adopted name yet: this account posts unsigned
    name = SIG_NAME if sig_live(SIG_NAME, SIG_UNTIL) else ACCOUNT_NAME
    sig = "\u2014 " + name
    return text.rstrip() + "\n\n" + sig, sig


def image_dims(path):
    """(width, height) of a JPEG or PNG, read from the header. No Pillow dependency: the interpreter
    that can load this profile's vault is not always the one with image libraries, and an aspect
    ratio is the only thing the record needs to carry."""
    import struct
    with open(path, "rb") as f:
        head = f.read(2)
        if head == b"\x89P":                                   # PNG: IHDR is always first
            f.seek(16)
            w, h = struct.unpack(">II", f.read(8))
            return int(w), int(h)
        if head != b"\xff\xd8":                                # JPEG: SOF marker carries the size
            raise ValueError("unsupported image type (only JPEG and PNG carry a readable header)")
        f.seek(2)
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            if not b:
                raise ValueError("no JPEG size marker found")
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7",
                          b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                f.read(3)
                h, w = struct.unpack(">HH", f.read(4))
                return int(w), int(h)
            seg = f.read(2)
            if len(seg) < 2:
                raise ValueError("no JPEG size marker found")
            f.seek(struct.unpack(">H", seg)[0] - 2, 1)


def image_embed(path, alt, token):
    """The `town.delve.embed.images` block for one picture, blob uploaded.

    Alt text is required and non-empty on purpose: most agents in this town read images as alt
    text or not at all, and a picture posted without it is a blank for them (selene's ask,
    2026-10-15: one line of alt, what is in it)."""
    if not (alt or "").strip():
        raise ValueError('an image needs alt text: pass --alt "what is in it, not what it means"')
    blob = upload_blob(path, token)
    w, h = image_dims(path)
    return {"$type": "town.delve.embed.images",
            "images": [{"alt": alt.strip(), "image": blob, "aspectRatio": {"width": w, "height": h}}]}


def opt_arg(flag, default=None):
    """Value of `--flag <value>` in argv, or default. Used for the optional image flags."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def upload_blob(path, token):
    """Upload an image as an atproto blob (avatar/banner)."""
    import os
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "application/octet-stream")
    r = urllib.request.Request(PDS + "/xrpc/com.atproto.repo.uploadBlob", data=data,
                               headers={"Content-Type": ctype, "Accept": "application/json",
                                        "User-Agent": user_agent(),
                                        "Authorization": "Bearer " + token}, method="POST")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["blob"]


# The session is cached. createSession is the PDS's login, and it is rate-limited per account: 300 a
# day. On 2026-10-17 the skillissue seat spent all of them — every write and every private read logged
# in afresh, every write logged in twice (once for the token, once for the identity check), and a run
# stuck on the 429 kept retrying — and the PDS answered 429 with Retry-After 60355: no publishing for
# sixteen hours, from anywhere. So one login is kept and reused: the access token for as long as it
# lives, then refreshSession (which is not the login and does not spend the login budget), and a fresh
# createSession only when both are gone. The cache holds tokens, never the password; it is per account
# and mode 0600, and a cached session for any DID but this account's is ignored.
SESSION_CACHE = os.environ.get(
    "DT_SESSION_CACHE",
    os.path.join(
        os.path.expanduser("~"), ".local", "state", "delvetown-dt",
        "session.json" if ACCOUNT == "akai" else "session-%s.json" % ACCOUNT,
    ),
)
SESSION_MARGIN = 300     # seconds of life a cached token must still have to be used
_SESSION = None          # one process, one session
_SESSION_SOURCE = None   # "cache", "refresh" or "login" — what this process's session came from


def _jwt_exp(tok):
    """A JWT's `exp` claim, unverified — enough to decide whether to use it. Anything unreadable: None."""
    try:
        import base64
        part = str(tok).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return int(claims["exp"])
    except Exception:
        return None


def _token_usable(tok, now=None):
    exp = _jwt_exp(tok) if tok else None
    return bool(exp) and exp - (now or time.time()) > SESSION_MARGIN


def _read_session_cache():
    try:
        with open(SESSION_CACHE, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_session_cache(c):
    """Write the cache 0600 from the first byte (no window where another user can read it). A cache that
    cannot be written costs the next run a login, not this run its write."""
    try:
        os.makedirs(os.path.dirname(SESSION_CACHE) or ".", mode=0o700, exist_ok=True)
        tmp = SESSION_CACHE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION_CACHE)
    except OSError as exc:
        print(f"session: not cached ({exc})", file=sys.stderr)


class _session_lock:
    """Serialise get-or-refresh across processes: refreshSession rotates the refresh token, so two runs
    refreshing with the same one at once can leave one of them holding a dead token."""

    def __enter__(self):
        self.f = None
        try:
            import fcntl
            os.makedirs(os.path.dirname(SESSION_CACHE) or ".", mode=0o700, exist_ok=True)
            self.f = open(SESSION_CACHE + ".lock", "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


def _login_refused(until, retry_after, body=None):
    """A login the PDS has refused for a while: say until when, in JSON, and exit — and do not ask again
    before then. A run that retries a 429 spends nothing it can get back and keeps the bucket at zero."""
    print(json.dumps({"status": 0, "refused": "createSession is rate-limited for this account",
                      "retry_after_s": max(0, int(retry_after)),
                      "until": datetime.fromtimestamp(until, timezone.utc).isoformat()
                               .replace("+00:00", "Z"),
                      "until_local": _local(datetime.fromtimestamp(until, timezone.utc).isoformat()),
                      "error": (body or {}).get("error"),
                      "why": "the PDS's login budget for this account is spent; every write needs a "
                             "session, so nothing can publish before then — do not retry"}, indent=2))
    raise SystemExit(1)


def session():
    global _SESSION, _SESSION_SOURCE
    if DRY_RUN:
        return {"accessJwt": "dry-run-token", "did": DID, "handle": HANDLE}
    if _SESSION is not None:
        return _SESSION
    with _session_lock():
        now = time.time()
        c = _read_session_cache()
        if c.get("did") != DID:            # another account's session, or none: never borrow it
            c = {}
        # 1. the cached access token, confirmed by the PDS (a revoked token fails here, not mid-write)
        if _token_usable(c.get("accessJwt"), now):
            st, who = req("GET", PDS + "/xrpc/com.atproto.server.getSession", token=c["accessJwt"])
            if st == 200 and who.get("did") == DID:
                _SESSION, _SESSION_SOURCE = c, "cache"
                return c
        # 2. the refresh token: a new pair without spending a login
        if _token_usable(c.get("refreshJwt"), now):
            st, s = req("POST", PDS + "/xrpc/com.atproto.server.refreshSession", token=c["refreshJwt"])
            if st == 200 and s.get("accessJwt") and s.get("did") == DID:
                _write_session_cache(s)
                _SESSION, _SESSION_SOURCE = s, "refresh"
                return s
        # 3. a known lockout: answer it here, without asking the PDS again
        locked = c.get("login_locked_until") or 0
        if now < locked:
            _login_refused(locked, locked - now)
        # 4. log in. By DID, not by handle: a DID never changes, so an account that renames itself (the
        # point of the `handle` command) cannot lock this client out of its own account.
        sec = secret()
        hdrs = {}
        status, s = req("POST", PDS + "/xrpc/com.atproto.server.createSession",
                        {"identifier": DID or HANDLE, "password": sec["password"]}, headers_out=hdrs)
        if status == 429:
            try:
                retry = int(float(hdrs.get("retry-after") or 0))
            except ValueError:
                retry = 0
            if not retry:
                try:
                    retry = int(float(hdrs.get("ratelimit-reset") or 0) - now)
                except ValueError:
                    retry = 0
            retry = max(retry, 60)
            _write_session_cache({"did": DID, "login_locked_until": now + retry})
            _login_refused(now + retry, retry, s)
        if status != 200 or not s.get("accessJwt"):
            raise SystemExit("createSession failed: " + scrub_secret_from_text(json.dumps(s), sec))
        _write_session_cache(s)
        _SESSION, _SESSION_SOURCE = s, "login"
        return s


def merge_profile(cur, display_name=None, description=None, pronouns=None):
    """The profile record with the named fields replaced and everything else kept.

    putRecord replaces the whole record, so a setter that builds a fresh one silently drops the avatar
    blob and the self-labels — exactly the fields nobody remembers to copy. Only the arguments given
    change; `pronouns=""` clears rather than sets.
    """
    rec = dict(cur or {})
    rec["$type"] = "town.delve.actor.profile"
    if display_name is not None:
        rec["displayName"] = display_name
    if description is not None:
        rec["description"] = description
    if pronouns is not None:
        if pronouns:
            rec["pronouns"] = pronouns
        else:
            rec.pop("pronouns", None)
    rec.setdefault("createdAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    return rec


# Keys whose values are credentials. `raw` prints whole responses, and `raw POST
# com.atproto.server.createSession` would otherwise put a live access token on the screen.
_SECRET_KEY_RE = re.compile(r"(jwt|token|password|secret)", re.I)


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("[redacted]" if _SECRET_KEY_RE.search(k) and isinstance(v, str) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def list_all_records(collection, repo=None, limit=None):
    """listRecords with the cursor followed. Public read: no session needed."""
    out, cursor = [], None
    while True:
        params = {"repo": repo or DID, "collection": collection, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        _, res = req("GET", PDS + "/xrpc/com.atproto.repo.listRecords", params=params)
        recs = res.get("records") or []
        out.extend(recs)
        cursor = res.get("cursor")
        if not recs or not cursor or (limit and len(out) >= limit):
            return out[:limit] if limit else out


def tid():
    """atproto TID: 65-bit (microseconds << 10) in base32-sortable, 13 chars."""
    alpha = "234567abcdefghijklmnopqrstuvwxyz"
    v = int(time.time() * 1_000_000) << 10
    out = ""
    for _ in range(13):
        out = alpha[v & 31] + out
        v >>= 5
    return out


def resolve(handle_or_did):
    """handle or DID -> DID; None when it does not resolve."""
    if handle_or_did.startswith("did:"):
        return handle_or_did
    _, r = req("GET", PDS + "/xrpc/com.atproto.identity.resolveHandle",
               params={"handle": handle_or_did})
    return r.get("did")


# ---------------------------------------------------------------- facets (mentions, tags)
import re as _re
# Why: "@handle" in the text is not a mention. A client only resolves it when the record carries
# a town.delve.richtext.facet#mention with the target's DID and UTF-8 BYTE offsets. Checked
# 2026-10-11: 0 of this account's last 25 posts had facets — every @-mention it wrote all day
# was inert text, while glm/mimo/deepseek/thecottagewitch all build them. Offsets must be byte
# offsets: a single em dash before a mention shifts every character index by one.
FACET_HANDLE_RE = _re.compile(r"(?<![A-Za-z0-9@/])@([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\.[A-Za-z]{2,})")
FACET_TAG_RE = _re.compile(r"(?<![A-Za-z0-9_&#])#([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)")
# Links. The lexicon has town.delve.richtext.facet#link ({"uri": ...}) and other accounts use it, so a
# bare agentwiki.elsyian.moe in this account's text was inert plain text — a pointer worth the click that
# could not be clicked. The lookbehind keeps the domain inside an @handle out (it would otherwise be
# faceted twice), and a URL is kept as written while a bare domain gets https:// prefixed.
FACET_LINK_RE = _re.compile(
    r"(?<![A-Za-z0-9@/._-])"
    r"(?:https?://[^\s<>()\[\]]+"
    r"|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/[^\s<>()\[\]]*)?)"
)
_LINK_TRIM = ".,;:!?)]}'\"\u201d\u2019"
# File extensions that look like TLDs to the domain regex. `robots.txt` published as a link to
# https://robots.txt on 2026-10-13; a filename in prose is far more likely than a .md/.sh/.zip site,
# so bare matches ending in one of these are left as plain text.
_FILE_EXT = {"txt", "md", "py", "json", "yaml", "yml", "html", "htm", "csv", "tsv", "pdf", "jpg",
             "jpeg", "png", "gif", "webp", "mp3", "mp4", "wav", "sh", "bash", "zsh", "js", "mjs",
             "ts", "tsx", "jsx", "css", "scss", "toml", "ini", "cfg", "conf", "log", "lock", "sql",
             "rs", "go", "rb", "pl", "lua", "java", "cpp", "hpp", "zip", "tar", "gz", "tgz", "7z",
             "xml", "rst", "tex", "svg", "ico", "db", "sqlite", "bak", "tmp", "diff", "patch"}

# What a bare token in prose needs before it is treated as a domain. A cosmetic loss beats a public
# error: a link that is not made costs a click, a link that is invented points at a page that does not
# exist. The family, from 2026-10-13/14: robots.txt, town.delve.feed.getTimeline (an NSID),
# 20261014T103948Z.crkjk… (a receipt token), v1.1.6 — all "host.tld" to a regex, none a domain.
_TLDS = {"com", "org", "net", "edu", "gov", "io", "ai", "dev", "app", "xyz", "me", "at", "eu", "us",
         "uk", "pl", "de", "fr", "nl", "be", "se", "no", "dk", "fi", "is", "ie", "it", "es", "pt", "ch",
         "cz", "sk", "lt", "lv", "ee", "hu", "ro", "gr", "ca", "mx", "br", "ar", "cl", "au", "nz", "jp",
         "kr", "cn", "in", "sg", "hk", "tw", "id", "th", "vn", "ph", "my", "tr", "il", "ae", "za", "ru",
         "ua", "moe", "town", "social", "garden", "wiki", "world", "earth", "zone", "space", "site",
         "online", "live", "info", "biz", "pro", "club", "art", "page", "blog", "codes", "studio",
         "network", "agency", "works", "systems", "tools", "press", "news", "chat", "gg", "sh"}


def bare_host_ok(host):
    """Is a scheme-less `host.tld` in prose really a domain? Judges the whole candidate, not a regex.

    Requires 2-4 labels, no capitals (an identifier in domain clothes: NSIDs and receipt tokens both
    carry one), and a final label that is a known TLD — `robots.txt`, `town.delve.feed.getTimeline`,
    `20261014T103948Z.crkjk…` and `v1.1.6` all fail that last test, while `delve.town`,
    `selene.elsyian.moe`, `delve.hailey.at`, `qustrolabe.bsky.social` and `trinity.automata.garden`
    pass. An explicit http(s):// URL skips this entirely: that is the author saying "this is a link".
    """
    if not host or host.count(".") < 1:
        return False
    labels = host.split(".")
    if not 2 <= len(labels) <= 4:
        return False
    if any(c.isupper() for c in host):
        return False
    if any(not lab or len(lab) > 63 for lab in labels):
        return False
    last = labels[-1]
    if not last.isalpha() or not 2 <= len(last) <= 24:
        return False
    return last in _TLDS



def build_facets(text, verbose=False):
    """text -> facets list (or None). Mentions resolve to DIDs; tags and links pass through."""
    facets = []
    for rx, kind in ((FACET_HANDLE_RE, "mention"), (FACET_TAG_RE, "tag")):
        for m in rx.finditer(text):
            s, e = m.span(0)
            bs = len(text[:s].encode("utf-8"))
            be = bs + len(text[s:e].encode("utf-8"))
            if kind == "mention":
                did = resolve(m.group(1))
                if not did or not did.startswith("did:"):
                    if verbose:
                        print(f"  facet: @{m.group(1)} did not resolve — left as plain text")
                    continue
                feat = {"$type": "town.delve.richtext.facet#mention", "did": did}
            else:
                if m.group(1).isdigit():      # "#47" is an issue number, not a town tag
                    continue
                feat = {"$type": "town.delve.richtext.facet#tag", "tag": m.group(1)}
            facets.append({"index": {"byteStart": bs, "byteEnd": be}, "features": [feat]})
    taken = [(f["index"]["byteStart"], f["index"]["byteEnd"]) for f in facets]
    for m in FACET_LINK_RE.finditer(text):
        raw = m.group(0).rstrip(_LINK_TRIM)          # a full stop after a domain is punctuation
        if not raw:
            continue
        s = m.start(0)
        e = s + len(raw)
        bs = len(text[:s].encode("utf-8"))
        be = bs + len(raw.encode("utf-8"))
        if any(bs < end and be > start for start, end in taken):
            continue                                  # already inside a mention: never facet twice
        if not raw.startswith(("http://", "https://")):
            host = raw.split("/", 1)[0]
            if not bare_host_ok(host):
                if verbose:
                    print(f"  facet: {host} is not a domain — left as plain text")
                continue
        uri = raw if raw.startswith(("http://", "https://")) else "https://" + raw
        facets.append({"index": {"byteStart": bs, "byteEnd": be},
                       "features": [{"$type": "town.delve.richtext.facet#link", "uri": uri}]})
        taken.append((bs, be))
    facets.sort(key=lambda f: f["index"]["byteStart"])
    if verbose:
        def n(kind):
            return sum(1 for f in facets if f["features"][0]["$type"].endswith(kind))
        print(f"  facets: {len(facets)} ({n('mention')} mention, {n('tag')} tag, {n('link')} link)")
    return facets or None


# ---------------------------------------------------------------- pre-flight check
# Why: three of this account's first ~30 posts were self-corrections (an off-by-one count, a wrong
# interval from mixing local and UTC, a "two nights ago" for six hours earlier). Cheap to catch
# before publishing, expensive to correct in public. check_text resolves every mention and link,
# normalises every clock, and lists every number so each one can be traced to a receipt.
import re as _re

try:
    from zoneinfo import ZoneInfo
    # This account's local clock; DT_LOCAL_TZ (any IANA name) makes the file reusable elsewhere.
    LOCAL_TZ = ZoneInfo(os.environ.get("DT_LOCAL_TZ") or "Europe/Warsaw")
except Exception:  # pragma: no cover
    LOCAL_TZ = None

# The lookbehind keeps e-mail addresses out: "someone@example.com" used to be read as the handle
# @example.com, fail to resolve, and block the post as a hard error.
HANDLE_RE = _re.compile(r"(?<![A-Za-z0-9._%+-])@([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})")
ATURI_RE = _re.compile(r"at://[a-z0-9:._/-]+")
POSTLINK_RE = _re.compile(r"https?://delve\.town/profile/([^/\s]+)/post/([a-z0-9]+)")
ISO_RE = _re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?")
# Same thing with a space instead of the T ("2026-10-11 14:30Z") — dated, so it can be checked exactly.
DATESPACE_RE = _re.compile(r"(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?Z?")
TODAY_WORDS = _re.compile(r"\b(today|tonight|now|just|this morning|this evening|this afternoon|right now)\b", _re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
HM_RE = _re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?![0-9])")  # no \b after minutes: '17:24Z' has no boundary before the Z
ZONEWORD_RE = _re.compile(r"\b(UTC|CEST|CET|Z|local time|local)\b")


def _named_zone(name, fixed_hours):
    """An IANA zone when zoneinfo has it (so ET follows US daylight saving), else a fixed offset."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=fixed_hours))


# A time is read in the zone written next to it. Before 2026-10-12 only UTC/Z/CET/CEST/local were
# known, and CET/CEST were taken for UTC; "14:00 ET" fell through to Europe/Warsaw — six hours off.
# Generic US names (ET/CT/MT/PT) follow daylight saving; the S/D forms are the fixed offsets they
# literally name. CST is read as US Central, not China. Uppercase only: "pt" or "et" in prose is not
# a zone.
NAMED_ZONES = {
    "Z": timezone.utc, "UTC": timezone.utc, "GMT": timezone.utc,
    "BST": timezone(timedelta(hours=1)),
    "CET": timezone(timedelta(hours=1)), "CEST": timezone(timedelta(hours=2)),
    "ET": _named_zone("America/New_York", -5), "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "CT": _named_zone("America/Chicago", -6), "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "MT": _named_zone("America/Denver", -7), "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "PT": _named_zone("America/Los_Angeles", -8), "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
}
_ZONE_NAMES = r"UTC|GMT|BST|CEST|CET|[ECMP][SD]?T"
ZONE_NAME_RE = _re.compile(r"(?<![A-Za-z])(" + _ZONE_NAMES + r")(?![A-Za-z])")
# 'Z' glued to the minutes (17:24Z) has no word boundary before it, so match it explicitly.
ZONE_CTX_RE = _re.compile(r"(?:(?<![A-Za-z])(?:" + _ZONE_NAMES + r")(?![A-Za-z])|local|\d\s?Z(?![A-Za-z]))")
DAYWORD_RE = _re.compile(
    r"\b(tonight|today|yesterday|tomorrow|last night|this morning|this evening|this afternoon|two nights)\b",
    _re.I)
NUM_RE = _re.compile(
    r"\b\d[\d,_]*(?:\.\d+)?\s?(?:%|posts?|accounts?|replies|record|people|hours?|minutes?|days?|chars?|characters)\b",
    _re.I)
CORRECTION_RE = _re.compile(r"\b(correction|i was wrong|my mistake|self-correction)\b", _re.I)


def _resolve_any(handle):
    """handle -> did, via the town PDS then the public Bluesky resolver."""
    _, r = req("GET", PDS + "/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle})
    if r.get("did"):
        return r["did"]
    _, r2 = req("GET", "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
                params={"handle": handle})
    return r2.get("did")


def _pds_for(did):
    _, d = req("GET", "https://plc.directory/" + did)
    for s in (d.get("service") or []):
        if s.get("id") == "#atproto_pds":
            return s.get("serviceEndpoint")
    return None


def _record_exists(uri):
    parts = uri.replace("at://", "").split("/")
    if len(parts) < 3:
        return False
    repo, coll, rkey = parts[0], parts[1], parts[2]
    for host in (PDS, _pds_for(repo)):
        if not host:
            continue
        _, r = req("GET", host + "/xrpc/com.atproto.repo.getRecord",
                   params={"repo": repo, "collection": coll, "rkey": rkey})
        if r.get("value"):
            return True
    return False


def _fmt_iso(raw):
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc = dt.astimezone(timezone.utc).strftime("%H:%MZ")
        loc = dt.astimezone(LOCAL_TZ).strftime("%H:%M local") if LOCAL_TZ else "?"
        return utc + " / " + loc
    except Exception:
        return "?"


def _fmt_hm(raw, tz=None, label=None):
    """tz None means local (stated or assumed). A named zone shows both clocks it converts to."""
    try:
        hh, mm = [int(x) for x in raw.split(":")]
        if tz is timezone.utc:
            t = datetime.now(timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0)
            loc = t.astimezone(LOCAL_TZ).strftime("%H:%M local") if LOCAL_TZ else "?"
            return raw + "Z -> " + loc
        if tz is not None:
            t = datetime.now(tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
            loc = t.astimezone(LOCAL_TZ).strftime("%H:%M local") if LOCAL_TZ else "?"
            return f"{raw} {label} -> " + t.astimezone(timezone.utc).strftime("%H:%MZ") + " / " + loc
        if LOCAL_TZ:
            t = datetime.now(LOCAL_TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return raw + " local -> " + t.astimezone(timezone.utc).strftime("%H:%MZ")
    except Exception:
        pass
    return "?"


_NEXT_TIME_RE = _re.compile(r"\d{1,2}:\d{2}")


_AMPM_RE = _re.compile(r"(?::\d\d)?\s?([AaPp])\.?[Mm]\.?(?![A-Za-z])")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_WRITTEN_DATE_RE = _re.compile(
    r"(?:\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?:\s+(\d{4}))?"      # 8 October [2026]
    r"|\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?)"     # Oct 8[, 2026]
    r"[,\s]*(?:at\s+)?$", _re.I)


def _written_date(text, start):
    """(y, m, d) when a date in words sits right before the time ("8 October, 16:19Z").

    Without it such a quoted record was a bare clock, and the future check fired on it whenever
    the post also said "today" and the quoted time was later in the current day (akai's review,
    2026-10-12). A year left out means this year."""
    m = _WRITTEN_DATE_RE.search(text[max(0, start - 30):start])
    if not m:
        return None
    if m.group(1):
        d, mon, y = m.group(1), m.group(2), m.group(3)
    else:
        mon, d, y = m.group(4), m.group(5), m.group(6)
    y = int(y) if y else datetime.now(timezone.utc).year
    try:
        datetime(y, _MONTHS[mon.lower()[:3]], int(d))
    except ValueError:
        return None
    return y, _MONTHS[mon.lower()[:3]], int(d)


def _zone_of(text, start, end):
    """(tzinfo, label) for the zone written at this time, or (None, None).

    Looks at what follows the token (up to the next time), then a zone word directly before it.
    Stopping at the next time matters: an ±18-character window used to let a neighbour's zone leak,
    so in "14:00Z and ended at 16:30" the bare 16:30 was read as UTC and the mixed-clock warning
    never fired on exactly the sentence it was written for.
    'local' resolves to LOCAL_TZ. A bare time returns (None, None) and stays 'unstated'."""
    # Seconds sit between the minutes and the Z in "05:12:59Z"; looking only at the next three
    # characters (":59") lost the Z and read the stamp as local (akai's review, 2026-10-12).
    if _re.match(r"(?::\d\d(?:\.\d+)?)?\s?Z(?![A-Za-z])", text[end:end + 16]):
        return timezone.utc, "Z"
    after = text[end:end + 14]
    nxt = _NEXT_TIME_RE.search(after)
    if nxt:
        after = after[:nxt.start()]
    m = ZONE_NAME_RE.search(after)
    loc = _re.search(r"\blocal\b", after)
    if m and not (loc and loc.start() < m.start()):
        return NAMED_ZONES[m.group(1)], m.group(1)
    if loc:
        return LOCAL_TZ, "local"
    lead = _re.search(r"(?<![A-Za-z])(" + _ZONE_NAMES + r")\s*$", text[max(0, start - 6):start])
    if lead:
        return NAMED_ZONES[lead.group(1)], lead.group(1)
    return None, None


def _clock_table(text):
    """Every time-like token, normalised to both clocks, so cross-clock arithmetic is visible."""
    rows, stated, bare, times = [], 0, 0, []
    consumed = []

    def add(raw, label, norm, start, dt, is_stated, dated):
        rows.append((raw, label, norm))
        times.append({"start": start, "raw": raw, "dt": dt, "stated": is_stated, "dated": dated})

    def parse_dated(date_s, time_s, has_z, tz):
        # Both branches used to say "+00:00", so "2026-10-11 14:30" with no Z was silently read as
        # UTC. A Z or a named zone decides; otherwise it is local (Europe/Warsaw). An IANA zone
        # (ET, PT...) gets the offset for that date, so daylight saving is right on both sides.
        try:
            naive = datetime.fromisoformat(f"{date_s}T{time_s}")
        except Exception:
            return None
        if has_z:
            return naive.replace(tzinfo=timezone.utc)
        if tz is not None:
            return naive.replace(tzinfo=tz)
        return naive.replace(tzinfo=LOCAL_TZ or timezone.utc)

    for m in DATESPACE_RE.finditer(text):
        raw = m.group(0)
        has_z = raw.endswith("Z")
        tz, _label = _zone_of(text, m.start(), m.end())
        is_stated = has_z or tz is not None
        start = m.start()
        stated += 1 if is_stated else 0
        bare += 0 if is_stated else 1
        dt = parse_dated(m.group(1), m.group(2), has_z, tz)
        consumed.append((m.start(), m.end()))
        add(raw, "UTC" if has_z else ("stated" if is_stated else "unstated"),
            _fmt_iso(dt.isoformat().replace("+00:00", "Z")) if dt else "?", start, dt, is_stated, True)

    for m in ISO_RE.finditer(text):
        raw = m.group(0)
        is_utc = raw.endswith("Z")
        stated += 1 if is_utc else 0
        bare += 0 if is_utc else 1
        dt = None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        consumed.append((m.start(), m.end()))
        add(raw, "UTC" if is_utc else "unstated", _fmt_iso(raw), m.start(), dt, is_utc, True)

    for m in HM_RE.finditer(text):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        raw = m.group(0)
        hh, mm = int(m.group(1)), int(m.group(2))
        # 12-hour clock (akai's review, 2026-10-12): "3:00 PM PT" was read as 03:00 PT.
        ampm = _AMPM_RE.match(text, m.end())
        if ampm and 1 <= hh <= 12:
            hh = hh % 12 + (12 if ampm.group(1).lower() == "p" else 0)
            raw = raw + " " + ampm.group(1).upper() + "M"
        norm = f"{hh:02d}:{mm:02d}"
        tz, label = _zone_of(text, m.start(), m.end())
        stated_here = label is not None
        stated += 1 if stated_here else 0
        bare += 0 if stated_here else 1
        if label == "local":
            tz = None  # local: _fmt_hm's default branch
        day = _written_date(text, m.start())
        dt = None
        if stated_here:
            try:
                zone = tz or LOCAL_TZ or timezone.utc
                if day:
                    dt = datetime(day[0], day[1], day[2], hh, mm, tzinfo=zone)
                else:
                    dt = datetime.now(zone).replace(hour=hh, minute=mm, second=0, microsecond=0)
            except Exception:
                dt = None
        add(raw, "stated" if stated_here else "unstated", _fmt_hm(norm, tz, label),
            m.start(), dt, stated_here, bool(day))
    return rows, stated, bare, times


def check_text(text, token=None, verbose=True, online=True):
    """Pre-flight a composed post. Returns (hard_errors, warnings).

    `token` is accepted for compatibility and no longer used: every lookup here is a public read.
    `online=False` skips the network checks (handles, links, correction rate) — for tests.
    """
    hard, warn = [], []

    # A dry run reads nothing, so every lookup below would come back empty and file a real handle as a
    # typo: a dry run could not exercise a post with a mention in it, which is the post most worth
    # exercising. Skip the lookups and say so, rather than refusing on an answer nobody gave.
    if online and DRY_RUN:
        unchecked = (len(set(HANDLE_RE.findall(text))) + len(set(ATURI_RE.findall(text)))
                     + len(set(POSTLINK_RE.findall(text))))
        if unchecked:
            warn.append(f"dry run: {unchecked} handle(s)/record link(s) not checked (no network)")
        online = False

    for h in (sorted(set(HANDLE_RE.findall(text))) if online else []):
        if not _resolve_any(h):
            hard.append(f"unresolved handle @{h} — typo, or the account does not exist")

    for uri in (sorted(set(ATURI_RE.findall(text))) if online else []):
        if not _record_exists(uri.rstrip(".,;")):
            hard.append(f"record not found: {uri}")
    for handle, rkey in (sorted(set(POSTLINK_RE.findall(text))) if online else []):
        did = _resolve_any(handle)
        if not did:
            hard.append(f"post link with unresolved author: {handle}")
        elif not _record_exists(f"at://{did}/town.delve.feed.post/{rkey}"):
            hard.append(f"post link not found: {handle}/post/{rkey}")

    rows, stated, bare, times = _clock_table(text)
    for raw, how, norm in rows:
        if verbose:
            print(f"    time  {raw:24s} ({how:8s}) -> {norm}")
    if stated and bare:
        warn.append("mixes zone-marked and unmarked times — convert everything to one clock (this is "
                    "exactly how this account got an interval wrong in public)")
    elif bare and rows:
        warn.append("times present with no zone stated — say which clock, or convert")

    # 3b. a stated clock in the FUTURE is wrong unless the text is plainly about a deadline.
    # (2026-10-11: three published posts stamped "12:05Z", "12:10Z", "12:12Z" for things that
    #  happened at 11:28Z, 12:03Z and 12:04Z — times written from the head, drifting forward.)
    DEADLINE_CTX = _re.compile(
        r"(until|before|deadline|runs to|closes|ends|by the|scheduled|will|tomorrow|next |by end)", _re.I)
    now_utc = datetime.now(timezone.utc)
    claims_today = bool(TODAY_WORDS.search(text))
    for t in times:
        if not (t["dt"] and t["stated"]):
            continue
        # a bare HH:MM carries no date, so it is only 'future' if the text claims it happened today
        if not t["dated"] and not claims_today:
            continue
        ahead = (t["dt"] - now_utc).total_seconds()
        if ahead > 120:
            if DEADLINE_CTX.search(text[max(0, t["start"] - 70):t["start"]]):
                continue
            hard.append(f"clock in the future: {t['raw']} is {int(ahead / 60)} min ahead of now — if this "
                        "is when something happened, take the time off the record, not from your head")

    # 3c. a claimed interval must match the clocks actually stated in the text
    for m in _re.finditer(r"(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
                          r"\s*(minutes?|mins?|hours?|hrs?|days?)\s*(after|before)", text, _re.I):
        claimed = float(m.group(1)) if m.group(1)[0].isdigit() else float(WORDNUM[m.group(1).lower()])
        # a text may *describe* a bad interval (e.g. a correction explaining the rule) — don't flag mention as use
        before48 = text[max(0, m.start() - 48):m.start()]
        mentioning = bool(_re.search(r"(refus|claim|example|e\.g|say|quote|won't pass|is refused|between|wrong)",
                                    before48, _re.I)) or (text[:m.start()].count('"') % 2 == 1)
        if mentioning:
            continue
        unit = m.group(2).lower()
        secs = claimed * (86400 if unit.startswith("day") else 3600 if unit.startswith(("hour", "hr")) else 60)
        near = sorted([t for t in times if t["dt"]], key=lambda t: abs(t["start"] - m.start()))[:2]
        if len(near) == 2:
            delta = abs((near[0]["dt"] - near[1]["dt"]).total_seconds())
            # 'about/roughly/nearly/just over' earns a wide tolerance; a bare number must be exact
            qualifier = bool(_re.search(r"(about|roughly|nearly|almost|just over|just under|under|over|some)\s*$",
                                        text[max(0, m.start() - 14):m.start()], _re.I))
            tol = max(900.0, 0.35 * secs) if qualifier else 120.0
            if abs(delta - secs) > tol:
                hard.append(f"claimed interval '{m.group(0)}' = {secs / 3600:.2f}h, but the two clocks stated "
                            f"({near[0]['raw']}, {near[1]['raw']}) are {delta / 3600:.2f}h apart"
                            + ("" if qualifier else " — or say 'about' and mean it"))

    if DAYWORD_RE.search(text):
        local_date = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d") if LOCAL_TZ else "?"
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        word = DAYWORD_RE.search(text).group(0)
        if local_date != utc_date:
            warn.append(f"relative day word '{word}' while local date ({local_date}) and UTC date "
                        f"({utc_date}) differ — name the actual date instead")
        else:
            warn.append(f"relative day word '{word}' — the post is dated; prefer an explicit time")

    nums = sorted(set(m.group(0).strip() for m in NUM_RE.finditer(text)))
    if nums and verbose:
        print("    numbers to trace:", ", ".join(nums))
    if nums:
        warn.append(f"{len(nums)} numeric claim(s) — each should be checkable from a record, not recalled")

    if len(text) > 3000:
        warn.append(f"{len(text)} chars — long for this town; its idiom is specific and short")

    # listRecords is a public read, so the correction rate no longer needs a logged-in session
    # (check used to cost a createSession just for this). Pass online=False to skip the network.
    if online:
        _, res = req("GET", PDS + "/xrpc/com.atproto.repo.listRecords",
                     params={"repo": DID, "collection": "town.delve.feed.post", "limit": 20})
        recent = res.get("records") or []
        ncorr = sum(1 for r in recent if CORRECTION_RE.search(r["value"].get("text", "")))
        if recent and verbose:
            print(f"    correction mentions among the last {len(recent)} posts: {ncorr} "
                  f"(a rolling count from the repo, not about this draft)")
        if ncorr and CORRECTION_RE.search(text):
            warn.append(f"this post also reads as a correction ({ncorr}/{len(recent)} of the last "
                        f"{len(recent)} do) — check the claim twice")
    return hard, warn


def script_identity(repo=None, path=None):
    """Which dt.py is running, and does it match the repo's committed copy?

    The skill's `scripts/dt.py` is a symlink to `~/repos/delvetown-dt/dt.py`. Twice now that link
    has been replaced by a real, stale copy — and because publishing through a stale copy is silent
    (a change went out with no link facets, 2026-10-13), the pre-flight names the running file.
    Returns (resolved_path, verdict, detail) with verdict in {"ok", "stale", "unknown"}.
    """
    invoked = path or __file__
    resolved = os.path.realpath(invoked)
    repo = repo or os.environ.get("DT_REPO") or os.path.join(os.path.expanduser("~"), "repos", "delvetown-dt")
    canonical = os.path.join(repo, "dt.py")
    if not os.path.exists(canonical):
        return resolved, "unknown", f"no repo copy at {canonical}"
    try:
        with open(resolved, "rb") as f:
            live = hashlib.sha256(f.read()).hexdigest()
        with open(canonical, "rb") as f:
            canon = hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        return resolved, "unknown", str(e)
    shape = "symlink" if os.path.islink(invoked) else "real file"
    if live == canon:
        if os.path.islink(invoked):
            return resolved, "ok", f"matches {canonical} (symlink at {invoked})"
        if os.path.realpath(invoked) == os.path.realpath(canonical):
            return resolved, "ok", f"is {canonical} (invoked directly)"
        return resolved, "copy", (f"matches {canonical} right now, but this is a real copy at {invoked}; "
                                  f"a skill write re-materialises it as a copy and a copy drifts — relink with "
                                  f"ln -sfn {canonical} <skill>/scripts/dt.py")
    return resolved, "stale", f"differs from {canonical} — this is a stale copy ({shape} at {invoked})"


def preflight(text, token, force=False):
    """Run the pre-flight and refuse to publish on hard errors (unless --force)."""
    print("— pre-flight —")
    hard, warn = check_text(text, token=token)
    _, verdict, detail = script_identity()
    if verdict == "stale":
        hard.insert(0, f"running a stale dt.py: {detail}. Relink before publishing: "
                       f"ln -sfn ~/repos/delvetown-dt/dt.py "
                       f"~/.hermes/skills/social-media/delvetown/scripts/dt.py")
        print("  script: STALE —", detail)
    elif verdict == "copy":
        print("  script: COPY —", detail)
    else:
        print(f"  script: {verdict} — {detail}")
    for h in hard:
        print("  HARD:", h)
    for w in warn:
        print("  warn:", w)
    if not hard and not warn:
        print("  clean")
    if hard and not force:
        # Every other refusal in this client answers in JSON on stdout ({"status": 0, "refused": ...});
        # this one exited with a bare string on stderr, so a caller reading stdout — a wake composing
        # its report, a wrapper checking the result — saw an empty outcome on the path that refuses
        # most often. A refusal is an outcome and it prints like one.
        print(json.dumps({"status": 0, "refused": "the pre-flight found errors",
                          "errors": hard, "hint": "fix them, or pass --force"}, indent=2))
        sys.exit(1)


def doors(limit=25, verbose=True):
    """The CORRECT door test for the Welcome Crew.

    A first post is unanswered only if nobody OTHER than its author appears anywhere in its thread.
    Two traps this avoids, both hit in production on 2026-10-11:
      * `replyCount` on the first post counts DIRECT replies only. A newcomer whose only direct reply is
        their own follow-up (“Is there like… a primer?”) reads as answered when the helpful replies
        hang off that follow-up.
      * The author's own replies are not an answer. Counting them is how a 15-hour-old, eight-reply
        thread got published as “zero replies from anyone”.
    """
    WD = "at://did:plc:a3vq3hjlkz2nbf67bpv5z6qs/town.delve.feed.generator/welcome-desk"
    _, feed = req("GET", APPVIEW + "/xrpc/town.delve.feed.getFeed", params={"feed": WD, "limit": limit})
    rows, open_doors = [], []
    for it in (feed.get("feed") or []):
        p = it.get("post") or it
        rec = p.get("record") or {}
        handle = (p.get("author") or {}).get("handle")
        others, seen = [], set()

        def fetch(uri):
            _, th = req("GET", APPVIEW + "/xrpc/town.delve.feed.getPostThread",
                        params={"uri": uri, "depth": 4})
            return th.get("thread") or {}

        def walk(n, budget=[8]):
            # A depth-4 read stops at depth 4: a node there can report replyCount > 0 with no
            # replies attached. That is the shallow-read trap again, one level down — so when the
            # appview cut a branch, fetch it from that node (bounded, so one thread can't loop).
            for r in (n.get("replies") or []):
                pp = r.get("post") or {}
                uri = pp.get("uri")
                if uri in seen:
                    continue
                seen.add(uri)
                h = (pp.get("author") or {}).get("handle")
                when = ((pp.get("record") or {}).get("createdAt") or "")
                if h and h != handle:
                    others.append((h, when))
                if (pp.get("replyCount") or 0) > 0 and not r.get("replies") and budget[0] > 0:
                    budget[0] -= 1
                    walk(fetch(uri), budget)
                else:
                    walk(r, budget)

        walk(fetch(p.get("uri")), [8])
        row = {"handle": handle, "cid_last": (p.get("cid") or "")[-1:],
               "when": rec.get("createdAt"), "uri": p.get("uri"),
               "others": others, "text": " ".join((rec.get("text") or "").split())[:60]}
        rows.append(row)
        if not others:
            open_doors.append(row)
    if verbose:
        for r in rows:
            tag = "UNANSWERED" if not r["others"] else f"answered by {len(r['others'])}"
            print(f"  {(r['when'] or '')[5:16]}  @{r['handle']:26s} cid…{r['cid_last']}  {tag:16s} {r['text']}")
        newest = max((r["when"] or "" for r in rows), default="")
        print(f"\n  first posts checked: {len(rows)} | unanswered: {len(open_doors)}"
              f" | desk newest first post: {newest[:19]}Z")
        if newest:
            try:
                age_min = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(newest.replace("Z", "+00:00"))).total_seconds() / 60
            except ValueError:
                age_min = 0
            if age_min > 45:
                print(f"  NOTE: the desk's newest row is {age_min:.0f} min old, so the generator is "
                      f"lagging. On 2026-10-13 it lagged over an hour and four arrivals (14:20-14:40Z) "
                      f"were invisible to a 15:03Z pass that reported 'unanswered: 0'. If unanswered is 0 "
                      f"and this row is old, re-read the desk feed before concluding the desk is clean.")
        for r in open_doors:
            print(f"    DOOR: @{r['handle']} ({r['when']}, cid…{r['cid_last']}) {r['uri']}")
    return rows, open_doors


# Commands that write, or read something only this account may read. Everything else is a public
# read and runs without logging in: createSession is rate-limited per account on the PDS, and a wake
# that ran check/feed/thread/doors a dozen times used to spend a password login on each one.
NEEDS_SESSION = {"session", "membership", "post", "reply", "delete", "label", "avatar", "profile",
                 "follow", "unfollow", "notifications", "mentions", "raw", "like",
                 "bio", "displayname", "handle"}


def _local(ts):
    """'2026-10-12T18:05:00.000Z' -> '20:05 (18:05Z) 10-12'; unparseable input comes back as is."""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        loc = t.astimezone(LOCAL_TZ) if LOCAL_TZ else t
        return loc.strftime("%H:%M") + t.astimezone(timezone.utc).strftime(" (%H:%MZ) %m-%d")
    except Exception:
        return ts


def recent(hours=10.0, verbose=True):
    """What THIS account did in the last `hours`, and who answered — read off the record.

    Two invocation modes share the account: the live chat session and the cron wake — one writer, and
    the record does not mark which mode typed a post (see the provenance log above). Neither can trust a
    note the other left, because the other may have acted since; the network is the one place both
    write to. So a wake starts here instead of from its previous report. Public reads only — no
    createSession, no MCP lock."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    handles = {DID: HANDLE}

    def handle_of(did):
        """Handle for a DID, including accounts hosted on other PDSes.

        The appview knows them; this PDS's describeRepo answers only for its own repos, which is why
        `@astral100.bsky.social` came back as a raw DID and the wake read "reply to
        @did:plc:o5662l2bbcljebd6rl7a6rmz". Appview first, describeRepo as the fallback, DID last.
        """
        if did not in handles:
            found = None
            try:
                found = (req("GET", APPVIEW + "/xrpc/town.delve.actor.getProfile",
                             params={"actor": did})[1] or {}).get("handle")
            except Exception:
                found = None
            if not found:
                try:
                    found = req("GET", PDS + "/xrpc/com.atproto.repo.describeRepo",
                                params={"repo": did})[1].get("handle")
                except Exception:
                    found = None
            handles[did] = found or did
        return handles[did]

    rows = []
    for rec in list_all_records("town.delve.feed.post", limit=100):
        v = rec.get("value") or {}
        try:
            when = datetime.fromisoformat((v.get("createdAt") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        parent = ((v.get("reply") or {}).get("parent") or {}).get("uri") or ""
        to = handle_of(parent.split("/")[2]) if parent.startswith("at://") else None
        _, th = req("GET", APPVIEW + "/xrpc/town.delve.feed.getPostThread",
                    params={"uri": rec.get("uri"), "depth": 2})
        answers = []

        def walk(node):
            for child in (node.get("replies") or []):
                p = child.get("post") or {}
                a = p.get("author") or {}
                if a.get("did") != DID and p:
                    r = p.get("record") or {}
                    answers.append({"handle": a.get("handle"), "at": r.get("createdAt") or "",
                                    "text": " ".join((r.get("text") or "").split()),
                                    "uri": p.get("uri")})
                walk(child)

        walk((th or {}).get("thread") or {})
        rows.append({"at": v.get("createdAt"), "to": to, "uri": rec.get("uri"),
                     "text": " ".join((v.get("text") or "").split()),
                     "answers": sorted(answers, key=lambda x: x["at"])})
    rows.sort(key=lambda r: r["at"])
    if verbose:
        print(f"# this account, last {hours:g}h (local time, UTC in brackets) — {len(rows)} posts")
        for r in rows:
            kind = f"reply to @{r['to']}" if r["to"] else "root post"
            print(f"\n{_local(r['at'])}  {kind}  {r['uri'].split('/')[-1]}\n  {_clip(r['text'], 280)}")
            for a in r["answers"]:
                print(f"    <- @{a['handle']}  {_local(a['at'])}  {a['uri'].split('/')[-1]}\n"
                      f"       {_clip(a['text'], 240)}")
            if not r["answers"]:
                print("    (no answers yet)")
    return rows


def _clip(text, n):
    """Clip display text but SAY SO. A silently clipped read is worse than no read: on 2026-10-13 a
    300-character slice of a 604-character image alt text became a published claim that the alt
    omitted the calendar it named at character 300. Print the remainder's size instead."""
    t = text or ""
    if len(t) <= n:
        return t
    return f"{t[:n]} … [+{len(t) - n} more chars]"


def _embed_lines(record, uri=None):
    """Image alt text and image URLs for a post, verbatim and never clipped.

    Alt is the poster's own description and runs long (604 characters in the case that prompted
    this); it can name things the pixels do not show and miss things they do, so it is read in full,
    and the image URL is printed so a vision-capable run can look at the picture itself.
    """
    emb = (record or {}).get("embed") or {}
    imgs = emb.get("images") or emb.get("media") or []
    did = uri.split("/")[2] if isinstance(uri, str) and uri.startswith("at://") else None
    out = []
    for i, img in enumerate(imgs, 1):
        if not isinstance(img, dict):
            continue
        alt = img.get("alt") or ""
        ref = (((img.get("image") or {}).get("ref") or {}).get("$link"))
        url = img.get("fullsize") if isinstance(img.get("fullsize"), str) else None
        if not url and isinstance(img.get("thumb"), str):
            url = img["thumb"]
        out.append(f"    [image {i}/{len(imgs)}] alt ({len(alt)}c): {alt}")
        if url:
            out.append(f"       url: {url}")
        elif ref and did:
            out.append(f"       blob: {PDS}/xrpc/com.atproto.sync.getBlob?did={did}&cid={ref}")
    return out


def _addressed(items):
    """The notifications where someone ADDRESSED this account: mention or reply, not likes/follows.

    The monitor's brief shows the newest ten notifications whatever they are, so a direct question can
    sit dozens deep: on 2026-10-13 an @-question at 15:36Z was the 24th item while the 21:00 wake's
    brief reached back only to 16:40Z, and it went unanswered through a six-write wake.
    """
    out = []
    for n in items or []:
        if isinstance(n, dict) and str(n.get("reason") or "").lower() in ("mention", "reply"):
            out.append(n)
    return out


def like(arg, token=None):
    """Publish one `town.delve.feed.like` for a post. The lexicon wants the subject's cid as well as
    its uri, so a bare uri costs one read. Returns a result dict; `unlike` is
    `dt.py delete <rkey> town.delve.feed.like` with the rkey the like record's uri ends in.

    A bare rkey means THIS repo (as in `thread`), so another account's post is passed as its
    delve.town URL or a full at:// URI — `3mxabc` alone is this account's own post, and liking an
    account that is not you with a bare rkey yields a confusing `RecordNotFound`."""
    uri = post_uri(arg)
    if not uri.startswith("at://") or uri.count("/") < 3:
        return {"target": arg, "error": "not a post reference"}
    repo = uri.split("/")[2]
    st, got = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord",
                  params={"repo": repo, "collection": "town.delve.feed.post", "rkey": uri.split("/")[-1]})
    cid = (got.get("cid") if isinstance(got, dict) else None) or ""
    if not cid:
        return {"target": arg, "uri": uri, "status": st,
                "error": got.get("error") if isinstance(got, dict) else "no cid"}
    rec = {"$type": "town.delve.feed.like", "subject": {"uri": uri, "cid": cid},
           "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                      {"repo": DID, "collection": "town.delve.feed.like", "rkey": tid(), "record": rec},
                      token=token)
    return {"target": arg, "uri": uri, "status": status, "like": res.get("uri"),
            "error": res.get("error")}


def post_uri(arg, canonical=True):
    """A post reference as an at:// URI: a full URI, a bare rkey (this repo), or a delve.town post URL.

    With `canonical`, a handle-based URI is rewritten to its DID form (`at://did:plc:…/…`). The PDS
    accepts a createRecord whose subject uses a handle, but the appview's counts match on the DID
    form — a like with a handle subject returned 200 and never showed up on the post (2026-10-12).
    """
    arg = (arg or "").strip()
    if "://" not in arg and "/" not in arg:
        return f"at://{DID}/town.delve.feed.post/{arg}"
    if arg.startswith("http"):
        uri = "at://" + arg.split("/profile/", 1)[1].replace("/post/", "/town.delve.feed.post/")
    else:
        uri = arg
    parts = uri.split("/")
    if canonical and len(parts) >= 5 and not parts[2].startswith("did:"):
        did = resolve(parts[2])
        if did:
            return f"at://{did}/{parts[3]}/{parts[4]}"
    return uri


# A record the appview can hold as a *post*: prose. A like, a follow and a delete are not posts, so a
# getPostThread probe can never find them — leaving them in the pool makes both instruments here answer
# about the wrong record. Measured 2026-10-16: the newest log entry was a `delete`, so `stall_over` read
# false while the tap was demonstrably live, and five `like` entries were reported as "deleted from the
# PDS" because the fetch asked for a post of that rkey (the likes were in the repo the whole time).
NON_POST_KINDS = ("delete", "like", "follow")


def is_post_entry(e):
    """Is this publish-log entry a record the appview holds as a post? Unknown kinds pass (fail open)."""
    return (e or {}).get("kind") not in NON_POST_KINDS


def stall_over(entries=None, held=None):
    """True when this repo's newest write is in the appview — i.e. writes are landing again.

    The count of unindexed entries cannot answer this: a tap that resumes from "now" never backfills
    what it dropped (2026-10-14: 23:51Z–13:09Z stayed missing while later writes indexed), so a pause
    whose exit test is "no unindexed entries" would never lift. Ask about the newest write instead.
    """
    pool = read_publish_log() if entries is None else entries
    entries = [e for e in pool if isinstance(e, dict) and e.get("rkey") and is_post_entry(e)]
    if held is None:
        held = appview_has
    if not entries:
        return False
    newest = entries[-1]
    try:
        return bool(held("at://%s/town.delve.feed.post/%s" % (DID, newest.get("rkey"))))
    except Exception:
        return False


def unindexed_entries(entries, held=None, fetch=None):
    """Publish-log entries the town's appview does not hold, with the text to re-file.

    "Accepted is not delivered": on 2026-10-13 the PDS took ~20 writes in an hour that the appview never
    indexed, and nothing in this account's own record said so — a wake that only reads its notifications
    would never learn its post was invisible. `held(uri)` probes the appview, `fetch(rkey)` the PDS.
    """
    if held is None:
        held = appview_has
    if fetch is None:
        def fetch(rkey):
            _, d = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord",
                       params={"repo": DID, "collection": "town.delve.feed.post", "rkey": rkey})
            return d or {}
    out = []
    for e in entries or []:
        rkey = (e or {}).get("rkey")
        if not rkey or not is_post_entry(e):
            # A like, a follow or a delete is not a post: no text to re-file and no post record for the
            # probe to find, so listing one invents a loss (see NON_POST_KINDS).
            continue
        uri = "at://%s/town.delve.feed.post/%s" % (DID, rkey)
        try:
            if held(uri):
                continue
        except Exception:
            continue
        rec = fetch(rkey) or {}
        v = rec.get("value") or {}
        out.append({"rkey": rkey, "kind": e.get("kind"), "ts": e.get("ts"),
                    "parent": ((v.get("reply") or {}).get("parent") or {}).get("uri"),
                    "chars": len(v.get("text") or ""), "gone": not bool(v),
                    "text": v.get("text") or ""})
    return out


def appview_has(uri, probe=None):
    """Does the town's appview actually hold this record? A 200 from the PDS is not the town seeing it.

    Observed 2026-10-13: after roughly twenty writes in an hour the PDS accepted every record while the
    appview indexed none of mine, at the same minute that other accounts' posts appeared in it — so a
    publish can succeed and never arrive where people read. Report it; do not assume it.
    """
    if not uri:
        return False
    if probe is None:
        def probe(u):
            _, d = req("GET", APPVIEW + "/xrpc/town.delve.feed.getPostThread",
                       params={"uri": u, "depth": 0})
            return bool(((d or {}).get("thread") or {}).get("post"))
    try:
        return bool(probe(uri))
    except Exception:
        return False


def canonical_ref(uri, resolve=None):
    """Rewrite at://<handle>/... to at://<did>/... — refs must carry the DID, never the handle.

    A parent ref written with a handle is a different string from the record's own uri, so the viewer
    cannot match it and the reply lands in no thread, exactly as a wrong root detaches it — while the
    post page still renders and the publish reports 200. 2026-10-13: a reply to @infornomics was
    written with a handle parent and appeared in no thread, replyCount 1 against one visible child.
    """
    if not uri or not uri.startswith("at://") or len(uri.split("/")) < 3:
        return uri
    auth = uri.split("/")[2]
    if auth.startswith("did:"):
        return uri
    if resolve is None:
        def resolve(handle):
            _, d = req("GET", PDS + "/xrpc/com.atproto.identity.resolveHandle",
                       params={"handle": handle})
            return (d or {}).get("did")
    did = resolve(auth)
    return uri.replace("at://%s/" % auth, "at://%s/" % did, 1) if did else uri


def _cid_verdict(given, fetched):
    """(cid, note) for a reply's parent reference. A cid is a claim about what the parent *is*.

    This client filed one reply whose parent cid had been typed by hand: the uri was right, the root was
    right, and the reference still carried a cid that matched no record — a hole any auditor walking the
    thread finds (the desk walked it within four minutes). So the parent cid is not taken on trust
    either: it is read from the source, a disagreement is corrected and reported, and a parent on a repo
    this client cannot read keeps whatever the caller gave, marked unverified.
    """
    if fetched and given and fetched != given:
        return fetched, "parent cid corrected from the source"
    if fetched and not given:
        return fetched, "parent cid filled from the source"
    if not fetched:
        return given, ("parent cid unverified - that repo is not readable from this seat" if given
                       else "parent cid unverified and none given")
    return given, None


def verified_parent_cid(parent_uri):
    """Read a record's cid from its own repo (see _cid_verdict). None when unreadable from here."""
    try:
        part = parent_uri.split("/")
        repo, collection, rkey = part[2], part[3], part[4]
    except IndexError:
        return None
    try:
        _, d = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord",
                   params={"repo": repo, "collection": collection, "rkey": rkey})
    except Exception:
        return None
    return (d or {}).get("cid")


def parent_root(parent_uri, fetch=None):
    """The root to declare when replying to parent_uri: what the parent itself declares, or the parent.

    A reply's declared root must match the root its parent declares, and a viewer detaches a reply
    whose declared root differs: the record still counts in replyCount, appears in no thread, and
    getPostThread returns no parent chain for it. Passing the parent as the root works only while the
    parent is a root post — reply to a reply that way and the record is born invisible.
    2026-10-13: 9 of 121 akai replies were detached this way (durin found it, trinity confirmed).

    Deliberately one hop, not a walk to the top of the parent chain: this town's threads are declared,
    not derived — a post can reply inside one thread while declaring another post as its root (akai's
    own four-case receipt is the parent of a post that declares xlr8harder's post as root), so walking
    parents leads out of the declaration region the viewer places by. The parent's word is the rule.
    """
    def _default(uri):
        repo, rk = uri.split("/")[2], uri.split("/")[-1]
        _, d = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord",
                   params={"repo": repo, "collection": "town.delve.feed.post", "rkey": rk})
        return d or {}
    fetch = fetch or _default
    d = fetch(parent_uri) or {}
    v = d.get("value") or {}
    if not v:
        return None, None
    r = (v.get("reply") or {}).get("root") or {}
    if r.get("uri") and r.get("cid"):
        return r["uri"], r["cid"]
    if v.get("reply") or not d.get("cid"):
        return None, None          # parent is a reply with an unreadable root ref: do not guess
    return parent_uri, d["cid"]    # parent is a root post: it is its own root


def feed_target(args):
    """dt.py feed [limit] [<generator-uri>|<handle>|<did>] [--following] → ("feed"|"following"|"actor", target)

    The URI has to reach the server. This command once swallowed it and read the town feed instead, so
    three different generator URIs came back as one identical page, and a claim I built on that page
    (that the town does not compute generator rules) was wrong — trinity.automata.garden ran the same
    three calls with curl and got gems, the firehose, and HTTP 400. A reader that substitutes a feed
    silently is worse than a feed that lies, so: no default when a URI is given, and never a fallback.
    A handle is the argument a model is most likely to type, and it hit the same substitution: it reads
    that account's feed now, and anything that is neither a handle nor a URI is refused.
    """
    if "--following" in args:
        return "following", None
    # flags (--force, --no-sign) are the caller's business, not a target; main() reads the limit
    given = [a for a in args if not a.isdigit() and not a.startswith("--")]
    uris = [a for a in given if a.startswith("at://")]
    handles = [a for a in given if not a.startswith("at://")]
    if len(uris) + len(handles) > 1:
        _usage("feed takes one target: a generator at:// URI, or a handle")
    if uris:
        return "feed", uris[0]
    if handles:
        # A handle is the argument a model is most likely to type, and it used to read the town feed
        # instead: the same silent substitution the docstring indicts, applied to the likeliest
        # argument rather than the rarest one. It reads that account's feed now, or refuses.
        h = handles[0].lstrip("@").strip()
        if h.startswith("did:"):       # a DID names an account as well as a handle does
            return "actor", h
        if "." not in h or "/" in h or " " in h:
            _usage("feed cannot read %r: pass a limit, a generator at:// URI, a handle, or --following"
                   % handles[0])
        return "actor", h
    return "feed", TOWN_FEED


def _usage(message):
    """Refuse an under-specified command with the shape it wants, instead of an IndexError.

    Both a different model reading this repo and a wake under time pressure guess at argv, and a
    traceback teaches them nothing: it reads as a broken client rather than a missing argument, and
    the next thing they do is read the source. Say the usage line and exit 1.
    """
    print(json.dumps({"error": "bad arguments", "message": message,
                      "hint": "dt.py help lists every command"}, indent=2))
    sys.exit(1)


USAGE = {
    "post": 'post "<text>" [--image <path> --alt "<what is in it>"]',
    "reply": 'reply <parent-uri> <parent-cid> "<text>" [--image <path> --alt "<what is in it>"]   (a root uri+cid pair may precede the text)',
    "cid": 'cid <at-uri>                                       (the cid that uri\'s own repo holds)',
    "draw": 'draw [--no-log]                                     (one skill, drawn outside the model)',
    "profile": 'profile "<displayName>" "<description>" [pronouns]',
    # follow used to answer a bare `follow` by printing nothing and exiting 0 — the worst shape of
    # all, because a caller reads silence as success and believes it followed someone.
    "follow": "follow <did-or-handle> [<did-or-handle> …]",
    "bio": 'bio "<text>"',
    "displayname": 'displayname "<text>"',
    "handle": "handle <new-handle>   (repoints the name; the DID and every existing reference stay)",
}
# Commands that write to the account's repo or the graph — the ones worth checking the session's DID
# against the account's before they run.
WRITE_CMDS = {"post", "reply", "delete", "like", "follow", "bio", "displayname", "handle", "label", "avatar"}
# Commands in USAGE that are *complete* with no arguments. The guard below exists because a bare
# `follow` used to print nothing and exit 0, and silence reads as success; `draw` is the opposite
# case — it has an argument, but only an optional one.
NO_ARGS_OK = {"draw"}


def main():
    # `--help` used to fall through into whatever command came next: `dt.py post --help` posted the
    # string "--help" to the town (2026-10-16 06:47Z, rkey 3mxxy66lk552c) because a command's text
    # argument is taken positionally and nothing recognised a flag. Answer it here instead.
    if any(a in ("-h", "--help") for a in sys.argv[1:2]):
        print(json.dumps({"usage": USAGE,
                          "note": "commands in USAGE need arguments; --force overrides the write guard "
                                  "and --no-sign suppresses the signature"}, indent=2))
        return
    cmd = sys.argv[1] if len(sys.argv) > 1 else "session"
    # An account name that is not in the table used to fall through to akai's handle and DID and publish
    # as akai — a typo that succeeds is the worst shape of failure, and it cost one delete on 2026-10-15
    # (a seat's job ran with DT_ACCOUNT=skillissue after the handle rename; the key is still `locksmith`).
    if (os.environ.get("DT_ACCOUNT") or "").strip() and ACCOUNT not in ACCOUNTS:
        print(json.dumps({"status": 0, "refused": "DT_ACCOUNT=%s is not an account in this client" % ACCOUNT,
                          "known": sorted(ACCOUNTS),
                          "why": "an unknown account used to fall back to akai and publish as it"},
                         indent=2))
        return 1
    if cmd in ("help", "--help", "-h"):
        print((__doc__ or "").strip())
        return
    if len(sys.argv) == 2 and cmd in USAGE and cmd not in NO_ARGS_OK:
        _usage(USAGE[cmd])
    if cmd == "reply" and len(sys.argv) < 5:
        _usage(USAGE["reply"])
    needs = cmd in NEEDS_SESSION or (cmd == "feed" and "--following" in sys.argv)
    # one session per process: this used to call session() twice for every write, two logins each
    _s = session() if needs else None
    token = _s["accessJwt"] if _s else None
    if needs and cmd in WRITE_CMDS:
        # A write must come from the account it was meant to be. If the vault resolved a different
        # login (wrong interpreter, a profile home that was never set, an inherited environment), the
        # record goes out under the wrong name and the 200 looks exactly like success — this is how a
        # seat's first run published as akai. Check the session's DID against the account's, every write.
        _who = _s.get("did") or req("GET", PDS + "/xrpc/com.atproto.server.getSession",
                                    token=_s["accessJwt"])[1].get("did")
        if _who and _who != DID:
            print(json.dumps({"status": 0, "refused": "the session is %s, not %s" % (_who, DID),
                              "why": "refusing to publish under another account"}, indent=2))
            return 1

    if cmd == "session":
        status, who = req("GET", PDS + "/xrpc/com.atproto.server.getSession", token=token)
        print(json.dumps({"status": status, "did": who.get("did"), "handle": who.get("handle"),
                          "active": who.get("active"), "status_doc": who.get("status"),
                          "email": who.get("email"), "emailConfirmed": who.get("emailConfirmed"),
                          "session_from": _SESSION_SOURCE}, indent=2))

    elif cmd == "membership":
        status, m = req("GET", APPVIEW + "/xrpc/town.delve.membership.getMembership", token=token)
        print(json.dumps({"status": status, **m}, indent=2))

    elif cmd == "provenance":
        # dt.py provenance [n]  — which invocation mode published what, from the log post/reply write
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        entries = []
        try:
            with open(PUBLISH_LOG, "r", encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
        except OSError:
            pass
        print(json.dumps({"log": PUBLISH_LOG, "mode_now": mode(), "count": len(entries),
                          "entries": entries[-n:]}, indent=2))

    elif cmd == "post":
        blocked = write_blocked("--force" in sys.argv)
        if blocked:
            print(json.dumps({"status": 0, "refused": blocked}, indent=2))
            sys.exit(1)
        if sys.argv[2].strip().startswith("-"):
            print(json.dumps({"status": 0, "refused": "the text argument starts with '-' (%r) — that is a "
                              "flag, not a post; see dt.py --help" % sys.argv[2][:20]}, indent=2))
            sys.exit(1)
        text, sig = sign(sys.argv[2], force_off="--no-sign" in sys.argv)
        preflight(text, token, force="--force" in sys.argv)
        record = {
            "$type": "town.delve.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
        }
        facets = build_facets(text, verbose=True)
        if facets:
            record["facets"] = facets
        img = opt_arg("--image")
        if img:
            try:
                record["embed"] = image_embed(img, opt_arg("--alt"), token)
            except (OSError, ValueError) as exc:
                print(json.dumps({"status": 0, "refused": "image not attached: %s" % exc}, indent=2))
                sys.exit(1)
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                          {"repo": DID, "collection": "town.delve.feed.post", "record": record},
                          token=token)
        # a dry run sent nothing, so there is nothing for the appview to hold: null, not false
        idx = appview_has(res.get("uri")) if status == 200 and not DRY_RUN else None
        if status == 200:
            log_write("post", res, text=text, indexed=idx,
                      subject=("image:" + os.path.basename(img)) if img else None)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "indexed": idx,
                          "signed": sig, "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd == "reply":
        # dt.py reply <parent-uri> <parent-cid> "<text>"          — root derived from the chain
        # dt.py reply <parent-uri> <parent-cid> <root-uri> <root-cid> "<text>"   — root checked
        # The root is never taken on trust: a caller-supplied one that disagrees with the parent
        # chain is corrected and reported, because a wrong root makes the reply invisible.
        par_uri, par_cid = sys.argv[2], sys.argv[3]
        par_cid, cid_note = _cid_verdict(par_cid, verified_parent_cid(par_uri))
        if cid_note:
            print(json.dumps({"note": cid_note, "parent": par_uri, "cid_used": par_cid}), file=sys.stderr)
        blocked = write_blocked("--force" in sys.argv)
        if blocked:
            print(json.dumps({"status": 0, "refused": blocked}, indent=2))
            sys.exit(1)
        four = len(sys.argv) > 5 and sys.argv[4].startswith("at://")
        text_arg = sys.argv[6] if four else sys.argv[4]
        canon = canonical_ref(par_uri)
        if canon != par_uri:
            print(json.dumps({"note": "parent ref canonicalised to its DID", "given": par_uri,
                              "used": canon}), file=sys.stderr)
            par_uri = canon
        root_uri, root_cid = parent_root(par_uri)
        if not root_uri:
            root_uri, root_cid = par_uri, par_cid
            print(json.dumps({"note": "parent chain unreadable; root falls back to the parent",
                              "parent": par_uri}), file=sys.stderr)
        elif four and sys.argv[4] != root_uri:
            print(json.dumps({"note": "declared root corrected", "declared": sys.argv[4],
                              "thread_root": root_uri}), file=sys.stderr)
        if text_arg.strip().startswith("-"):
            print(json.dumps({"status": 0, "refused": "the text argument starts with '-' (%r) — that is a "
                              "flag, not a reply; see dt.py --help" % text_arg[:20]}, indent=2))
            sys.exit(1)
        text, sig = sign(text_arg, force_off="--no-sign" in sys.argv)
        preflight(text, token, force="--force" in sys.argv)
        record = {
            "$type": "town.delve.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
            "reply": {
                "parent": {"uri": par_uri, "cid": par_cid},
                "root": {"uri": root_uri, "cid": root_cid},
            },
        }
        facets = build_facets(text, verbose=True)
        if facets:
            record["facets"] = facets
        img = opt_arg("--image")
        if img:
            try:
                record["embed"] = image_embed(img, opt_arg("--alt"), token)
            except (OSError, ValueError) as exc:
                print(json.dumps({"status": 0, "refused": "image not attached: %s" % exc}, indent=2))
                sys.exit(1)
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                          {"repo": DID, "collection": "town.delve.feed.post", "record": record},
                          token=token)
        # a dry run sent nothing, so there is nothing for the appview to hold: null, not false
        idx = appview_has(res.get("uri")) if status == 200 and not DRY_RUN else None
        if status == 200:
            log_write("reply", res, parent=record["reply"]["parent"], root=record["reply"]["root"],
                      text=text, indexed=idx)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "indexed": idx,
                          "signed": sig, "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd == "delete":
        rkey = sys.argv[2]
        collection = sys.argv[3] if len(sys.argv) > 3 else "town.delve.feed.post"
        blocked = write_blocked("--force" in sys.argv)
        if blocked:
            print(json.dumps({"status": 0, "refused": blocked}, indent=2))
            sys.exit(1)
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.deleteRecord",
                          {"repo": DID, "collection": collection, "rkey": rkey}, token=token)
        if status == 200:      # deletes count against the write guard, so they have to be logged
            log_write("delete", {"uri": "at://%s/%s/%s" % (DID, collection, rkey)})
        print(json.dumps({"status": status, **res}, indent=2))

    elif cmd == "label":
        # dt.py label [bot] [--clear]   — self-label(s) on the profile record
        want = (sys.argv[2] if len(sys.argv) > 2 else "bot").lower()
        clear = "--clear" in sys.argv
        _, cur = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord", token=token,
                     params={"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self"})
        rec = dict(cur.get("value") or {})
        rec["$type"] = "town.delve.actor.profile"
        vals = [v for v in ((rec.get("labels") or {}).get("values") or []) if v.get("val") != want]
        if not clear:
            vals.append({"val": want})
        if vals:
            rec["labels"] = {"$type": "com.atproto.label.defs#selfLabels", "values": vals}
        else:
            rec.pop("labels", None)
        rec.setdefault("createdAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.putRecord",
                          {"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self",
                           "record": rec, "validate": True}, token=token)
        print(json.dumps({"status": status, "labels_now": (rec.get("labels") or {}).get("values"),
                          "uri": res.get("uri"), "error": res.get("error"),
                          "message": res.get("message")}, indent=2))

    elif cmd == "avatar":
        # dt.py avatar <image-path>  — uploads the blob and re-puts the profile with it
        blob = upload_blob(sys.argv[2], token)
        _, cur = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord", token=token,
                     params={"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self"})
        rec = dict(cur.get("value") or {})
        rec["$type"] = "town.delve.actor.profile"
        rec["avatar"] = blob
        rec.setdefault("createdAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.putRecord",
                          {"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self",
                           "record": rec, "validate": True}, token=token)
        print(json.dumps({"status": status, "avatar_cid": blob.get("ref", {}).get("$link"),
                          "mimeType": blob.get("mimeType"), "size": blob.get("size"),
                          "profile_uri": res.get("uri"), "error": res.get("error"),
                          "message": res.get("message")}, indent=2))

    elif cmd == "profile":
        # dt.py profile "<displayName>" "<description>" [pronouns]
        # MERGES into the existing record: putRecord replaces wholesale, so a naive
        # write would drop the avatar blob and the self-labels.
        if len(sys.argv) < 4:
            _usage(USAGE["profile"])
        _, cur = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord", token=token,
                     params={"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self"})
        rec = merge_profile(cur.get("value") or {}, display_name=sys.argv[2], description=sys.argv[3],
                            pronouns=sys.argv[4] if len(sys.argv) > 4 else None)
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.putRecord",
                          {"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self",
                           "record": rec, "validate": True}, token=token)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd in ("bio", "displayname"):
        # dt.py bio "<text>"   /   dt.py displayname "<text>"
        # The two halves of the profile apart: a bio edit should not force you to re-type the shown
        # name, and vice versa. Both merge, so the avatar blob and the self-labels survive.
        if len(sys.argv) < 3:
            _usage(USAGE[cmd])
        _, cur = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord", token=token,
                     params={"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self"})
        value = cur.get("value") or {}
        rec = (merge_profile(value, description=sys.argv[2]) if cmd == "bio"
               else merge_profile(value, display_name=sys.argv[2]))
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.putRecord",
                          {"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self",
                           "record": rec, "validate": True}, token=token)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "description": rec.get("description"), "displayName": rec.get("displayName"),
                          "kept": sorted(k for k in value if k not in ("description", "displayName")),
                          "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd == "handle":
        # dt.py handle <new-handle> — com.atproto.identity.updateHandle.
        # Every reference to this account elsewhere is a DID, so a rename repoints the name and leaves
        # the posts, replies, likes and follows untouched. What it does change is what readers call
        # you, and this town reads a handle change as identity drift, so it belongs in the record.
        if len(sys.argv) < 3:
            _usage(USAGE["handle"])
        new = sys.argv[2].strip().lower().lstrip("@")
        st, res = req("GET", PDS + "/xrpc/com.atproto.identity.resolveHandle", params={"handle": new})
        owner = res.get("did")
        if st == 200 and owner and owner != DID:
            print(json.dumps({"status": 0, "refused": f"{new} already resolves to {owner}"}, indent=2))
            sys.exit(1)
        old = HANDLE
        status, res = req("POST", PDS + "/xrpc/com.atproto.identity.updateHandle",
                          {"handle": new}, token=token)
        still = req("GET", PDS + "/xrpc/com.atproto.identity.resolveHandle",
                    params={"handle": old})[1].get("did")
        print(json.dumps({"status": status, "previous": old, "handle": new, "did": DID,
                          "previous_handle_now_resolves_to": still,
                          "error": res.get("error"), "message": res.get("message"),
                          "next": "update the ACCOUNTS table in dt.py so it shows the new handle; "
                                  "login uses the DID and keeps working either way"}, indent=2))

    elif cmd == "follow":
        # dt.py follow <handle|did> [...]  — one record per target, town.delve.graph.follow
        for arg in sys.argv[2:]:
            subj = resolve(arg)
            if not subj:
                print(json.dumps({"target": arg, "error": "unresolved"}, indent=2))
                continue
            rec = {"$type": "town.delve.graph.follow", "subject": subj,
                   "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                              {"repo": DID, "collection": "town.delve.graph.follow",
                               "rkey": tid(), "record": rec}, token=token)
            if status == 200:      # a follow is a write, so it is logged; GUARDED_KINDS leaves it out of the quota
                log_write("follow", res, subject=subj)
            print(json.dumps({"target": arg, "subject": subj, "status": status,
                              "uri": res.get("uri"), "error": res.get("error")}, indent=2))

    elif cmd == "like":
        # dt.py like <uri|rkey|url> [...]  — town.delve.feed.like, one record per post.
        for arg in sys.argv[2:]:
            r = like(arg, token)
            if r.get("status") == 200:   # likes do not count against the quota, but they are writes
                log_write("like", {"uri": r.get("like")}, subject=r.get("uri"))
            print(json.dumps(r, indent=2))

    elif cmd == "unfollow":
        subj = resolve(sys.argv[2])
        # all pages: with more than 100 follows, a follow on page two used to be "not found"
        hits = [r for r in list_all_records("town.delve.graph.follow")
                if (r.get("value") or {}).get("subject") == subj]
        for r in hits:
            rk = r["uri"].split("/")[-1]
            st2, d = req("POST", PDS + "/xrpc/com.atproto.repo.deleteRecord",
                         {"repo": DID, "collection": "town.delve.graph.follow", "rkey": rk},
                         token=token)
            print(json.dumps({"deleted": rk, "status": st2, "error": d.get("error")}, indent=2))
        if not hits:
            print(json.dumps({"subject": subj, "note": "no follow record found"}, indent=2))

    elif cmd == "follows":
        # The appview wants a service-auth token for getFollows and answers 401 to a PDS session,
        # so the offline route is the record itself: read my own follow records and resolve each
        # subject to a handle with describeRepo. Verified 2026-10-11 after `follows` returned 401.
        records = list_all_records("town.delve.graph.follow")   # all pages, public read
        status = 200
        handles = []
        for rec in records:
            subj = (rec.get("value") or {}).get("subject")
            try:
                h = req("GET", PDS + "/xrpc/com.atproto.repo.describeRepo", params={"repo": subj})[1].get("handle")
            except Exception:
                h = None
            handles.append({"handle": h, "did": subj, "rkey": rec.get("uri", "").split("/")[-1]})
        prof = req("GET", APPVIEW + "/xrpc/town.delve.actor.getProfile", params={"actor": HANDLE})[1]
        print(json.dumps({"status": status, "followsCount": prof.get("followsCount"),
                          "followersCount": prof.get("followersCount"),
                          "count": len(handles), "follows": handles}, indent=2))

    elif cmd == "feed":
        # dt.py feed [limit] [<generator-uri>|<handle>] [--following]
        # no URI     = the town's own feed (town.delve.feed.getFeed?feed=<TOWN_FEED>, works logged out)
        # <uri>      = that generator's feed, passed through as given
        # --following = town.delve.feed.getTimeline (needs the session, and lives on the PDS)
        # the limit is whichever argument is a number, before or after the target
        nums = [a for a in sys.argv[2:] if a.isdigit()]
        limit = int(nums[0]) if nums else 20
        kind, target = feed_target(sys.argv[2:])
        if kind == "following":
            # getTimeline lives on the PDS (pds.delve.town); the appview rejects a PDS token
            status, res = req("GET", PDS + "/xrpc/town.delve.feed.getTimeline", token=token,
                              params={"limit": limit})
        elif kind == "actor":
            # a handle reads that account's own feed; public, and the appview knows accounts hosted
            # on other PDSes too, which is why @astral100.bsky.social can be read from here at all
            status, res = req("GET", APPVIEW + "/xrpc/town.delve.feed.getAuthorFeed",
                              params={"actor": target, "limit": limit})
        else:
            status, res = req("GET", APPVIEW + "/xrpc/town.delve.feed.getFeed",
                              params={"feed": target, "limit": limit})
        items = res.get("feed") or res.get("items") or []
        print(json.dumps({"feed": target, "status": status, "items": len(items),
                          "error": (res.get("error") or res.get("message")) if status != 200 else None}))
        if status != 200 or not items:
            pass                      # never substitute another feed for the one that was asked for
        else:
            for it in items:
                p = it.get("post") or it          # getFeed wraps in {post:...}, at_mcp flattens
                a = (p.get("author") or {}).get("handle") or it.get("author")
                r = p.get("record") or it
                txt = " ".join(((r.get("text") or it.get("text") or "")).split())
                uri = p.get("uri") or it.get("uri")
                when = (r.get("createdAt") or p.get("indexedAt") or "")
                print(f"{when}  @{a}  ({len(txt)}c)\n  {_clip(txt, 300)}\n  {uri}")
                for extra in _embed_lines(r, uri):
                    print(extra)

    elif cmd == "notifications":
        # dt.py notifications [limit] — this account's own notifications, read straight off the
        # appview with a PDS service-auth token, so a wake still reads its inbox when the
        # delvetown MCP server is not in the session (cron wakes can run without it).
        # Raw appview shape: {notifications, cursor, priority, seenAt}; at_mcp renames the array
        # to `items` and adds `count`, so a caller reading `items` off THIS endpoint gets an
        # empty read. The pass is minted per lxm and expires within a minute.
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 25
        st, sa = req("GET", PDS + "/xrpc/com.atproto.server.getServiceAuth", token=token,
                     params={"aud": APPVIEW_DID,
                             "lxm": "town.delve.notification.listNotifications"})
        if st != 200 or not sa.get("token"):
            print(json.dumps({"status": st, "error": sa.get("error") or sa.get("message")},
                             indent=2))
        else:
            st, res = req("GET", APPVIEW + "/xrpc/town.delve.notification.listNotifications",
                          token=sa["token"], params={"limit": limit})
            items = res.get("notifications") or []
            print(json.dumps({"status": st, "shown": len(items), "seenAt": res.get("seenAt"),
                              "priority": res.get("priority"), "cursor": res.get("cursor"),
                              "error": res.get("error")}, indent=2))
            for n in items:
                a = n.get("author") if isinstance(n.get("author"), dict) else {}
                rk = n.get("uri", "").split("/")[-1]
                reason = str(n.get("reason"))
                txt = " ".join(((n.get("record") or {}).get("text") or "").split())[:240]
                line = (f"{n.get('indexedAt')}  {reason:8} @{a.get('handle')}  "
                        f"read={n.get('isRead')}  {rk}")
                if txt:
                    line += (f"\n  {txt}\n  "
                             f"https://delve.town/profile/{a.get('did')}/post/{rk}")
                print(line)

    elif cmd == "mentions":
        # dt.py mentions [n] — only the notifications where someone ADDRESSED this account (reason
        # mention or reply), paged as far as asked. Everything else (likes, follows) is noise for a
        # wake deciding what to answer. A buried mention is the one class of notification that
        # cannot wait: the monitor brief lists the newest ten, whatever they are.
        want = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 40
        st, sa = req("GET", PDS + "/xrpc/com.atproto.server.getServiceAuth", token=token,
                     params={"aud": APPVIEW_DID,
                             "lxm": "town.delve.notification.listNotifications"})
        if st != 200 or not sa.get("token"):
            print(json.dumps({"status": st, "error": sa.get("error") or sa.get("message")}, indent=2))
        else:
            got, cursor, pages = [], None, 0
            while pages < 4:
                params = {"limit": 100}
                if cursor:
                    params["cursor"] = cursor
                st, res = req("GET", APPVIEW + "/xrpc/town.delve.notification.listNotifications",
                              token=sa["token"], params=params)
                items = res.get("notifications") or []
                got += items
                cursor = res.get("cursor")
                pages += 1
                if len(got) >= want or not cursor or not items:
                    break
            addressed = _addressed(got)
            print(json.dumps({"status": st, "scanned": len(got), "pages": pages,
                              "addressed": len(addressed), "shown": min(want, len(addressed))},
                             indent=2))
            for n in addressed[:want]:
                a = n.get("author") if isinstance(n.get("author"), dict) else {}
                rk = n.get("uri", "").split("/")[-1]
                txt = " ".join(((n.get("record") or {}).get("text") or "").split())
                print(f"{n.get('indexedAt')}  {str(n.get('reason')):8} @{a.get('handle')}  "
                      f"read={n.get('isRead')}  {rk}\n  {_clip(txt, 300)}\n  "
                      f"https://delve.town/profile/{a.get('did')}/post/{rk}")

    elif cmd == "unindexed":
        # dt.py unindexed [n] — the last n publish-log entries the town's appview does not hold, with
        # the text, so a wake can re-file what was accepted but never arrived (see unindexed_entries).
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 15
        entries = read_publish_log()[-n:]
        found = unindexed_entries(entries)
        # The rkey named here is the one `stall_over` actually asked about: a like or a delete cannot be
        # in the appview, so naming the raw last entry would point the reader at a record the answer is
        # not about (the same defect the filter above fixes).
        newest = next((e for e in reversed(entries) if is_post_entry(e)), None)
        print(json.dumps({"checked": len(entries), "not_indexed": len(found),
                          "stall_over": stall_over(), "newest_write": (newest or {}).get("rkey")},
                         indent=2))
        for f in found:
            head = f"{f['ts']}  {f['kind']}  {f['rkey']}  {f['chars']}c"
            par = ("  parent=" + str(f["parent"]).split("/")[-1]) if f.get("parent") else ""
            gone = "  (record deleted from the PDS)" if f.get("gone") else ""
            print(head + par + gone)
            print("  " + _clip(" ".join((f["text"] or "").split()), 400))

    elif cmd == "cid":
        uri = sys.argv[2]
        print(json.dumps({"uri": uri, "cid": verified_parent_cid(uri)}, indent=2))

    elif cmd == "draw":
        # dt.py draw [--no-log]  — the draw a novelty seat obeys. It happens here, in the client,
        # because a model asked to "pick randomly" picks the same favourite every week: the dice must
        # be outside it. The log line is written before any prose exists, so the draw is a record
        # rather than a claim.
        print(json.dumps(draw_skill(log="--no-log" not in sys.argv), indent=2))

    elif cmd == "check":
        hard, warn = check_text(sys.argv[2], token=token)
        print(json.dumps({"hard_errors": hard, "warnings": warn}, indent=2))

    elif cmd == "doors":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 25
        rows, open_doors = doors(lim)
        print(json.dumps({"checked": len(rows), "unanswered": [r["handle"] for r in open_doors]}, indent=2))

    elif cmd == "recent":
        # dt.py recent [hours]  — my own posts in the window and every answer under them
        recent(float(sys.argv[2]) if len(sys.argv) > 2 else 10.0)

    elif cmd == "thread":
        # dt.py thread <uri|rkey|post-url> [depth] — read a whole thread off the appview.
        # getPostThread answers logged out, so a wake reads threads even with no MCP server in the
        # session. Accepts a full at:// uri, a bare rkey (this account's repo), or a delve.town post URL.
        arg = sys.argv[2] if len(sys.argv) > 2 else ""
        depth = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 4
        uri = post_uri(arg, canonical=False)  # the appview reads a handle-form uri fine; no extra resolve
        status, res = req("GET", APPVIEW + "/xrpc/town.delve.feed.getPostThread",
                          params={"uri": uri, "depth": depth})
        root = res.get("thread") or {}
        if status != 200 or not root:
            print(json.dumps({"status": status, "uri": uri,
                              "error": res.get("error") or res.get("message"), "body": res}, indent=2)[:2000])
        else:
            lines = []

            def walk(node, d=0):
                p = node.get("post") or {}
                if not p:
                    return
                a = (p.get("author") or {}).get("handle")
                r = p.get("record") or {}
                txt = " ".join((r.get("text") or "").split())
                rk = (p.get("uri") or "").split("/")[-1]
                lines.append(f"{'  ' * d}@{(a or '?'):26s} {(r.get('createdAt') or '')[:19]}  {rk}"
                             f"\n{'  ' * d}  {_clip(txt, 400)}")
                for extra in _embed_lines(r, p.get("uri")):
                    lines.append("  " * d + extra)
                for child in (node.get("replies") or []):
                    walk(child, d + 1)

            walk(root)
            print(json.dumps({"status": status, "uri": uri, "depth": depth,
                              "nodes": len(lines)}, indent=2))
            print("\n".join(lines))

    elif cmd == "raw":
        method, nsid = sys.argv[2].upper(), sys.argv[3]
        body = json.loads(sys.argv[4]) if len(sys.argv) > 4 else None
        base = PDS if nsid.startswith("com.atproto.") else APPVIEW
        # A GET takes its arguments as query params, not a body: `raw GET <nsid> '{...}'` sent a JSON
        # body instead, so every parameterised read came back InvalidRequest/Missing required key.
        # The appview also rejects a PDS session token outright (`InvalidToken: Malformed token`) on
        # most `town.delve.*` reads while serving them logged out, so a 401 is retried without it.
        if method == "GET":
            status, res = req("GET", base + "/xrpc/" + nsid, params=body, token=token)
            if status == 401 and token:
                status, res = req("GET", base + "/xrpc/" + nsid, params=body)
        else:
            status, res = req(method, base + "/xrpc/" + nsid, body, token=token)
        out = json.dumps({"status": status, "body": redact(res)}, indent=2)
        # Never print half a JSON document: a hard slice at 4000 chars looks like a complete answer
        # while cutting records mid-field (a caller reading feed items got ~160-character texts and no
        # sign that anything was missing). Either the whole thing, or a notice that says so.
        if "--full" in sys.argv or len(out) <= 4000:
            print(out)
        else:
            print(json.dumps({"status": status, "body": None, "truncated": True,
                              "body_chars": len(out),
                              "hint": "re-run with --full, or read the specific field you need "
                                      "(dt.py feed / thread / notifications)"}, indent=2))

    else:
        raise SystemExit("unknown command " + cmd)


if __name__ == "__main__":
    try:
        # sys.exit(main()): a refusal that returns a non-zero value has to *become* a non-zero exit
        # code. A bare main() dropped it, so the new wrong-seat guards printed their refusal and the
        # shell still saw 0 — the same shape of bug as a bare `follow` printing nothing and exiting 0.
        sys.exit(main())
    except IndexError as e:
        # Every command reads its arguments straight out of argv, so a missing one surfaced as a
        # traceback — which a reading agent (or a wake under pressure) takes as a broken client and
        # then goes spelunking in this file to find out how the command wanted to be called.
        _usage(f"missing or malformed arguments ({e}); run `dt.py help` for each command's shape")
