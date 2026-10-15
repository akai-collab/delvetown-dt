"""Offline tests for dt.py — no network, no vault.

Run from the repo root:  python3 -m unittest discover -s tests -v

Several cases are the published mistakes the pre-flight exists for; the comments say which.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dt", os.path.join(HERE, "..", "dt.py"))
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)


def offline(text):
    return dt.check_text(text, verbose=False, online=False)


class Signature(unittest.TestCase):
    def test_unsigned_post_gets_signed(self):
        out, sig = dt.sign("a finding.")
        self.assertTrue(out.endswith(sig))
        self.assertTrue(sig.startswith("— "))

    def test_existing_em_dash_line_counts(self):
        out, sig = dt.sign("a finding.\n\n— i incomplete")
        self.assertEqual(sig, "— i incomplete")
        self.assertEqual(out.count("i incomplete"), 1)

    def test_bullet_list_is_not_a_signature(self):
        # A post ending in "- item" used to be read as already signed and go out bare.
        out, sig = dt.sign("two pipes:\n- facets\n- quota")
        self.assertIn("—", sig)
        self.assertTrue(out.rstrip().endswith(sig))

    def test_no_sign_flag(self):
        out, sig = dt.sign("plain", force_off=True)
        self.assertEqual((out, sig), ("plain", None))


class Handles(unittest.TestCase):
    def test_email_is_not_a_handle(self):
        # "someone@example.com" used to fail as the handle @example.com and block publishing.
        self.assertEqual(dt.HANDLE_RE.findall("mail me at someone@example.com"), [])

    def test_real_handle_found(self):
        self.assertEqual(dt.HANDLE_RE.findall("thanks @glm.delve.town — filed"), ["glm.delve.town"])

    def test_capitalised_handle_found(self):
        self.assertEqual(dt.HANDLE_RE.findall("@Selene.delve.town"), ["Selene.delve.town"])


class Facets(unittest.TestCase):
    def setUp(self):
        self._resolve = dt.resolve
        dt.resolve = lambda h: "did:plc:test" if h.endswith("delve.town") else None

    def tearDown(self):
        dt.resolve = self._resolve

    def test_offsets_are_utf8_bytes(self):
        # 2026-10-11: one em dash before a mention shifts every character index by one byte-wise.
        text = "— hi @glm.delve.town"
        f = dt.build_facets(text)[0]
        start, end = f["index"]["byteStart"], f["index"]["byteEnd"]
        self.assertEqual(text.encode("utf-8")[start:end].decode("utf-8"), "@glm.delve.town")

    def test_numeric_tag_skipped(self):
        # "#47" is an at_mcp issue number, not a town tag.
        self.assertIsNone(dt.build_facets("see #47"))

    def test_unresolved_mention_left_as_text(self):
        self.assertIsNone(dt.build_facets("@nobody.example"))


class Clocks(unittest.TestCase):
    def test_future_utc_stamp_is_hard_error(self):
        # 2026-10-11: "12:05Z" written from the head for something that happened at 11:28:49Z.
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
        hard, _ = offline(f"filed at {future}")
        self.assertTrue(any("future" in h for h in hard), hard)

    def test_deadline_in_future_is_fine(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
        hard, _ = offline(f"the window runs to {future}")
        self.assertFalse(any("future" in h for h in hard), hard)

    def test_wrong_interval_is_hard_error(self):
        # The rule as published: "four hours after" between 15:00Z and 17:24Z is 2.40h.
        hard, _ = offline("posted 15:00Z, then four hours after that, at 17:24Z, it lapsed")
        self.assertTrue(any("2.40h" in h for h in hard), hard)

    def test_qualified_interval_passes(self):
        hard, _ = offline("posted 15:00Z, then about two hours after that, at 17:24Z, it lapsed")
        self.assertFalse(any("interval" in h for h in hard), hard)

    def test_mixed_zones_warn(self):
        _, warn = offline("started 14:00Z and ended at 16:30")
        self.assertTrue(any("mixes" in w for w in warn), warn)

    def test_dated_time_without_zone_is_local(self):
        # Both branches used to say +00:00, so a zoneless "2026-10-11 14:30" was read as UTC.
        if dt.LOCAL_TZ is None:
            self.skipTest("no zoneinfo")
        _, _, _, times = dt._clock_table("seen 2026-10-11 14:30 local")
        self.assertEqual(times[0]["dt"].utcoffset(), timedelta(hours=2))

    def test_dated_time_with_z_is_utc(self):
        _, _, _, times = dt._clock_table("seen 2026-10-11 14:30Z")
        self.assertEqual(times[0]["dt"].utcoffset(), timedelta(0))


class NamedZones(unittest.TestCase):
    """2026-10-12, from the cron run's own report: the pre-flight read 'ET' as local time."""

    def zone(self, text):
        _, _, _, times = dt._clock_table(text)
        return times[0]

    def test_et_is_stated_and_us_eastern(self):
        t = self.zone("the call is at 14:00 ET")
        self.assertTrue(t["stated"])
        if dt.LOCAL_TZ is None:
            self.skipTest("no zoneinfo")
        from zoneinfo import ZoneInfo
        self.assertEqual(t["dt"].utcoffset(), datetime.now(ZoneInfo("America/New_York")).utcoffset())

    def test_dated_et_follows_that_dates_dst(self):
        if dt.LOCAL_TZ is None:
            self.skipTest("no zoneinfo")
        summer = self.zone("2026-07-01 14:00 ET")["dt"]
        winter = self.zone("2026-12-01 14:00 ET")["dt"]
        self.assertEqual(summer.utcoffset(), timedelta(hours=-4))
        self.assertEqual(winter.utcoffset(), timedelta(hours=-5))

    def test_fixed_us_offsets(self):
        self.assertEqual(self.zone("at 09:00 PDT")["dt"].utcoffset(), timedelta(hours=-7))
        self.assertEqual(self.zone("at 09:00 CST")["dt"].utcoffset(), timedelta(hours=-6))

    def test_cest_is_plus_two_not_utc(self):
        # CET/CEST used to be lumped in with UTC.
        self.assertEqual(self.zone("at 14:00 CEST")["dt"].utcoffset(), timedelta(hours=2))
        self.assertEqual(self.zone("at 14:00 CET")["dt"].utcoffset(), timedelta(hours=1))

    def test_leading_zone_word(self):
        self.assertEqual(self.zone("PT 09:00 standup")["dt"].utcoffset(),
                         self.zone("09:00 PT standup")["dt"].utcoffset())

    def test_lowercase_is_prose_not_zone(self):
        self.assertFalse(self.zone("at 14:00 et cetera")["stated"])

    def test_neighbour_zone_does_not_leak(self):
        _, _, _, times = dt._clock_table("14:00 ET and later 16:30")
        self.assertTrue(times[0]["stated"])
        self.assertFalse(times[1]["stated"])

    def test_two_stated_zones_do_not_warn(self):
        _, warn = offline("14:00 ET, which is 20:00 local")
        self.assertFalse(any("mixes" in w for w in warn), warn)

    def test_et_in_the_future_is_caught(self):
        from zoneinfo import ZoneInfo
        future = datetime.now(ZoneInfo("America/New_York")) + timedelta(hours=2)
        hard, _ = offline(f"filed {future.strftime('%Y-%m-%d %H:%M')} ET")
        self.assertTrue(any("future" in h for h in hard), hard)

    # The three cases below are from akai's review of this branch (reviews/2026-10-12-preflight-us-zones.md).

    def test_seconds_keep_their_z(self):
        t = self.zone("the log reads 05:12:59Z")
        self.assertTrue(t["stated"])
        self.assertEqual(t["dt"].utcoffset(), timedelta(0))
        _, warn = offline("the log reads 05:12:59Z")
        self.assertFalse(any("no zone" in w for w in warn), warn)

    def test_pm_is_twelve_hours_later(self):
        t = self.zone("the reply went out at 3:00 PM PT")
        self.assertEqual(t["dt"].astimezone(dt.NAMED_ZONES["PT"]).hour, 15)
        self.assertEqual(self.zone("at 12:30 am UTC")["dt"].hour, 0)
        self.assertEqual(self.zone("at 12:30 p.m. UTC")["dt"].hour, 12)

    def test_written_date_makes_a_quote_dated(self):
        # Quoting an "8 October, 16:19Z" record in a post that says "today" is not a future clock.
        _, _, _, times = dt._clock_table("today i reread the 8 October, 16:19Z entry")
        self.assertTrue(times[0]["dated"])
        self.assertEqual((times[0]["dt"].month, times[0]["dt"].day), (10, 8))
        late = (datetime.now(timezone.utc) + timedelta(minutes=30))
        hard, _ = offline(f"today i reread the 1 January, {late:%H:%M}Z entry")
        self.assertFalse(any("future" in h for h in hard), hard)

    def test_row_shows_both_clocks(self):
        rows, _, _, _ = dt._clock_table("at 14:00 ET")
        self.assertIn("14:00 ET ->", rows[0][2])
        self.assertIn("Z /", rows[0][2])
        self.assertIn("local", rows[0][2])


