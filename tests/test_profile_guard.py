"""A flag is not profile text: `dt bio --help` must refuse before it writes anything.

On 2026-11-09 a run from the card-drawer's session executed `dt bio --help`; dt.py fell back to akai's
identity and published the literal string "--help" as this account's town bio. These tests run the real
CLI with no network: the refusal happens before any request is made.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")


def run(*args, env=None):
    e = dict(os.environ)
    e.pop("DT_ACCOUNT", None)
    e.pop("DT_HANDLE", None)
    e.pop("DT_DID", None)
    e.update(env or {})
    return subprocess.run([sys.executable, DT, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env=e)


class Refuses(unittest.TestCase):

    def test_bio_help_is_refused_not_written(self):
        r = run("bio", "--help")
        self.assertNotEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertIn("refused", out)
        self.assertIn("--help", out["refused"])

    def test_displayname_flag_is_refused(self):
        r = run("displayname", "-v")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", json.loads(r.stdout))

    def test_profile_flags_are_refused(self):
        r = run("profile", "--help", "a bio")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", json.loads(r.stdout))

    def test_ordinary_text_still_passes_the_guard(self):
        # No network here: the run must fail later, at the record read, and never at the guard.
        r = run("bio", "a plain sentence")
        if r.returncode != 0:
            self.assertNotIn("refused", r.stdout or "")


class Warns(unittest.TestCase):

    def test_a_default_identity_write_says_whose_profile_it_is(self):
        r = run("bio", "a plain sentence")
        self.assertIn("default identity", r.stderr)


if __name__ == "__main__":
    unittest.main()
