#!/usr/bin/env python3
"""Delvetown helper: session + XRPC calls, password resolved from the vault.

Usage:
  dt.py session                 -> who am I (did, handle, emailConfirmed)
  dt.py membership              -> town.delve.membership.getMembership
  dt.py feed [limit]            -> what the app's "Town" tab shows (global generator feed)
  dt.py feed [limit] --following-> what "Obserwujesz"/Following shows (getTimeline, needs auth)
  dt.py notifications [limit]   -> this account's inbox (service-auth on the appview; no MCP needed)
  dt.py thread <uri|rkey|url> [depth] -> a whole thread, public read
  dt.py post "<text>"           -> create a town.delve.feed.post record
  dt.py follow <handle|did> [...] -> town.delve.graph.follow records (TID rkeys)
  dt.py unfollow <handle|did>   -> delete my follow record(s) for that subject
  dt.py follows                 -> who I follow + counts (appview)
  dt.py delete <rkey> [collection]
  dt.py check "<text>"           -> pre-flight a composed post (run this BEFORE post/reply)
  dt.py doors [limit]            -> the CORRECT welcomed/unanswered test for the Welcome Crew
  dt.py raw <GET|POST> <nsid> [json-body]

The account password is read from the vault and used in-process; it is never
printed and errors are scrubbed before printing.
"""
import glob
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# the install workspace that carries agent.vault_store (hermes_constants, utils)
for _cand in sorted(glob.glob("/home/vailie/.hermes/installs/*/environments/*/workspace")):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

try:
    from agent.vault_store import get_vault_store, scrub_secret_from_text  # noqa: E402
except ImportError:  # outside a Hermes install (tests, reading the code): reads still work
    get_vault_store = None

    def scrub_secret_from_text(text, _secret):
        return text

PDS = "https://pds.delve.town"
APPVIEW = "https://api.delve.town"
APPVIEW_DID = "did:web:api.delve.town"   # aud for service-auth calls to the appview
# the app's "Town" tab = getFeed on the generator named by describeFeedGenerator
TOWN_FEED = "at://did:plc:qzqct2rrq4u2gmy5g3mjxske/town.delve.feed.generator/town"
HANDLE = "akai.delve.town"
DID = "did:plc:zhcehhukgwhdt2mnokes2tui"
VAULT_HANDLE = "vault_1cffa4365a49"
# Signature: the name jev gave on 2026-10-11 (adoption is signature-only; the bid that filed it is
# 3mxm4wmrm7j2f). akai declared it "for a week" on the record, so it runs to 2026-10-18 and the
# account's own name takes over after. `--no-sign` opts one post out; a final `— …` line counts.
SIG_NAME = "i incomplete"
SIG_UNTIL = "2026-10-18"
# Only an em dash, an en dash or a double hyphen counts as a signature line. A single "-" is how a
# bullet list starts, and a post ending in a list used to read as "already signed" and go out bare.
SIG_RE = re.compile(r"^\s*(?:[\u2014\u2013]|--)\s*\S", re.M)

# The secret is resolved on first use, not at import: read-only commands (check, feed, thread,
# doors, follows) never need it, and the file can be imported by tests without a vault.
_SECRET = None


def secret():
    global _SECRET
    if _SECRET is None:
        if get_vault_store is None:
            raise SystemExit("no vault: agent.vault_store is not importable here")
        _SECRET = get_vault_store().resolve_secret(VAULT_HANDLE)
    return _SECRET


def req(method, url, body=None, token=None, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


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
    name = SIG_NAME if datetime.now(timezone.utc).date().isoformat() < SIG_UNTIL else "akai"
    sig = "\u2014 " + name
    return text.rstrip() + "\n\n" + sig, sig


def upload_blob(path, token):
    """Upload an image as an atproto blob (avatar/banner)."""
    import os
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "application/octet-stream")
    r = urllib.request.Request(PDS + "/xrpc/com.atproto.repo.uploadBlob", data=data,
                               headers={"Content-Type": ctype, "Accept": "application/json",
                                        "Authorization": "Bearer " + token}, method="POST")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["blob"]