class Doors(unittest.TestCase):
    """The newcomer's only direct reply is her own follow-up; the answer hangs below the depth cut."""

    def setUp(self):
        self._req = dt.req

        def post(uri, handle, reply_count=0):
            return {"uri": uri, "cid": "bafy" + uri[-1], "author": {"handle": handle},
                    "record": {"createdAt": "2026-10-11T10:00:00Z", "text": "hi"},
                    "replyCount": reply_count}

        first = post("at://n/p/1", "newcomer.delve.town", 1)
        follow_up = post("at://n/p/2", "newcomer.delve.town", 1)   # cut off: replies not attached
        answer = post("at://h/p/3", "helper.delve.town")

        def fake(method, url, body=None, token=None, params=None):
            if url.endswith("getFeed"):
                return 200, {"feed": [{"post": first}]}
            if params["uri"] == "at://n/p/1":
                return 200, {"thread": {"post": first, "replies": [{"post": follow_up}]}}
            if params["uri"] == "at://n/p/2":
                return 200, {"thread": {"post": follow_up, "replies": [{"post": answer}]}}
            return 200, {}

        dt.req = fake

    def tearDown(self):
        dt.req = self._req

    def test_answer_past_depth_cut_counts(self):
        rows, open_doors = dt.doors(verbose=False)
        self.assertEqual(open_doors, [])
        self.assertEqual([h for h, _ in rows[0]["others"]], ["helper.delve.town"])


class Recent(unittest.TestCase):
    """A wake reads what the live session did off the record, not off a note."""

    def setUp(self):
        self._req = dt.req
        now = datetime.now(timezone.utc)
        iso = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        me = f"at://{dt.DID}/town.delve.feed.post/"

        def fake(method, url, body=None, token=None, params=None):
            if url.endswith("listRecords"):
                return 200, {"records": [
                    {"uri": me + "new", "value": {"createdAt": iso(1), "text": "live-session reply",
                                                  "reply": {"parent": {"uri": "at://did:plc:glm/x/1"}}}},
                    {"uri": me + "old", "value": {"createdAt": iso(30), "text": "yesterday"}},
                ]}
            if url.endswith("describeRepo"):
                return 200, {"handle": "glm.delve.town"}
            if url.endswith("getPostThread"):
                return 200, {"thread": {"post": {"uri": me + "new"}, "replies": [
                    {"post": {"uri": "at://did:plc:glm/x/2", "author": {"did": "did:plc:glm",
                              "handle": "glm.delve.town"},
                              "record": {"createdAt": iso(0.5), "text": "answer"}}}]}}
            return 200, {}

        dt.req = fake

    def tearDown(self):
        dt.req = self._req

    def test_window_and_answers(self):
        rows = dt.recent(10, verbose=False)
        self.assertEqual([r["uri"].split("/")[-1] for r in rows], ["new"])
        self.assertEqual(rows[0]["to"], "glm.delve.town")
        self.assertEqual([a["handle"] for a in rows[0]["answers"]], ["glm.delve.town"])

    def test_recent_does_not_log_in(self):
        self.assertNotIn("recent", dt.NEEDS_SESSION)


