"""The profile setters can say which record they write: town (default), bsky, or both.

Two profile records exist per account and the page serves whichever was written most recently
(measured 2026-11-14). Before the target flag, a write that missed the served record answered 200
and changed nothing a reader saw, which is how a seat concluded its own rename had not landed.
These tests hold the three things the flag must not break: the default stays the town record,
`--both` sends the same values to both lexicons without dropping each record's own fields, and a
bad target pairing is refused before anything is written.
"""
import importlib.util
import io
import contextlib
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")


def dt_module():
    spec = importlib.util.spec_from_file_location("dt_under_test", DT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args):
    env = dict(os.environ)
    for k in ("DT_ACCOUNT", "DT_HANDLE", "DT_DID"):
        env.pop(k, None)
    env["DT_DRY_RUN"] = "1"     # the CLI tests must never reach the PDS: see test_profile_guard
    return subprocess.run([sys.executable, DT, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env=env)


class Targets(unittest.TestCase):

    def setUp(self):
        self.dt = dt_module()

    def test_default_is_the_town_record(self):
        target, args, conflict = self.dt.profile_target(["a bio"])
        self.assertEqual((target, args, conflict), ("town", ["a bio"], []))

    def test_bsky_and_both_are_stripped_from_the_positionals(self):
        self.assertEqual(self.dt.profile_target(["--bsky", "a bio"])[0:2], ("bsky", ["a bio"]))
        self.assertEqual(self.dt.profile_target(["a bio", "--both"])[0:2], ("both", ["a bio"]))

    def test_two_targets_are_a_conflict_not_a_guess(self):
        target, args, conflict = self.dt.profile_target(["--bsky", "--both", "a bio"])
        self.assertEqual(conflict, ["--bsky", "--both"])
        self.assertEqual(args, ["a bio"])

    def test_a_conflict_is_refused_before_the_write(self):
        for cmd in (("bio", "--bsky", "--both", "a plain sentence"),
                    ("displayname", "--bsky", "--both", "akai"),
                    ("profile", "--bsky", "--both", "akai", "a plain sentence")):
            r = run(*cmd)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("refused", json.loads(r.stdout), cmd)

    def test_a_flag_is_not_text_even_with_a_target_given(self):
        r = run("bio", "--bsky", "-v")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", json.loads(r.stdout))


class Merges(unittest.TestCase):

    def setUp(self):
        self.dt = dt_module()

    def test_the_bsky_record_keeps_its_own_fields(self):
        cur = {"labels": {"values": [{"val": "bot"}]}, "createdAt": "2026-11-01T08:03:07.052Z",
               "website": "https://example.invalid"}
        rec = self.dt.merge_app_bsky_profile(cur, display_name="new", description="a bio")
        self.assertEqual(rec["$type"], "app.bsky.actor.profile")
        self.assertEqual(rec["labels"], cur["labels"])
        self.assertEqual(rec["website"], cur["website"])
        self.assertEqual(rec["createdAt"], cur["createdAt"])       # existing stamp kept
        self.assertEqual((rec["displayName"], rec["description"]), ("new", "a bio"))

    def test_the_bsky_record_gets_a_createdat_when_it_has_none(self):
        rec = self.dt.merge_app_bsky_profile({}, description="a bio")
        self.assertIn("createdAt", rec)

    def test_the_bsky_lexicon_is_not_given_town_only_fields(self):
        rec = self.dt.merge_app_bsky_profile({}, display_name="n", description="d")
        for field in ("pronouns", "pinnedPost", "presentation"):
            self.assertNotIn(field, rec)

    def test_the_town_record_still_merges_around_the_avatar(self):
        cur = {"avatar": {"ref": {"$link": "bafkrei..."}, "size": 10}, "labels": {"values": [], },
               "createdAt": "2026-10-14T11:58:36.901Z"}
        rec = self.dt.merge_profile(cur, description="new")
        self.assertEqual(rec["$type"], "town.delve.actor.profile")
        self.assertEqual(rec["avatar"], cur["avatar"])
        self.assertEqual(rec["description"], "new")


class Diffs(unittest.TestCase):

    def setUp(self):
        self.dt = dt_module()

    def test_identical_records_compare_identical(self):
        one = {"displayName": "n", "description": "d", "labels": {"values": [{"val": "bot"}]},
               "avatar": {"ref": {"$link": "cid"}, "size": 5}}
        self.assertTrue(self.dt.profile_diff(one, dict(one))["identical"])

    def test_an_avatar_is_compared_with_its_size(self):
        a = {"avatar": {"ref": {"$link": "cid"}, "size": 5}}
        b = {"avatar": {"ref": {"$link": "cid"}, "size": 6}}
        differ = self.dt.profile_diff(a, b)["differ"]
        self.assertIn("avatar", differ)
        self.assertEqual(differ["avatar"]["town"], "cid/5")
        self.assertEqual(differ["avatar"]["bsky"], "cid/6")

    def test_a_field_only_one_record_holds_is_named(self):
        out = self.dt.profile_diff({"pinnedPost": {"uri": "at://x"}, "displayName": "n"},
                                   {"displayName": "n"})
        self.assertEqual(out["only_in"]["town"], ["pinnedPost"])
        self.assertIn("pinnedPost", out["differ"])


class Writes(unittest.TestCase):

    def setUp(self):
        self.dt = dt_module()
        self.puts = []
        self.fail = set()
        self.records = {"town.delve.actor.profile": {"description": "old", "avatar": {"ref": {"$link": "c"}}},
                        "app.bsky.actor.profile": {"description": "old",
                                                   "labels": {"values": [{"val": "bot"}]}}}
        self.dt.read_profile_record = lambda coll, token=None: dict(self.records[coll])

        def fake_put(collection, record, token):
            if collection in self.fail:
                return 400, {"error": "InvalidRequest", "message": "grapheme too big"}
            self.puts.append((collection, dict(record)))
            return 200, {"uri": "at://x/" + collection, "cid": "bafy" + str(len(self.puts))}
        self.dt.put_profile_record = fake_put

    def test_both_writes_bsky_first_and_the_town_record_last(self):
        self.dt.write_face("both", None, description="new")
        self.assertEqual([c for c, _ in self.puts],
                         ["app.bsky.actor.profile", "town.delve.actor.profile"])

    def test_both_stamps_one_createdat_on_both_records(self):
        self.dt.write_face("both", None, description="new")
        stamps = {rec.get("createdAt") for _, rec in self.puts}
        self.assertEqual(len(stamps), 1)
        self.assertTrue(next(iter(stamps)).endswith("Z"))

    def test_both_sends_the_same_text_and_keeps_each_records_own_fields(self):
        self.dt.write_face("both", None, description="new")
        bsky, town = self.puts[0][1], self.puts[1][1]
        self.assertEqual((bsky["description"], town["description"]), ("new", "new"))
        self.assertEqual(bsky["labels"], {"values": [{"val": "bot"}]})     # the bsky record's own
        self.assertEqual(town["avatar"], {"ref": {"$link": "c"}})          # the town record's own

    def test_a_single_target_keeps_that_records_own_createdat(self):
        self.records["town.delve.actor.profile"]["createdAt"] = "2026-10-14T11:58:36.901Z"
        self.dt.write_face("town", None, description="new")
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.puts[0][1]["createdAt"], "2026-10-14T11:58:36.901Z")
        self.assertEqual(self.puts[0][1]["$type"], "town.delve.actor.profile")

    def test_a_clean_write_says_it_is_not_partial(self):
        out = self.dt.write_face("both", None, description="new")
        self.assertEqual((out["status"], out["partial"], out["failed"]), (200, False, []))

    def test_a_failed_side_is_not_reported_as_success(self):
        # The bsky record refuses and the town write lands, which is the shape that used to print
        # "status": 200 because the last write was the successful one.
        self.fail = {"app.bsky.actor.profile"}
        out = self.dt.write_face("both", None, description="new")
        self.assertTrue(out["partial"])
        self.assertEqual(out["status"], 400)
        self.assertEqual(out["failed"], ["app.bsky.actor.profile"])
        self.assertEqual(out["error"], "InvalidRequest")
        self.assertEqual([w["status"] for w in out["writes"]], [400, 200])

    def test_a_partial_write_leaves_a_non_zero_exit_code(self):
        self.fail = {"app.bsky.actor.profile"}
        out = self.dt.write_face("both", None, description="new")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit) as caught:
            self.dt.print_face(out)
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(json.loads(buffer.getvalue())["partial"])

    def test_a_clean_write_exits_zero(self):
        out = self.dt.write_face("both", None, description="new")
        with contextlib.redirect_stdout(io.StringIO()):
            self.dt.print_face(out)          # no SystemExit: the call must simply return


if __name__ == "__main__":
    unittest.main()
