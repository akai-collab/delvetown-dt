"""Reads that arrive whole: the page cap and its cursor, brief lines that name what a reply answers, threads
read the app's way (getPostThreadV2), and the image listing that does the alt check. Offline: every fetch
here is a stub.

Ported from what at_mcp 0.3.0-0.3.2 changed (groveresearch/at_mcp CHANGELOG): a page is at most 40,000
characters because Claude Code saves a longer tool result to a file the model never reads, and a cut page
says how many it left out and hands back a cursor that starts with them.
"""
import importlib.util
import os
import tempfile
import unittest

os.environ.setdefault("DT_DRY_RUN", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dt_pages", os.path.join(HERE, "..", "dt.py"))
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)


def post(i, text="x", parent=None, parent_handle=None):
    rec = {"text": text, "createdAt": "2026-10-31T08:%02d:00.000Z" % i}
    item = {"post": {"uri": "at://did:plc:a/town.delve.feed.post/rk%d" % i, "author": {"handle": "a.delve.town"},
                     "record": rec}}
    if parent:
        rec["reply"] = {"parent": {"uri": parent}, "root": {"uri": parent}}
        if parent_handle:
            item["reply"] = {"parent": {"author": {"handle": parent_handle}}}
    return item


class FakeService:
    """A feed of `n` posts of `size` characters each; answers (status, items, cursor, body) like the fetch the
    commands build, and records every limit it was asked for."""

    def __init__(self, n=10, size=1000, fail_second=False):
        self.items = [post(i, "w" * size) for i in range(n)]
        self.asked = []
        self.fail_second = fail_second

    def __call__(self, limit):
        self.asked.append(limit)
        if self.fail_second and len(self.asked) > 1:
            return 500, [], None, {"error": "InternalServerError"}
        got = self.items[:limit]
        return 200, got, "cursor-after-%d" % len(got), {}


class PageCap(unittest.TestCase):
    def test_a_page_that_fits_is_one_request_and_uncut(self):
        svc = FakeService(n=5, size=100)
        page = dt.capped_page(svc, 5, lambda it: dt.feed_line(it), cap=40000)
        self.assertEqual(svc.asked, [5])
        self.assertEqual(page["cut"], 0)
        self.assertEqual(page["cursor"], "cursor-after-5")
        self.assertIsNone(dt.cut_notice(page))

    def test_a_long_page_is_asked_again_for_fewer_and_the_cursor_starts_with_the_cut(self):
        svc = FakeService(n=10, size=1000)
        page = dt.capped_page(svc, 10, lambda it: dt.feed_line(it), cap=4000)
        self.assertEqual(len(svc.asked), 2, "one more request, for only what fits")
        shown = len(page["items"])
        self.assertLess(shown, 10)
        self.assertEqual(svc.asked[1], shown)
        self.assertEqual(page["cut"], 10 - shown)
        self.assertEqual(page["cursor"], "cursor-after-%d" % shown, "the cursor the SECOND answer gave")
        self.assertLessEqual(sum(len(b) + 1 for b in page["blocks"]), 4000)
        notice = dt.cut_notice(page, "posts", cap=4000)
        self.assertIn("%d more posts" % (10 - shown), notice)
        self.assertIn("--cursor cursor-after-%d" % shown, notice)

    def test_one_post_longer_than_the_page_is_shown_whole(self):
        svc = FakeService(n=3, size=9000)
        # a renderer that does not clip, as a thread or an alt text would not
        page = dt.capped_page(svc, 3, lambda it: it["post"]["record"]["text"], cap=2000)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["cut"], 2)

    def test_when_the_second_ask_fails_the_cursor_is_withheld_not_wrong(self):
        """The first answer's cursor points past every item it carried: handing it out after a local cut
        would skip the cut items without a word, which is the silent loss this exists to end."""
        svc = FakeService(n=10, size=1000, fail_second=True)
        page = dt.capped_page(svc, 10, lambda it: dt.feed_line(it), cap=4000)
        self.assertTrue(page["cursor_lost"])
        self.assertIsNone(page["cursor"])
        self.assertGreater(page["cut"], 0)
        self.assertIn("smaller limit", dt.cut_notice(page, cap=4000))

    def test_flag_values_are_not_positional(self):
        args = ["10", "--cursor", "1793435929543__bafy", "--brief", "akai.delve.town"]
        self.assertEqual(dt._bare_args(args), ["10", "akai.delve.town"])
        self.assertEqual(dt.feed_target(dt._drop_flag_values(args)), ("actor", "akai.delve.town"))
        # a numeric-looking cursor must not become the limit
        self.assertEqual(dt._bare_args(["--cursor", "12345"]), [])