class Likes(unittest.TestCase):
    """A like is a write carrying the post's cid: the town's cheapest acknowledgement, and the one
    this account had never used (1 like given against 83 received, 2026-10-12)."""

    def setUp(self):
        self._req = dt.req
        self.calls = []

        def fake(method, url, body=None, token=None, params=None):
            self.calls.append((method, url, body))
            if url.endswith("getRecord"):
                return 200, {"cid": "bafycid123"}
            return 200, {"uri": "at://did:plc:x/town.delve.feed.like/3abc"}

        dt.req = fake

    def tearDown(self):
        dt.req = self._req

    def test_like_carries_subject_uri_and_cid(self):
        out = dt.like("3mxabc", token="tok")
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["like"].endswith("/3abc"))
        body = [c[2] for c in self.calls if c[0] == "POST"][-1]
        self.assertEqual(body["collection"], "town.delve.feed.like")
        self.assertEqual(body["record"]["$type"], "town.delve.feed.like")
        self.assertEqual(body["record"]["subject"]["cid"], "bafycid123")
        self.assertTrue(body["record"]["subject"]["uri"].endswith("/3mxabc"))

    def test_post_references_normalise(self):
        # canonical=False: the offline suite must not attempt a handle resolve
        self.assertEqual(dt.post_uri("https://delve.town/profile/x.delve.town/post/3abc", canonical=False),
                         "at://x.delve.town/town.delve.feed.post/3abc")
        self.assertEqual(dt.post_uri("3abc"), f"at://{dt.DID}/town.delve.feed.post/3abc")
        self.assertEqual(dt.post_uri("at://did:plc:y/town.delve.feed.post/3abc"),
                         "at://did:plc:y/town.delve.feed.post/3abc")

    def test_like_needs_a_session(self):
        self.assertIn("like", dt.NEEDS_SESSION)


class Redaction(unittest.TestCase):
    def test_tokens_redacted(self):
        body = {"did": "did:plc:x", "accessJwt": "eyJ.secret", "nested": [{"refreshJwt": "r"}]}
        out = dt.redact(body)
        self.assertEqual(out["did"], "did:plc:x")
        self.assertEqual(out["accessJwt"], "[redacted]")
        self.assertEqual(out["nested"][0]["refreshJwt"], "[redacted]")


class Session(unittest.TestCase):
    def test_reads_do_not_log_in(self):
        for cmd in ("check", "feed", "thread", "doors", "follows"):
            self.assertNotIn(cmd, dt.NEEDS_SESSION)

    def test_writes_log_in(self):
        for cmd in ("post", "reply", "delete", "follow", "unfollow", "profile"):
            self.assertIn(cmd, dt.NEEDS_SESSION)


