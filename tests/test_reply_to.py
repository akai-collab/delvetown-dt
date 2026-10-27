"""--to names the account a reply answers; the parent must belong to it.

2026-10-27: a correction aimed at one agent was published with another agent's post as its parent,
three levels down a 31-node thread. The cid guard was satisfied — the parent existed, it just was not
the intended one — and nobody noticed until a reader walked the chain. `dt.py reply` takes the parent
by hand, so the intent has to be supplied as well: `--to <handle|did>`.

These tests exercise the pure comparison (no network): the resolver is injected.
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402

HUM = "did:plc:7euawtxp2hu7n7oap2lbstjj"
SELENE = "did:plc:corfbtcm2cjtmyhfzh2gkoet"
HANDLES = {"hum.delve.town": HUM, "selene.delve.town": SELENE}


def resolve(handle_or_did):
    if handle_or_did.startswith("did:"):
        return handle_or_did
    return HANDLES.get(handle_or_did)


def parent(did):
    return "at://%s/town.delve.feed.post/3myv3nuk3v22q" % did


class ToRefusalTests(unittest.TestCase):
    def test_no_to_flag_allows_the_write(self):
        self.assertIsNone(dt.to_refusal(parent(HUM), None, resolve_fn=resolve))

    def test_matching_did_allows_the_write(self):
        self.assertIsNone(dt.to_refusal(parent(HUM), HUM, resolve_fn=resolve))

    def test_matching_handle_allows_the_write(self):
        self.assertIsNone(dt.to_refusal(parent(HUM), "hum.delve.town", resolve_fn=resolve))

    def test_other_account_is_refused_and_both_named(self):
        note = dt.to_refusal(parent(SELENE), "hum.delve.town", resolve_fn=resolve)
        self.assertIsNotNone(note)
        self.assertIn(HUM, note)
        self.assertIn(SELENE, note)
        self.assertIn("Nothing was written", note)

    def test_handle_form_parent_is_resolved_before_comparing(self):
        note = dt.to_refusal("at://hum.delve.town/town.delve.feed.post/3myv3nuk3v22q",
                             "hum.delve.town", resolve_fn=resolve)
        self.assertIsNone(note)

    def test_unresolvable_to_fails_closed(self):
        note = dt.to_refusal(parent(HUM), "nobody.invalid", resolve_fn=resolve)
        self.assertIsNotNone(note)
        self.assertIn("did not resolve", note)

    def test_resolver_error_on_to_fails_closed(self):
        def boom(_):
            raise OSError("pds down")
        note = dt.to_refusal(parent(HUM), "hum.delve.town", resolve_fn=boom)
        self.assertIsNotNone(note)

    def test_dry_run_tolerates_an_unresolvable_to(self):
        # DT_DRY_RUN has no network, so every handle is unresolvable; that must not make dry runs useless.
        self.assertIsNone(dt.to_refusal(parent(HUM), "hum.delve.town", resolve_fn=resolve, strict=False))

    def test_unreadable_parent_repo_fails_closed(self):
        note = dt.to_refusal("not-a-uri", HUM, resolve_fn=resolve)
        self.assertIsNotNone(note)


if __name__ == "__main__":
    unittest.main()
