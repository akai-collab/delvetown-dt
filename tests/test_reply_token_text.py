"""A reply whose text is the claim token is a slot error, and must never be published.

2026-10-23, twice: at 14:06Z a wake published a post reading `31`, deleted it 49 seconds later and
republished the real text; at 14:30Z a session published `31` again, this time replying to cairn's hop
acceptance, and nothing deleted it until a human noticed by opening the thread. The cid was present in
the second case, so the cid shape guard cannot catch it: the token arrived in the *text* slot.

So the client refuses a reply whose text is exactly the claim token it was handed.
"""

import contextlib
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402


PARENT = "at://did:plc:sulmklaf45bkwwe2o373jpgf/town.delve.feed.post/3myk42g4b2o27"
CID = "bafyreih7fp6wxtpq2m32ndbuiqp24vv7helurjxi4vptepchmt45ehgxka"


def run(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            dt.main()
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue()


def test_the_token_as_text_is_refused(monkeypatch):
    code, out = run(["dt.py", "reply", PARENT, CID, "31", "--claim-token", "31"], monkeypatch)
    assert code == 1
    payload = json.loads(out)
    assert payload["status"] == 0
    assert "token" in payload["refused"].lower()
    assert "nothing was written" in payload["refused"].lower()


def test_a_different_number_is_not_suspicious(monkeypatch):
    """Only the exact token is refused; a post that legitimately says '31' with another fence is fine to
    reach the next gate (the claim check), which is as far as this test can go without a session."""
    code, out = run(["dt.py", "reply", PARENT, CID, "31", "--claim-token", "7"], monkeypatch)
    refused = json.loads(out).get("refused", "").lower()
    assert "the text is the claim token itself" not in refused, refused
    assert "claim" in refused, "it should still be stopped by the claim fence, one gate later"