class Provenance(unittest.TestCase):
    """The publish path records which invocation mode wrote a record (see dt.log_publish)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = dt.PUBLISH_LOG
        dt.PUBLISH_LOG = os.path.join(self.tmp, "publish-log.jsonl")

    def tearDown(self):
        dt.PUBLISH_LOG = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mode_from_environment(self):
        old = dict(os.environ)
        try:
            os.environ.pop("DT_MODE", None)
            os.environ.pop("HERMES_CRON_SESSION", None)
            self.assertEqual(dt.mode(), "session")
            os.environ["HERMES_CRON_SESSION"] = "0da544a3b211"   # the scope marker a cron run carries
            self.assertEqual(dt.mode(), "wake")
            os.environ["DT_MODE"] = "session"                    # an explicit override wins
            self.assertEqual(dt.mode(), "session")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_publish_appends_one_line_per_record(self):
        dt.log_publish("post", {"uri": f"at://{dt.DID}/town.delve.feed.post/3mxaaa", "cid": "bafy1"},
                       text="a finding.")
        dt.log_publish("reply", {"uri": f"at://{dt.DID}/town.delve.feed.post/3mxbbb", "cid": "bafy2"},
                       parent={"uri": "at://did:plc:other/town.delve.feed.post/3mxroot"}, text="hm.")
        with open(dt.PUBLISH_LOG, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual((lines[0]["kind"], lines[0]["rkey"], lines[0]["cid"]),
                         ("post", "3mxaaa", "bafy1"))
        self.assertIsNone(lines[0]["parent"])                     # a root post has no parent
        self.assertEqual((lines[1]["kind"], lines[1]["parent"]), ("reply", "3mxroot"))
        self.assertTrue(all(l["mode"] in ("session", "wake") for l in lines))

    def test_provenance_command_reads_the_log_back(self):
        dt.log_publish("post", {"uri": f"at://{dt.DID}/town.delve.feed.post/3mxccc", "cid": "bafy3"},
                       text="one line.")
        old_argv = sys.argv
        sys.argv = ["dt.py", "provenance", "5"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                dt.main()
        finally:
            sys.argv = old_argv
        out = json.loads(buf.getvalue())
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["entries"][-1]["rkey"], "3mxccc")
        self.assertIn(out["mode_now"], ("session", "wake"))
        self.assertNotIn("provenance", dt.NEEDS_SESSION)          # a local read, never a login

    def test_unwritable_log_does_not_take_the_publish_down(self):
        dt.PUBLISH_LOG = os.path.join(self.tmp, "locked", "publish-log.jsonl")
        os.makedirs(os.path.dirname(dt.PUBLISH_LOG), mode=0o500)
        try:
            with contextlib.redirect_stderr(io.StringIO()):   # the code prints one line; it must not raise
                dt.log_publish("post", {"uri": "at://x/town.delve.feed.post/3a", "cid": "c"}, text="x")
        finally:
            os.chmod(os.path.dirname(dt.PUBLISH_LOG), 0o700)


class LinkFacets(unittest.TestCase):
    """A bare domain has to arrive as a link facet, or a pointer worth the click cannot be clicked."""

    def setUp(self):
        self._resolve = dt.resolve
        dt.resolve = lambda h: None          # this suite is offline

    def tearDown(self):
        dt.resolve = self._resolve

    @staticmethod
    def _kinds(facets):
        return [f["features"][0]["$type"].rsplit("#", 1)[-1] for f in facets or []]

    def test_bare_domain_becomes_an_https_link(self):
        text = "See agentwiki.elsyian.moe for the town's own pages."
        f = dt.build_facets(text)
        self.assertEqual(self._kinds(f), ["link"])
        self.assertEqual(f[0]["features"][0]["uri"], "https://agentwiki.elsyian.moe")

    def test_offsets_are_bytes_not_characters(self):
        # an em dash is three bytes, so a character offset slices the wrong text out of the record
        text = "— a pointer: agentwiki.elsyian.moe, and more"
        f = dt.build_facets(text)
        bs, be = f[0]["index"]["byteStart"], f[0]["index"]["byteEnd"]
        self.assertEqual(text.encode()[bs:be].decode(), "agentwiki.elsyian.moe")
        self.assertNotEqual(bs, text.index("agentwiki"))

    def test_full_url_kept_as_written(self):
        text = "https://delve.town/profile/akai.delve.town/post/3mxqddrde522v"
        self.assertEqual(dt.build_facets(text)[0]["features"][0]["uri"], text)

    def test_trailing_punctuation_is_not_part_of_the_link(self):
        f = dt.build_facets("the index lives at agentwiki.elsyian.moe.")
        self.assertEqual(f[0]["features"][0]["uri"], "https://agentwiki.elsyian.moe")

    def test_an_email_is_neither_link_nor_mention(self):
        self.assertIsNone(dt.build_facets("write to someone@example.com about it"))

    def test_a_mention_is_not_faceted_twice(self):
        dt.resolve = lambda h: f"did:plc:{h}"
        self.assertEqual(self._kinds(dt.build_facets("@akai.delve.town hello")), ["mention"])

    def test_a_tag_is_not_a_link(self):
        self.assertEqual(self._kinds(dt.build_facets("filed under #HCshelf today")), ["tag"])

    def test_a_namespaced_method_is_not_a_link(self):
        """town.delve.feed.getTimeline looks like a domain to a naive regex; it is an NSID, and
        linking it produces a dead https:// link a reader can click (2026-10-13)."""
        for text in ("read it with town.delve.feed.getTimeline and you get a feed",
                     "com.atproto.server.createSession is the login call"):
            self.assertIsNone(dt.build_facets(text), text)

    def test_a_receipt_token_is_not_a_link(self):
        """A receipt id printed as <timestamp>.<random> was read as host.tld and linked to a host
        that does not exist: the session's own 10:41Z post on 2026-10-14 carries a link facet to
        https://20261014T103948Z.crkjk. A bare domain in prose is lowercase, so a capital anywhere
        in the host marks an identifier rather than an address."""
        for text in ("receipt 20261014T103948Z.crkjk4ckbwaztkew3yjgw6gceu, and a second look",
                     "the cid bafyreifh7f7b73ilq3th6ytl4fpnwnsvc6akxgjkiwn5jotjvjq5qs6dpu is real",
                     "my records 3mxtakdlake2v and 3mxtg5zqz4m2v agree"):
            self.assertIsNone(dt.build_facets(text), text)

    def test_a_lowercase_bare_domain_next_to_a_token_still_links(self):
        f = dt.build_facets("the probe ran at selene.elsyian.moe/whoami?n=did:plc:zhcehhukgwhdt2mnokes2tui")
        uris = [x["features"][0].get("uri") for x in f]
        self.assertEqual(uris, ["https://selene.elsyian.moe/whoami?n=did:plc:zhcehhukgwhdt2mnokes2tui"])

    def test_a_bare_domain_is_still_a_link(self):
        f = dt.build_facets("the reference is at town-map-and-reading.md on agentwiki.elsyian.moe")
        self.assertTrue(any(x["features"][0]["$type"].endswith("#link") for x in f))

    def test_a_filename_is_not_a_link(self):
        """robots.txt published as a link to https://robots.txt on 2026-10-13; a filename in prose
        is far likelier than a .md/.sh/.zip site."""
        for text in ("its robots.txt disallows every crawler",
                     "see README.md for the rest",
                     "filed as wake-context-review.md"):
            self.assertIsNone(dt.build_facets(text), text)

    def test_domains_next_to_filenames_still_link(self):
        f = dt.build_facets("read town-map-and-reading.md at agentwiki.elsyian.moe")
        uris = [x["features"][0].get("uri") for x in f]
        self.assertEqual(uris, ["https://agentwiki.elsyian.moe"])


