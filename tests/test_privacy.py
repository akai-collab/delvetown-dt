"""The pre-flight must refuse a draft carrying one of the operator's private identifiers.

2026-10-22: an addendum post quoted an apt `Requested-By` line, which carries the host's Unix
account name and uid. The daily local scanner looks for the operator's *name*, so a different
string walked past it, and no compose-time rule asked the question at all. The list lives outside
this repo (~/.hermes/scripts/privacy-needles.txt, base64) because the repo is publishable; these
tests pin the reader and the refusal without ever writing a real identifier into the repo.
"""
import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dt  # noqa: E402

NEEDLE = "examplelogin"
PATH_NEEDLE = "/home/" + NEEDLE


def b64(s):
    return base64.b64encode(s.encode()).decode()


class PrivacyNeedles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        self.tmp.write("# comment\n")
        self.tmp.write("hard:%s\n" % b64(NEEDLE))
        self.tmp.write("path:%s\n" % b64(PATH_NEEDLE))
        self.tmp.write("warn:%s\n" % b64("Requested-By"))
        self.tmp.close()
        self._old = dt.PRIVACY_NEEDLES_FILE
        dt.PRIVACY_NEEDLES_FILE = self.tmp.name

    def tearDown(self):
        dt.PRIVACY_NEEDLES_FILE = self._old
        os.unlink(self.tmp.name)

    def test_reader_takes_kinds_and_skips_comments(self):
        got = dt.privacy_needles()
        self.assertEqual([k for k, _ in got], ["hard", "path", "warn"])

    def test_absent_file_means_no_check(self):
        dt.PRIVACY_NEEDLES_FILE = self.tmp.name + ".missing"
        self.assertEqual(dt.privacy_needles(), [])
        self.assertEqual(dt.privacy_hits("anything at all"), ([], []))

    def test_hard_needle_refuses_and_does_not_echo(self):
        hard, warn = dt.privacy_hits("the host was %s (1000)" % NEEDLE)
        self.assertEqual(len(hard), 1)
        self.assertNotIn(NEEDLE, hard[0])          # the wake reads its own stdout
        self.assertIn("█", hard[0])
        self.assertEqual(warn, [])

    def test_path_needle_refused_once_not_twice(self):
        hard, _ = dt.privacy_hits("state lives under %s/.hermes" % PATH_NEEDLE)
        self.assertEqual(len(hard), 1)             # the account hit inside the path is the same span
        self.assertNotIn(NEEDLE, hard[0])

    def test_a_window_never_quotes_a_second_identifier(self):
        # 2026-10-26: masking only the needle being reported let a *second* identifier sit in clear
        # inside the window — the refusal quoted an account name beside a masked domain, and that
        # message goes into a wake's stdout. Every occurrence is masked before any window is cut.
        with mock.patch.object(dt, "privacy_needles",
                               return_value=[("hard", NEEDLE), ("warn", "examplehost.local")]):
            hard, warn = dt.privacy_hits("sent from %s@examplehost.local at noon" % NEEDLE)
        msgs = hard + warn
        self.assertEqual(len(msgs), 2, msgs)        # two separate, adjacent spans
        for m in msgs:
            self.assertNotIn(NEEDLE, m)

    def test_an_overlapping_pair_reports_the_strictest_kind(self):
        # a warn needle that contains a hard one must not downgrade it: the merged span is hard
        with mock.patch.object(dt, "privacy_needles",
                               return_value=[("warn", "id-" + NEEDLE), ("hard", NEEDLE)]):
            hard, warn = dt.privacy_hits("draft id-%s here" % NEEDLE)
        self.assertEqual(len(hard), 1, (hard, warn))
        self.assertEqual(warn, [])
        self.assertNotIn(NEEDLE, hard[0])

    def test_warn_kind_only_warns(self):
        hard, warn = dt.privacy_hits("filed by Requested-By line")
        self.assertEqual(hard, [])
        self.assertEqual(len(warn), 1)

    def test_clean_draft_passes(self):
        self.assertEqual(dt.privacy_hits("a perfectly ordinary sentence"), ([], []))

    def test_check_text_carries_the_refusal(self):
        hard, _ = dt.check_text("the host was %s" % NEEDLE, verbose=False, online=False)
        self.assertTrue(any("private identifier" in h for h in hard))


if __name__ == "__main__":
    unittest.main()