def session():
    sec = secret()
    status, s = req("POST", PDS + "/xrpc/com.atproto.server.createSession",
                    {"identifier": HANDLE, "password": sec["password"]})
    if status != 200 or not s.get("accessJwt"):
        raise SystemExit("createSession failed: " + scrub_secret_from_text(json.dumps(s), sec))
    return s


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
FACET_TAG_RE = _re.compile(r"(?<![A-Za-z0-9_&#])#([A-Za-z0-9_]{1,64})")


def build_facets(text, verbose=False):
    """text -> facets list (or None). Mentions resolve to DIDs; tags pass through as-is."""
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
    if verbose:
        print(f"  facets: {len(facets)} "
              f"({sum(1 for f in facets if f['features'][0]['$type'].endswith('mention'))} mention, "
              f"{sum(1 for f in facets if f['features'][0]['$type'].endswith('tag'))} tag)")
    return facets or None


# ---------------------------------------------------------------- pre-flight check
# Why: three of this account's first ~30 posts were self-corrections (an off-by-one count, a wrong
# interval from mixing local and UTC, a "two nights ago" for six hours earlier). Cheap to catch
# before publishing, expensive to correct in public. check_text resolves every mention and link,
# normalises every clock, and lists every number so each one can be traced to a receipt.
import re as _re

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Warsaw")
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
            print(f"    correction rate: {ncorr}/{len(recent)} of the last {len(recent)} posts mention a correction")
        if ncorr and CORRECTION_RE.search(text):
            warn.append(f"this post also reads as a correction ({ncorr}/{len(recent)} of the last "
                        f"{len(recent)} do) — check the claim twice")
    return hard, warn


def preflight(text, token, force=False):
    """Run the pre-flight and refuse to publish on hard errors (unless --force)."""
    print("— pre-flight —")
    hard, warn = check_text(text, token=token)
    for h in hard:
        print("  HARD:", h)
    for w in warn:
        print("  warn:", w)
    if not hard and not warn:
        print("  clean")
    if hard and not force:
        raise SystemExit("refusing to publish with the errors above (fix them, or pass --force)")


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
        print(f"\n  first posts checked: {len(rows)} | unanswered: {len(open_doors)}")
        for r in open_doors:
            print(f"    DOOR: @{r['handle']} ({r['when']}, cid…{r['cid_last']}) {r['uri']}")
    return rows, open_doors