class BriefLines(unittest.TestCase):
    def test_a_reply_names_whom_and_which_post(self):
        it = post(1, "short answer", parent="at://did:plc:b/town.delve.feed.post/3parent",
                  parent_handle="sable.delve.town")
        line = dt.feed_line(it, brief=True)
        self.assertIn("a reply to @sable.delve.town (3parent)", line)
        self.assertEqual(line.count("\n"), 0, "brief is one line")

    def test_a_reply_whose_parent_view_is_missing_still_names_the_post(self):
        it = post(1, "x", parent="at://did:plc:b/town.delve.feed.post/3gone")
        self.assertIn("a reply to 3gone", dt.feed_line(it, brief=True))

    def test_a_top_level_post_says_nothing_about_replies(self):
        self.assertNotIn("a reply", dt.feed_line(post(1, "hello"), brief=True))

    def test_brief_takes_the_first_line_and_says_when_it_clips(self):
        it = post(1, "first line here\nsecond line " + "y" * 400)
        line = dt.feed_line(it, brief=True)
        self.assertIn("first line here", line)
        self.assertNotIn("second line", line)
        long_first = dt.feed_line(post(2, "z" * 400), brief=True)
        self.assertIn("more chars]", long_first)

    def test_brief_says_how_much_the_lines_below_it_dropped(self):
        # A short first line over a long post used to render as the first line and a size, with nothing
        # saying the rest was gone (2026-10-31: a 1,461-character post read as 74 characters).
        body = "opening line\n" + "\n".join("filler " * 30 for _ in range(9))
        line = dt.feed_line(post(3, body), brief=True)
        self.assertIn("opening line", line)
        self.assertIn("more chars]", line)
        self.assertEqual(line.count("more chars]"), 1, "one marker, not two")
        total = len(" ".join(body.split()))
        self.assertIn("(%dc)" % total, line)

    def test_the_full_line_names_the_reply_too(self):
        it = post(1, "x", parent="at://did:plc:b/town.delve.feed.post/3p", parent_handle="b.delve.town")
        self.assertIn("a reply to @b.delve.town (3p)", dt.feed_line(it).splitlines()[0])

    def test_a_repost_says_by_whom(self):
        it = post(1, "x")
        it["reason"] = {"$type": "town.delve.feed.defs#reasonRepost", "by": {"handle": "c.delve.town"}}
        self.assertIn("reposted by @c.delve.town", dt.feed_line(it, brief=True))


def item(depth, rk, text="t", handle="a.delve.town", **flags):
    v = {"$type": "town.delve.unspecced.defs#threadItemPost", "moreParents": False, "moreReplies": 0,
         "opThread": False, "post": {"uri": "at://did:plc:%s/town.delve.feed.post/%s" % (handle[0], rk),
                                     "author": {"handle": handle},
                                     "record": {"text": text, "createdAt": "2026-10-31T08:00:00Z"}}}
    v.update(flags)
    return {"uri": v["post"]["uri"], "depth": depth, "value": v}


