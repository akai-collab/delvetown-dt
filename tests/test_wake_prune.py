"""An item answered outside the gate must stop looking queued.

The gate's state keeps a `pending` map of items it is holding before answering. When a session or a
clock wake answers one of those items instead, the gate kept listing it as waiting — on 2026-10-23
three kaiasky items sat in that queue after being answered at 09:40Z, 10:32Z and 08:29Z. Nothing marks
notifications seen (`dt.py seen` is still unbuilt), so the queue has to be pruned where the answer is
published.
"""

import json
import pathlib
import sys

import pytest

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


def test_the_answered_item_leaves_the_queue(tmp_path):
    p = state_file(tmp_path)
    assert dt.prune_pending(PARENT, path=str(p)) is True
    after = json.loads(p.read_text())
    assert PARENT not in after["pending"]
    assert after["fired"], "the fired map is the de-duplication guard: it must survive a prune"
    assert after["last"] == "a brief" and after["newest"] == 1792744272.0


def test_another_item_is_left_alone(tmp_path):
    other = "at://did:plc:x/town.delve.feed.post/3myabc"
    p = state_file(tmp_path, pending={PARENT: {"t": 1.0}, other: {"t": 2.0}})
    assert dt.prune_pending(PARENT, path=str(p)) is True
    after = json.loads(p.read_text())
    assert list(after["pending"]) == [other]


def test_nothing_to_prune_is_not_an_error(tmp_path):
    p = state_file(tmp_path, pending={})
    assert dt.prune_pending(PARENT, path=str(p)) is False


def test_a_missing_state_file_is_not_an_error(tmp_path):
    assert dt.prune_pending(PARENT, path=str(tmp_path / "absent.json")) is False


def test_a_corrupt_state_file_is_not_an_error(tmp_path):
    p = tmp_path / "wake-akai.json"
    p.write_text("{not json")
    assert dt.prune_pending(PARENT, path=str(p)) is False


def test_the_write_is_atomic_and_leaves_no_temp(tmp_path):
    p = state_file(tmp_path)
    dt.prune_pending(PARENT, path=str(p))
    assert not (tmp_path / "wake-akai.json.tmp").exists()
    json.loads(p.read_text())   # still valid JSON


def test_the_path_follows_the_environment(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"pending": {PARENT: {"t": 1.0}}, "fired": {}}))
    monkeypatch.setenv("AT_MCP_WAKE_STATE", str(explicit))
    assert dt.wake_state_path() == str(explicit)
    assert dt.prune_pending(PARENT) is True