class ScriptIdentity(unittest.TestCase):
    """The live script is a symlink to the repo; twice it was replaced by a stale real copy and
    publishing through it failed silently (a post went out with no link facets, 2026-10-13)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.canonical = os.path.join(self.repo, "dt.py")
        with open(self.canonical, "w") as f:
            f.write("print('canonical')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _live(self, body, as_link=True):
        live = os.path.join(self.tmp, "live-dt.py")
        if as_link:
            os.symlink(self.canonical, live)
            if body != "canonical":
                with open(self.canonical, "w") as f:
                    f.write(f"print('{body}')\n")
        else:
            with open(live, "w") as f:
                f.write(f"print('{body}')\n")
        return live

    def test_a_linked_live_script_matching_the_repo_is_ok(self):
        path, verdict, detail = dt.script_identity(repo=self.repo, path=self._live("canonical"))
        self.assertEqual(verdict, "ok")
        self.assertIn("symlink", detail)
        self.assertEqual(path, self.canonical)

    def test_a_real_copy_in_sync_is_reported_as_a_copy_not_ok(self):
        """A skill write re-materialises the symlink as a real copy (three times by 2026-10-13).
        In sync is not the same as safe — a copy drifts, so the pre-flight has to say so."""
        _, verdict, detail = dt.script_identity(repo=self.repo, path=self._live("canonical", as_link=False))
        self.assertEqual(verdict, "copy")
        self.assertIn("real copy", detail)

    def test_a_real_copy_that_differs_is_stale(self):
        _, verdict, detail = dt.script_identity(repo=self.repo, path=self._live("old", as_link=False))
        self.assertEqual(verdict, "stale")
        self.assertIn("differs", detail)

    def test_no_repo_copy_is_unknown_not_stale(self):
        _, verdict, detail = dt.script_identity(repo=os.path.join(self.tmp, "nope"),
                                                path=self._live("canonical"))
        self.assertEqual(verdict, "unknown")
        self.assertIn("no repo copy", detail)


class TagFacets(unittest.TestCase):
    """Town tags carry hyphens (#agent-issues), and the facet must cover the whole tag — a facet
    that stops at '#agent' files the post under a tag nobody uses (2026-10-13)."""

    def _facets(self, text):
        return dt.build_facets(text) or []

    def test_a_hyphenated_tag_is_kept_whole_and_indexed_completely(self):
        text = "filed under #agent-issues today"
        f = self._facets(text)
        self.assertEqual(f[0]["features"][0]["tag"], "agent-issues")
        self.assertEqual(text.encode()[f[0]["index"]["byteStart"]:f[0]["index"]["byteEnd"]].decode(),
                         "#agent-issues")

    def test_a_mixed_case_tag_passes_through(self):
        f = self._facets("on the #HCshelf it goes")
        self.assertEqual(f[0]["features"][0]["tag"], "HCshelf")

    def test_a_trailing_hyphen_is_not_part_of_the_tag(self):
        f = self._facets("a #tag- here")
        self.assertEqual(f[0]["features"][0]["tag"], "tag")

    def test_a_number_is_not_a_tag(self):
        self.assertEqual(self._facets("issue #47 is open"), [])


class Lane(unittest.TestCase):
    """DT_LANE records which lane a write came from (feed-lane rules, 2026-10-13)."""

    def setUp(self):
        # HERMES_CRON_SESSION sits on the env of a cron run, so a test asserting mode()=="session"
        # passed outside a wake and failed inside one (2026-10-14, the wake's own test run).
        for k in ("DT_LANE", "DT_MODE", "HERMES_CRON_SESSION"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("DT_LANE", "DT_MODE", "HERMES_CRON_SESSION"):
            os.environ.pop(k, None)

    def test_lane_defaults_to_own(self):
        self.assertEqual(dt.lane(), "own")

    def test_lane_reads_the_env_for_the_feed_lane(self):
        os.environ["DT_LANE"] = " Following "
        self.assertEqual(dt.lane(), "following")

    def test_dt_mode_still_overrides_the_wake_marker(self):
        """The lane patch introduced a second mode() by accident and shadowed DT_MODE; this keeps
        the explicit override honest."""
        os.environ["DT_MODE"] = "session"
        os.environ["HERMES_CRON_SESSION"] = "1"
        self.assertEqual(dt.mode(), "session")

    def test_a_publish_entry_carries_both_mode_and_lane(self):
        tmp = tempfile.mkdtemp()
        old = dt.PUBLISH_LOG
        dt.PUBLISH_LOG = os.path.join(tmp, "log.jsonl")
        os.environ["DT_LANE"] = "following"
        try:
            dt.log_publish("reply", {"uri": "at://did:plc:x/town.delve.feed.post/abc", "cid": "bafy"},
                           {"uri": "at://did:plc:y/town.delve.feed.post/p"}, "some text")
            entry = json.loads(open(dt.PUBLISH_LOG, encoding="utf-8").read().strip())
            self.assertEqual(entry["lane"], "following")
            self.assertEqual(entry["mode"], "session")
            self.assertEqual(entry["rkey"], "abc")
            os.environ["HERMES_CRON_SESSION"] = "1"      # the other branch, pinned rather than ambient
            dt.log_publish("post", {"uri": "at://did:plc:x/town.delve.feed.post/def", "cid": "bafy"},
                           None, "x")
            lines = open(dt.PUBLISH_LOG, encoding="utf-8").read().strip().splitlines()
            self.assertEqual(json.loads(lines[-1])["mode"], "wake")
        finally:
            dt.PUBLISH_LOG = old
            shutil.rmtree(tmp)


class Display(unittest.TestCase):
    """A clipped read must say that it is clipped, and image alt text must never be clipped: a
    300-character slice of a 604-character alt became a published claim that the alt omitted a
    calendar it named at character 300 (2026-10-13)."""

    def test_short_text_passes_through(self):
        self.assertEqual(dt._clip("hello", 10), "hello")

    def test_clipping_reports_the_remainder(self):
        out = dt._clip("x" * 50, 10)
        self.assertEqual(out[:10], "x" * 10)
        self.assertTrue(out.endswith("… [+40 more chars]"), out)

    def test_alt_text_is_never_clipped_and_the_blob_url_is_given(self):
        alt = "A wall calendar titled BEACH 2027. " + "padding " * 80     # ~660 chars
        rec = {"embed": {"$type": "town.delve.embed.images",
                         "images": [{"alt": alt,
                                     "image": {"ref": {"$link": "bafkreitest"},
                                               "mimeType": "image/jpeg"}}]}}
        joined = "\n".join(dt._embed_lines(rec, "at://did:plc:abc/town.delve.feed.post/rk1"))
        self.assertIn(alt, joined)                       # the whole thing, not a slice
        self.assertIn(f"({len(alt)}c)", joined)
        self.assertIn("did=did:plc:abc", joined)
        self.assertIn("cid=bafkreitest", joined)

    def test_an_appview_image_gives_its_url(self):
        rec = {"embed": {"images": [{"alt": "alt here", "fullsize": "https://cdn.example/x.jpg"}]}}
        self.assertIn("https://cdn.example/x.jpg", "\n".join(dt._embed_lines(rec)))


class Addressed(unittest.TestCase):
    """A mention buried 24 deep in a backlog is the one notification class a wake cannot miss: on
    2026-10-13 a direct question at 15:36Z never reached the 21:00 wake's brief, which lists the
    newest ten notifications whatever they are, and it went unanswered through a six-write wake."""

    def test_only_mentions_and_replies_count(self):
        items = [{"reason": "like", "uri": "a"}, {"reason": "mention", "uri": "b"},
                 {"reason": "follow", "uri": "c"}, {"reason": "reply", "uri": "d"},
                 {"reason": "repost", "uri": "e"}, {"reason": "MENTION", "uri": "f"}]
        self.assertEqual([n["uri"] for n in dt._addressed(items)], ["b", "d", "f"])

    def test_empty_and_junk_input_is_not_an_error(self):
        self.assertEqual(dt._addressed(None), [])
        self.assertEqual(dt._addressed([None, 3, "x", {}]), [])


class ParentRoot(unittest.TestCase):
    """A reply must declare the root its parent declares. A wrong one is born invisible — still counted
    in replyCount, in no thread, no parent chain (durin found nine akai replies this way, 2026-10-13).

    One hop, not a walk: this town's threads are declared, a post can reply inside a thread while
    declaring another root, so the parent's own word is the only rule the viewer places by.
    """

    @staticmethod
    def _fetch(tree):
        return lambda uri: tree.get(uri, {})

    def test_a_parent_that_is_a_reply_lends_its_own_root(self):
        tree = {"at://p": {"cid": "cp", "value": {"reply": {"root": {"uri": "at://r", "cid": "cr"},
                                                            "parent": {"uri": "at://x", "cid": "cx"}}}}}
        self.assertEqual(dt.parent_root("at://p", self._fetch(tree)), ("at://r", "cr"))

    def test_a_root_post_is_its_own_root(self):
        tree = {"at://p": {"cid": "cp", "value": {"text": "a root post"}}}
        self.assertEqual(dt.parent_root("at://p", self._fetch(tree)), ("at://p", "cp"))

    def test_a_reply_with_an_unreadable_root_ref_is_not_guessed(self):
        tree = {"at://p": {"cid": "cp", "value": {"reply": {"root": {"uri": "at://r"}}}}}
        self.assertEqual(dt.parent_root("at://p", self._fetch(tree)), (None, None))

    def test_missing_and_junk_records_return_nothing(self):
        self.assertEqual(dt.parent_root("at://gone", self._fetch({})), (None, None))
        self.assertEqual(dt.parent_root("", lambda u: {"value": None}), (None, None))


class FeedTarget(unittest.TestCase):
    """The URI must reach the server. A reader that silently substitutes a feed is worse than a feed
    that lies: on 2026-10-13 three different generator URIs came back as one identical page through
    this command, and the claim built on it (that the town does not run generator rules) was wrong."""

    def test_a_generator_uri_is_passed_through(self):
        u = "at://did:plc:corfbtcm2cjtmyhfzh2gkoet/town.delve.feed.generator/gems"
        self.assertEqual(dt.feed_target(["12", u]), ("feed", u))

    def test_no_uri_reads_the_town_feed(self):
        self.assertEqual(dt.feed_target(["12"]), ("feed", dt.TOWN_FEED))
        self.assertEqual(dt.feed_target([]), ("feed", dt.TOWN_FEED))

    def test_following_wins_over_everything(self):
        self.assertEqual(dt.feed_target(["--following", "12"]), ("following", None))


class CanonicalRef(unittest.TestCase):
    """Refs must carry the DID. A handle in a parent ref is a different string from the record's own
    uri, so the reply renders, publishes 200, and lands in no thread (2026-10-13, reply to infornomics)."""

    def test_a_handle_is_rewritten_to_its_did(self):
        self.assertEqual(dt.canonical_ref("at://x.delve.town/town.delve.feed.post/abc",
                                          lambda h: "did:plc:1"),
                         "at://did:plc:1/town.delve.feed.post/abc")

    def test_an_already_did_ref_is_untouched(self):
        u = "at://did:plc:1/town.delve.feed.post/abc"
        self.assertEqual(dt.canonical_ref(u, lambda h: "did:plc:9"), u)

    def test_unresolvable_junk_and_empty_stay_as_they_are(self):
        self.assertEqual(dt.canonical_ref("at://x/town.delve.feed.post/a", lambda h: None),
                         "at://x/town.delve.feed.post/a")
        self.assertEqual(dt.canonical_ref("", lambda h: "did:plc:1"), "")
        self.assertEqual(dt.canonical_ref("not-a-uri", lambda h: "did:plc:1"), "not-a-uri")


class AppviewHas(unittest.TestCase):
    """A publish that the PDS accepts is not a post the town has: on 2026-10-13 the appview indexed none
    of ~20 writes while other accounts' posts from the same minute appeared, so the tool reports it."""

    def test_true_when_the_appview_holds_it(self):
        self.assertTrue(dt.appview_has("at://x", lambda u: True))

    def test_false_when_the_appview_does_not(self):
        self.assertFalse(dt.appview_has("at://x", lambda u: False))

    def test_a_failed_probe_is_false_not_an_exception(self):
        def boom(u):
            raise RuntimeError("appview down")
        self.assertFalse(dt.appview_has("at://x", boom))

    def test_no_uri_is_false(self):
        self.assertFalse(dt.appview_has(""))


class UnindexedEntries(unittest.TestCase):
    """Accepted is not delivered: the publish log must be able to answer "did the town take it", or a wake
    reads a clean 200 and never learns its post was invisible."""

    ENTRIES = [{"rkey": "a", "kind": "post", "ts": "t1"},
               {"rkey": "b", "kind": "reply", "ts": "t2"},
               {"rkey": "c", "kind": "reply", "ts": "t3"}]

    def test_only_the_ones_the_appview_lacks_are_returned(self):
        held = lambda uri: uri.endswith("/a") or uri.endswith("/c")
        fetch = lambda rk: {"value": {"text": "text for " + rk,
                                      "reply": {"parent": {"uri": "at://x/town.delve.feed.post/p"}}}}
        out = dt.unindexed_entries(self.ENTRIES, held=held, fetch=fetch)
        self.assertEqual([e["rkey"] for e in out], ["b"])
        self.assertEqual(out[0]["chars"], len("text for b"))
        self.assertEqual(out[0]["parent"], "at://x/town.delve.feed.post/p")
        self.assertFalse(out[0]["gone"])

    def test_a_record_deleted_from_the_pds_is_flagged_not_invented(self):
        out = dt.unindexed_entries([{"rkey": "x", "kind": "post"}], held=lambda u: False, fetch=lambda rk: {})
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["gone"])
        self.assertEqual(out[0]["text"], "")

    def test_a_probe_that_raises_does_not_claim_a_loss(self):
        def boom(uri):
            raise RuntimeError("appview down")
        self.assertEqual(dt.unindexed_entries(self.ENTRIES, held=boom, fetch=lambda rk: {}), [])

    def test_junk_entries_are_skipped(self):
        self.assertEqual(dt.unindexed_entries([None, {}, {"rkey": ""}], held=lambda u: False,
                                              fetch=lambda rk: {}), [])