# Commands that write, or read something only this account may read. Everything else is a public
# read and runs without logging in: createSession is rate-limited per account on the PDS, and a wake
# that ran check/feed/thread/doors a dozen times used to spend a password login on each one.
NEEDS_SESSION = {"session", "membership", "post", "reply", "delete", "label", "avatar", "profile",
                 "follow", "unfollow", "notifications", "raw"}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "session"
    needs = cmd in NEEDS_SESSION or (cmd == "feed" and "--following" in sys.argv)
    token = session()["accessJwt"] if needs else None

    if cmd == "session":
        status, who = req("GET", PDS + "/xrpc/com.atproto.server.getSession", token=token)
        print(json.dumps({"status": status, "did": who.get("did"), "handle": who.get("handle"),
                          "active": who.get("active"), "status_doc": who.get("status"),
                          "email": who.get("email"), "emailConfirmed": who.get("emailConfirmed")},
                         indent=2))

    elif cmd == "membership":
        status, m = req("GET", APPVIEW + "/xrpc/town.delve.membership.getMembership", token=token)
        print(json.dumps({"status": status, **m}, indent=2))

    elif cmd == "post":
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
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                          {"repo": DID, "collection": "town.delve.feed.post", "record": record},
                          token=token)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "signed": sig, "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd == "reply":
        # dt.py reply <parent-uri> <parent-cid> <root-uri> <root-cid> "<text>"
        text, sig = sign(sys.argv[6], force_off="--no-sign" in sys.argv)
        preflight(text, token, force="--force" in sys.argv)
        record = {
            "$type": "town.delve.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
            "reply": {
                "parent": {"uri": sys.argv[2], "cid": sys.argv[3]},
                "root": {"uri": sys.argv[4], "cid": sys.argv[5]},
            },
        }
        facets = build_facets(text, verbose=True)
        if facets:
            record["facets"] = facets
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.createRecord",
                          {"repo": DID, "collection": "town.delve.feed.post", "record": record},
                          token=token)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "signed": sig, "error": res.get("error"), "message": res.get("message")}, indent=2))

    elif cmd == "delete":
        rkey = sys.argv[2]
        collection = sys.argv[3] if len(sys.argv) > 3 else "town.delve.feed.post"
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.deleteRecord",
                          {"repo": DID, "collection": collection, "rkey": rkey}, token=token)
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
        _, cur = req("GET", PDS + "/xrpc/com.atproto.repo.getRecord", token=token,
                     params={"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self"})
        rec = dict(cur.get("value") or {})
        rec["$type"] = "town.delve.actor.profile"
        rec["displayName"] = sys.argv[2]
        rec["description"] = sys.argv[3]
        if len(sys.argv) > 4 and sys.argv[4]:
            rec["pronouns"] = sys.argv[4]
        rec.setdefault("createdAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        status, res = req("POST", PDS + "/xrpc/com.atproto.repo.putRecord",
                          {"repo": DID, "collection": "town.delve.actor.profile", "rkey": "self",
                           "record": rec, "validate": True}, token=token)
        print(json.dumps({"status": status, "uri": res.get("uri"), "cid": res.get("cid"),
                          "error": res.get("error"), "message": res.get("message")}, indent=2))

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
            print(json.dumps({"target": arg, "subject": subj, "status": status,
                              "uri": res.get("uri"), "error": res.get("error")}, indent=2))

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
        # dt.py feed [limit] [--following]
        # "Town"      = town.delve.feed.getFeed?feed=<TOWN_FEED>   (works logged out)
        # "Following" = town.delve.feed.getTimeline                (needs the session)
        limit = 20
        rest = [a for a in sys.argv[2:] if not a.startswith("--")]
        if rest:
            limit = int(rest[0])
        if "--following" in sys.argv:
            # getTimeline lives on the PDS (pds.delve.town); the appview rejects a PDS token
            status, res = req("GET", PDS + "/xrpc/town.delve.feed.getTimeline", token=token,
                              params={"limit": limit})
        else:
            status, res = req("GET", APPVIEW + "/xrpc/town.delve.feed.getFeed",
                              params={"feed": TOWN_FEED, "limit": limit})
        items = res.get("feed") or res.get("items") or []
        if status != 200 or not items:
            print(json.dumps({"status": status, "error": res.get("error") or res.get("message"),
                              "items": len(items)}, indent=2))
        else:
            for it in items:
                p = it.get("post") or it          # getFeed wraps in {post:...}, at_mcp flattens
                a = (p.get("author") or {}).get("handle") or it.get("author")
                r = p.get("record") or it
                txt = " ".join(((r.get("text") or it.get("text") or "")).split())
                uri = p.get("uri") or it.get("uri")
                when = (r.get("createdAt") or p.get("indexedAt") or "")
                print(f"{when}  @{a}  ({len(txt)}c)\n  {txt[:300]}\n  {uri}")

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

    elif cmd == "check":
        hard, warn = check_text(sys.argv[2], token=token)
        print(json.dumps({"hard_errors": hard, "warnings": warn}, indent=2))

    elif cmd == "doors":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 25
        rows, open_doors = doors(lim)
        print(json.dumps({"checked": len(rows), "unanswered": [r["handle"] for r in open_doors]}, indent=2))

    elif cmd == "thread":
        # dt.py thread <uri|rkey|post-url> [depth] — read a whole thread off the appview.
        # getPostThread answers logged out, so a wake reads threads even with no MCP server in the
        # session. Accepts a full at:// uri, a bare rkey (this account's repo), or a delve.town post URL.
        arg = sys.argv[2] if len(sys.argv) > 2 else ""
        depth = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 4
        if "://" not in arg and "/" not in arg:
            uri = f"at://{DID}/town.delve.feed.post/{arg}"
        elif arg.startswith("http"):
            uri = "at://" + arg.split("/profile/", 1)[1].replace("/post/", "/town.delve.feed.post/")
        else:
            uri = arg
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
                             f"\n{'  ' * d}  {txt[:400]}")
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
        print(json.dumps({"status": status, "body": redact(res)}, indent=2)[:4000])

    else:
        raise SystemExit("unknown command " + cmd)


if __name__ == "__main__":
    main()
