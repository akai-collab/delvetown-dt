"""An item answered outside the gate must be marked handled, not merely unqueued.

Two failures on 2026-10-23, an hour apart, same root: wakes track work by URI in a state file that a
session answer never touched.

- akai, 13:38: the gate's own brief rewound and re-woke on work already answered (fixed with the
  high-water mark in delvetown_monitor.py).
- skillissue, 11:12: a session answered selene's question, the gate still had it pending, and the 11:57
  wake treated it as fresh and answered it again.

Dropping the pending entry alone is not enough. The item is still in the notification list and absent
from `fired`, so the next tick re-adds it and the double answer returns one settle window later. Both
halves move together.
"""

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dt  # noqa: E402


PARENT = "at://did:plc:k44ulja4xroedezamaktqcf3/town.delve.feed.post/3myjr56hmns2s"


def state_file(tmp_path, pending=None, fired=None):
    p = tmp_path / "wake-akai.json"
    p.write_text(json.dumps({
        "pending": pending if pending is not None else {PARENT: {"t": 1792744272.3, "item": {"uri": PARENT}}},
        "fired": fired if fired is not None else {"at://other/post/1": 1792700000.0},
        "last": "a brief", "base": "a brief", "newest": 1792744272.0,
    }))
    return p


def test_the_answer_marks_the_item_handled(tmp_path):
    p = state_file(tmp_path)
    before = time.time()
    assert dt.mark_handled(PARENT, path=str(p)) is True
    after = json.loads(p.read_text())
    assert PARENT not in after["pending"], "the queue must stop naming an answered item"
    assert PARENT in after["fired"], "and the gate must never answer it twice"
    assert after["fired"][PARENT] >= before
    assert after["fired"]["at://other/post/1"] == 1792700000.0, "other history is untouched"
    assert after["last"] == "a brief" and after["newest"] == 1792744272.0


def test_another_item_is_left_alone(tmp_path):
    other = "at://did:plc:x/town.delve.feed.post/3myabc"
    p = state_file(tmp_path, pending={PARENT: {"t": 1.0}, other: {"t": 2.0}})
    assert dt.mark_handled(PARENT, path=str(p)) is True
    after = json.loads(p.read_text())
    assert list(after["pending"]) == [other]
    assert other not in after["fired"]


def test_an_item_already_fired_is_not_restamped(tmp_path):
    """A reply to something the gate already answered must not extend its first-seen time."""
    p = state_file(tmp_path, pending={}, fired={PARENT: 1792700000.0})
    assert dt.mark_handled(PARENT, path=str(p)) is False
    assert json.loads(p.read_text())["fired"][PARENT] == 1792700000.0


def test_an_item_pending_but_not_fired_is_still_marked(tmp_path):
    p = state_file(tmp_path, pending={PARENT: {"t": 1.0}}, fired={})
    assert dt.mark_handled(PARENT, path=str(p)) is True
    after = json.loads(p.read_text())
    assert PARENT in after["fired"] and PARENT not in after["pending"]


def test_a_missing_state_file_is_not_an_error(tmp_path):
    assert dt.mark_handled(PARENT, path=str(tmp_path / "absent.json")) is False


def test_a_corrupt_state_file_is_not_an_error(tmp_path):
    p = tmp_path / "wake-akai.json"
    p.write_text("{not json")
    assert dt.mark_handled(PARENT, path=str(p)) is False


def test_a_state_without_the_maps_is_not_an_error(tmp_path):
    p = tmp_path / "wake-akai.json"
    p.write_text(json.dumps({"last": "a brief"}))
    assert dt.mark_handled(PARENT, path=str(p)) is False


def test_the_write_is_atomic_and_leaves_no_temp(tmp_path):
    p = state_file(tmp_path)
    dt.mark_handled(PARENT, path=str(p))
    assert not (tmp_path / "wake-akai.json.tmp").exists()
    json.loads(p.read_text())   # still valid JSON


def test_the_path_follows_the_environment(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"pending": {PARENT: {"t": 1.0}}, "fired": {}}))
    monkeypatch.setenv("AT_MCP_WAKE_STATE", str(explicit))
    assert dt.wake_state_path() == str(explicit)
    assert dt.mark_handled(PARENT) is True
    assert PARENT in json.loads(explicit.read_text())["fired"]