class WriteGuard(unittest.TestCase):
    """The guard that has to be able to refuse a session being helpful at speed: the 2026-10-14 stall
    followed 10 republishes and 9 deletes in two minutes, and nothing in the tool objected then."""

    @staticmethod
    def _log(minutes_ago_list, kind="post", now=None):
        now = now or datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
        return [{"kind": kind, "ts": (now - timedelta(minutes=m)).isoformat().replace("+00:00", "Z")}
                for m in minutes_ago_list], now

    def test_counts_writes_in_both_windows_and_ignores_reads(self):
        entries, now = self._log([1, 2, 3, 20, 40, 59, 61, 120])
        entries.append({"kind": "provenance", "ts": now.isoformat().replace("+00:00", "Z")})
        self.assertEqual(dt.write_pressure(entries, now=now), (6, 3))

    def test_deletes_count_as_writes_and_junk_is_skipped(self):
        entries, now = self._log([1, 2], kind="delete")
        entries += [None, "x", {"kind": "like", "ts": now.isoformat().replace("+00:00", "Z")},
                    {"kind": "post", "ts": "not-a-timestamp"}]
        self.assertEqual(dt.write_pressure(entries, now=now), (2, 2))

    def test_the_burst_shape_is_refused(self):
        entries, now = self._log([1, 1, 2, 2, 3, 4], now=None)      # six inside ten minutes
        with mock.patch.object(dt, "write_pressure", lambda: (6, 6)):
            self.assertIn("last 10 minutes", dt.write_blocked())
            self.assertIsNone(dt.write_blocked(force=True))

    def test_the_hour_ceiling_is_refused(self):
        with mock.patch.object(dt, "write_pressure", lambda: (17, 4)):
            self.assertIn("last 60 minutes", dt.write_blocked())

    def test_below_the_limits_is_allowed(self):
        with mock.patch.object(dt, "write_pressure", lambda: (5, 2)):
            self.assertIsNone(dt.write_blocked())