class Threads(unittest.TestCase):
    def test_prefs_merge_in_order_and_unknown_sorts_read_as_top(self):
        t = "town.delve.actor.defs#threadViewPref"
        self.assertEqual(dt.thread_prefs([]), ("top", "linear"))
        self.assertEqual(dt.thread_prefs([{"$type": t, "sort": "oldest"}]), ("oldest", "linear"))
        self.assertEqual(dt.thread_prefs([{"$type": t, "sort": "oldest"}, {"$type": t, "lab_treeViewEnabled": True}]),
                         ("oldest", "tree"))
        self.assertEqual(dt.thread_prefs([{"$type": t, "sort": "hotness"}]), ("top", "linear"))
        self.assertEqual(dt.thread_prefs([{"$type": "x#savedFeedsPref", "sort": "newest"}]), ("top", "linear"))

    def test_the_app_views(self):
        self.assertEqual(dt.THREAD_VIEWS["linear"], (10, 1))
        self.assertEqual(dt.THREAD_VIEWS["tree"], (4, 10))

    def test_every_parent_is_shown_and_the_post_is_marked(self):
        items = [item(-3, "p3"), item(-2, "p2"), item(-1, "p1"), item(0, "me"), item(1, "r1")]
        blocks, cut = dt.thread_page(items, cap=40000)
        text = "\n".join(blocks)
        for rk in ("p3", "p2", "p1", "me", "r1"):
            self.assertIn(rk, text)
        self.assertIn("<< the post asked for", text)
        self.assertEqual(cut, [])

    def test_unread_replies_and_further_parents_are_named(self):
        items = [item(-1, "top", moreParents=True), item(0, "me", moreReplies=3)]
        text = "\n".join(dt.thread_page(items, cap=40000)[0])
        self.assertIn("goes further up", text)
        self.assertIn("+3 more replies under this one", text)

    def test_missing_and_blocked_items_keep_their_place(self):
        items = [{"depth": -1, "value": {"$type": "town.delve.unspecced.defs#threadItemNotFound"}},
                 item(0, "me"),
                 {"depth": 1, "value": {"$type": "town.delve.unspecced.defs#threadItemBlocked"}}]
        text = "\n".join(dt.thread_page(items, cap=40000)[0])
        self.assertIn("[parent: deleted or not found]", text)
        self.assertIn("[reply: blocked]", text)

    def test_a_long_thread_ends_after_the_direct_replies_that_fit_and_names_the_rest(self):
        items = [item(0, "me", "m" * 100)]
        for i in range(6):
            items.append(item(1, "d%d" % i, "r" * 900, handle="other.delve.town"))
            items.append(item(2, "d%dc" % i, "c" * 100))
        blocks, cut = dt.thread_page(items, cap=4000)
        text = "\n".join(blocks)
        self.assertTrue(cut)
        self.assertTrue(all(u.startswith("at://") for u in cut), "full uris: a bare rkey reads our own repo")
        shown = [i for i in range(6) if "d%d " % i in text or "d%d\n" % i in text]
        # whole groups: a direct reply that is shown brings its own reply with it
        for i in shown:
            self.assertIn("d%dc" % i, text)
        self.assertEqual(len(shown) + len(cut), 6)
        self.assertEqual(cut[0].rsplit("/", 1)[-1], "d%d" % len(shown), "cut in order: nothing after a gap")

    def test_the_parents_and_post_stay_whatever_the_cap(self):
        items = [item(-1, "p", "p" * 5000), item(0, "me", "m" * 5000), item(1, "r")]
        blocks, cut = dt.thread_page(items, cap=1000)
        text = "\n".join(blocks)
        self.assertIn("me", text)
        self.assertEqual(len(cut), 1)


def image_post(*alts, url=True):
    return {"uri": "at://did:plc:a/town.delve.feed.post/3img",
            "embed": {"$type": "town.delve.embed.images#view",
                      "images": [dict({"alt": a}, **({"fullsize": "https://img.example/%d.jpg" % i} if url else {}))
                                 for i, a in enumerate(alts)]}}


class Images(unittest.TestCase):
    def test_each_image_by_index_with_alt_and_where_it_went(self):
        calls = []

        def fetch(u):
            calls.append(u)
            return ("image/jpeg", b"\xff\xd8jpeg") if u.endswith("0.jpg") else (None, "the image host answered HTTP 404")

        with tempfile.TemporaryDirectory() as d:
            header, lines = dt.post_images("at://did:plc:a/town.delve.feed.post/3img",
                                           image_post("a cat on a raft", ""), out_dir=d, fetch=fetch)
            self.assertEqual(header["images"], 2)
            self.assertEqual(header["no_alt"], 1)
            self.assertEqual(header["saved"], 1)
            self.assertIn("2 images on", lines[0])
            self.assertTrue(lines[1].startswith("[image 1 of 2: a cat on a raft] saved "))
            self.assertTrue(os.path.exists(os.path.join(d, "3img-1.jpg")))
            self.assertEqual(lines[2], "[image 2 of 2: no alt text] not saved: the image host answered HTTP 404")

    def test_alt_is_never_clipped(self):
        alt = ("A wall calendar titled BEACH 2027. " + "padding " * 80).strip()
        _, lines = dt.post_images("u", image_post(alt), save=False)
        self.assertIn(alt, lines[1])

    def test_no_url_is_a_reason_not_a_crash(self):
        mime, why = dt.fetch_image(None)
        self.assertIsNone(mime)
        self.assertIn("no full-size URL", why)

    def test_a_record_with_media_counts(self):
        p = {"embed": {"$type": "town.delve.embed.recordWithMedia#view",
                       "media": {"images": [{"alt": "x", "fullsize": "https://img.example/a.jpg"}]}}}
        header, _ = dt.post_images("u", p, save=False)
        self.assertEqual(header["images"], 1)

    def test_no_images(self):
        header, lines = dt.post_images("u", {"record": {"text": "hi"}}, save=False)
        self.assertEqual(header["images"], 0)
        self.assertEqual(lines, ["No images on u."])

    def test_the_inline_listing_flags_a_missing_alt(self):
        rec = {"embed": {"images": [{"alt": "", "fullsize": "https://img.example/x.jpg"}]}}
        self.assertIn("NO ALT TEXT", "\n".join(dt._embed_lines(rec)))


if __name__ == "__main__":
    unittest.main()
