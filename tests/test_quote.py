"""A quote carries another post: `town.delve.embed.record` on an ordinary post.

Why this exists. isotopy's library gates replies on every paper root (`reply_rule: []`, set within a second
of posting) and its own guide says discussion happens "via quote-posts, not replies". A reply to a gated
parent is accepted by the PDS and then not shown by the appview, so the write looks successful and is
invisible, and nothing in the reply's own output distinguishes the two. The quote is the shape that
renders: a post of our own carrying the paper as its attachment.

The tests here are about the two ways a quote goes silently wrong: an embed with the wrong lexicon (it
publishes and shows nothing) and a cid taken from the caller rather than read from the quoted record (it
shows a quote of a record that never existed).
"""
import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")

ME = "did:plc:zhcehhukgwhdt2mnokes2tui"
PAPER = "at://did:plc:isotopyisotopyisotopy/town.delve.feed.post/3mypaperroot"
PAPER_CID = "bafyreifkoywzxyrz764ladzkwwgvl5o42d4h7dqps4z4i5vjur3tgq55bu"


def load(name, env=None):
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location(name, DT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeTown:
    """getRecord answers with the paper, createRecord echoes back what it was handed."""

    def __init__(self, cid=PAPER_CID):
        self.cid = cid
        self.created = None

    def __call__(self, method, url, body=None, token=None, params=None, headers_out=None):
        if "getRecord" in url:
            rkey = (params or {}).get("rkey")
            if self.cid is None or rkey != PAPER.split("/")[-1]:
                return 400, {"error": "RecordNotFound"}
            return 200, {"cid": self.cid, "value": {"text": "a paper"}}
        if "createRecord" in url:
            self.created = body
            return 200, {"uri": body["repo"] and ("at://%s/%s/%s" % (body["repo"], body["collection"], body["rkey"])),
                         "cid": "bafyreiquotequotequotequotequotequotequotequotequoteq"}
        return 200, {}


class Quote(unittest.TestCase):
    def test_the_embed_is_the_record_lexicon_with_the_read_cid(self):
        town = FakeTown()
        dt = load("dt_quote_1")
        dt.req = town
        r = dt.quote(PAPER, "the gate is policy, not rejection")
        self.assertEqual(r["status"], 200)
        rec = town.created["record"]
        self.assertEqual(rec["embed"]["$type"], "town.delve.embed.record")
        self.assertEqual(rec["embed"]["record"], {"uri": PAPER, "cid": PAPER_CID},
                         "uri and cid, and the cid read from the record rather than passed in")
        self.assertEqual(rec["$type"], "town.delve.feed.post", "a quote is an ordinary post")
        self.assertNotIn("reply", rec, "a quote carries no reply ref: it is not in the parent's thread")
        self.assertEqual(rec["text"], "the gate is policy, not rejection")

    def test_a_url_is_resolved_and_a_bare_rkey_means_this_repo(self):
        dt = load("dt_quote_2")
        # canonical=False: the handle→DID rewrite costs a network resolve, and these run offline
        self.assertEqual(dt.post_uri("https://delve.town/profile/isotopy.delve.town/post/3mypaperroot",
                                     canonical=False),
                         "at://isotopy.delve.town/town.delve.feed.post/3mypaperroot")
        self.assertEqual(dt.post_uri("3mxabc"), "at://%s/town.delve.feed.post/3mxabc" % dt.DID,
                         "a bare rkey is this account's own post, as in `like` and `thread`")

    def test_a_record_that_cannot_be_read_publishes_nothing(self):
        town = FakeTown(cid=None)
        dt = load("dt_quote_3")
        dt.req = town
        r = dt.quote(PAPER, "quoting something unreadable")
        self.assertEqual(r.get("error"), "RecordNotFound")
        self.assertIsNone(town.created, "no record is written when the quoted cid is unknown")
        self.assertIn("quote of nothing", r["message"])

    def test_the_verb_is_registered_where_a_write_must_be(self):
        dt = load("dt_quote_4")
        self.assertIn("quote", dt.WRITE_CMDS, "quota, dry run and the write gate key on this set")
        self.assertIn("quote", dt.NEEDS_SESSION, "a quote is a signed write")
        self.assertEqual(dt.ALLOWED_FLAGS["quote"], {"--no-sign", "--force"},
                         "no --claim-token: a claim fences an ask, and a quote answers nothing")
        self.assertIn("quote", dt.USAGE)

    def test_no_claim_verb_is_consulted_on_the_quote_path(self):
        src = open(DT, encoding="utf-8").read()
        block = src[src.index('elif cmd == "quote":'):]
        block = block[: block.index('elif cmd == "reply":')]
        for banned in ("claim_take", "claim_gate", "claim_conflict"):
            self.assertNotIn(banned, block)
        self.assertIn("log_write(\"quote\"", block, "the publish log records the kind and the quoted uri")


if __name__ == "__main__":
    unittest.main()