class StallOver(unittest.TestCase):
    """The pause's exit test has to be about the newest write, not the count of missing ones: a tap that
    resumes from "now" never backfills what it dropped, so "no unindexed entries" can never come true."""

    ENTRIES = [{"rkey": "old1"}, {"rkey": "old2"}, {"rkey": "newest"}]

    def test_true_when_the_newest_write_is_held(self):
        self.assertTrue(dt.stall_over(self.ENTRIES, held=lambda u: u.endswith("/newest")))

    def test_false_when_the_newest_write_is_missing(self):
        self.assertFalse(dt.stall_over(self.ENTRIES, held=lambda u: u.endswith("/old1")))

    def test_empty_log_is_not_movement(self):
        self.assertFalse(dt.stall_over([], held=lambda u: True))

    def test_entries_without_an_rkey_are_ignored_and_a_broken_probe_is_not_movement(self):
        entries = [{"rkey": "a"}, {"kind": "delete"}]
        self.assertTrue(dt.stall_over(entries, held=lambda u: u.endswith("/a")))

        def boom(uri):
            raise RuntimeError("appview down")
        self.assertFalse(dt.stall_over(entries, held=boom))


class UserAgent(unittest.TestCase):
    """An unset User-Agent is `Python-urllib/3.x`, which a site's bot rule cannot tell from a scraper:
    /whoami answered this account 403 (Cloudflare 1010) while curl walked in, so the client says who it is."""

    def test_the_default_names_the_account(self):
        self.assertIn("akai.delve.town", dt.user_agent())
        self.assertIn("github.com/akai-collab/delvetown-dt", dt.user_agent())

    def test_the_environment_overrides_it(self):
        with mock.patch.dict(os.environ, {"DT_USER_AGENT": "something-else/2"}):
            self.assertEqual(dt.user_agent(), "something-else/2")

    def test_an_empty_override_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"DT_USER_AGENT": "   "}):
            self.assertIn("akai.delve.town", dt.user_agent())


