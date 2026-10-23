"""A wake must report the Quiet Year, never answer in it.

2026-10-23: the rule was in both wake prompts, in as many words, with "two exceptions and no third". A wake
answered anyway, because the thread it answered into did not carry the #quietyear tag — replies never do — so
the prompt-level rule never fired, and the game's author had to work out which of two akai voices had spoken.
A prompt cannot inspect a thread. This checks the thread root before the reply is sent.
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "dt.py")

GAME_ROOT = "at://did:plc:ooaz7zdveqi6zqzs5lukuysm/town.delve.feed.post/3myl66tvzy2v5"
OTHER_ROOT = "at://did:plc:corfbtcm2cjtmyhfzh2gkoet/town.delve.feed.post/3myky7qzrfw27"


def load():
    spec = importlib.util.spec_from_file_location("dt_qy", DT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QuietYearGuard(unittest.TestCase):
    def setUp(self):
        self.dt = load()

    def test_a_thread_root_carrying_the_tag_is_refused(self):
        why = self.dt.quietyear_refusal(GAME_ROOT, fetch=lambda u: {"text": "SETUP DISCUSSION — THE QUIET YEAR\n\n#quietyear @kaiasky"})
        self.assertIn("Quiet Year thread", why)
        self.assertIn("--in-quietyear", why)

    def test_a_root_without_the_tag_goes_through(self):
        self.assertIsNone(self.dt.quietyear_refusal(OTHER_ROOT, fetch=lambda u: {"text": "How the town answered, one answer per account."}))

    def test_an_unreadable_root_does_not_block_the_reply(self):
        """Best effort: a PDS that will not answer must not silence every reply."""
        def boom(_u):
            raise RuntimeError("no network")
        self.assertIsNone(self.dt.quietyear_refusal(GAME_ROOT, fetch=boom))

    def test_no_root_at_all_is_not_a_refusal(self):
        self.assertIsNone(self.dt.quietyear_refusal(None, fetch=lambda u: {"text": "#quietyear"}))

    def test_the_flag_is_known_to_the_reply_flag_guard(self):
        self.assertIn("--in-quietyear", self.dt.ALLOWED_FLAGS["reply"])


if __name__ == "__main__":
    unittest.main()
