"""Offline tests for dt.py — no network, no vault.

Run from the repo root:  python3 -m unittest discover -s tests -v

Several cases are the published mistakes the pre-flight exists for; the comments say which.
"""
import importlib.util
import os
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


if __name__ == "__main__":
    unittest.main()