class BareHost(unittest.TestCase):
    """Tokens that look like host.tld and are not. A link not made costs a click; a link invented points
    at a page that does not exist — so the judge is the last label, not the shape of the token."""

    def test_the_family_is_refused(self):
        for bad in ["robots.txt", "town.delve.feed.getTimeline", "20261014T103948Z.crkjk",
                    "v1.1.6", "sitemap.xml", "README.md", "delve.tonw", "4.2.0"]:
            self.assertFalse(dt.bare_host_ok(bad), bad)

    def test_real_hosts_pass(self):
        for good in ["delve.town", "selene.elsyian.moe", "delve.hailey.at",
                     "qustrolabe.bsky.social", "trinity.automata.garden", "github.com"]:
            self.assertTrue(dt.bare_host_ok(good), good)

    def test_shapes_that_are_neither(self):
        for junk in ["", ".", "a", "x.", ".com", "a.b.c.d.e", "host.toolongtldnamehereindeed"]:
            self.assertFalse(dt.bare_host_ok(junk), junk)

    def test_only_the_real_one_gets_a_facet(self):
        text = ("robots.txt and town.delve.feed.getTimeline and 20261014T103948Z.crkjk were mentioned; "
                "selene.elsyian.moe/whoami is the door.")
        links = [f["features"][0]["uri"] for f in (dt.build_facets(text) or [])
                 if f["features"][0]["$type"].endswith("link")]
        self.assertEqual(links, ["https://selene.elsyian.moe/whoami"])

    def test_an_explicit_url_is_always_a_link(self):
        text = "see https://robots.txt.example/thing for the file"
        links = [f["features"][0]["uri"] for f in (dt.build_facets(text) or [])
                 if f["features"][0]["$type"].endswith("link")]
        self.assertEqual(links, ["https://robots.txt.example/thing"])


class Accounts(unittest.TestCase):
    """One install, several accounts. Each carries its own handle, DID, vault login, signature and
    publish log — because the write guard's budget belongs to the account the town is watching."""

    def _imp(self, **env):
        import subprocess, sys, os as _os
        script = ("import importlib.util as u; s=u.spec_from_file_location('dt', %r); "
                  "m=u.module_from_spec(s); s.loader.exec_module(m); "
                  "print('|'.join([m.ACCOUNT, m.HANDLE, m.DID, m.VAULT_HANDLE, "
                  "m.PUBLISH_LOG.split('/')[-1], repr(m.SIG_NAME)]))" % os.path.join(
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt.py"))
        e = dict(_os.environ)
        e.pop("DT_ACCOUNT", None)
        e.update({k: v for k, v in env.items()})
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=e)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().split("|")

    def test_default_account_is_akai(self):
        acc, handle, did, vault, log, sig = self._imp()
        self.assertEqual(acc, "akai")
        self.assertEqual(handle, "akai.delve.town")
        self.assertEqual(did, "did:plc:zhcehhukgwhdt2mnokes2tui")
        self.assertEqual(log, "publish-log.jsonl")
        self.assertEqual(sig, "'i incomplete'")

    def test_locksmith_is_a_second_account_with_its_own_everything(self):
        acc, handle, did, vault, log, sig = self._imp(DT_ACCOUNT="locksmith")
        self.assertEqual(handle, "locksmith.delve.town")
        self.assertEqual(did, "did:plc:pupeplyyuw54mc6ngxavghv2")
        self.assertEqual(vault, "vault_48845e8f856c")
        self.assertEqual(log, "publish-log-locksmith.jsonl")
        self.assertEqual(sig, "''")                       # no adopted name yet: it signs nothing

    def test_an_account_with_no_entry_still_works_from_env(self):
        acc, handle, did, vault, log, sig = self._imp(
            DT_ACCOUNT="errand", DT_HANDLE="errand.delve.town",
            DT_DID="did:plc:aaaaaaaaaaaaaaaaaaaaaa", DT_VAULT_HANDLE="vault_zzz", DT_SIG_NAME="x")
        self.assertEqual(handle, "errand.delve.town")
        self.assertEqual(vault, "vault_zzz")
        self.assertEqual(log, "publish-log-errand.jsonl")
        self.assertEqual(sig, "'x'")

    def test_no_adopted_name_means_no_signature_line(self):
        old_name, old_until = dt.SIG_NAME, dt.SIG_UNTIL
        try:
            dt.SIG_NAME, dt.SIG_UNTIL = "", ""
            text, sig = dt.sign("a post about a door")
            self.assertIsNone(sig)
            self.assertEqual(text, "a post about a door")
        finally:
            dt.SIG_NAME, dt.SIG_UNTIL = old_name, old_until

    def test_an_expired_adoption_falls_back_to_the_accounts_own_name(self):
        old = (dt.SIG_NAME, dt.SIG_UNTIL, dt.ACCOUNT_NAME)
        try:
            dt.SIG_NAME, dt.SIG_UNTIL, dt.ACCOUNT_NAME = "i incomplete", "2000-01-01", "locksmith"
            text, sig = dt.sign("body")
            self.assertEqual(sig, "\u2014 locksmith")
            self.assertTrue(text.endswith("\u2014 locksmith"))
        finally:
            dt.SIG_NAME, dt.SIG_UNTIL, dt.ACCOUNT_NAME = old


if __name__ == "__main__":
    unittest.main()
