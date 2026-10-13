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


if __name__ == "__main__":
    unittest.main()
