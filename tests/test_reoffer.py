"""A batch whose run answered none of it is offered once more — bounded, so it cannot become churn.

2026-10-24: on both seats asks sat 45-60 minutes after a woken run that exited cleanly without answering
them; they were in `fired`, so nothing changed the gate's output until an operator fired it by hand. The
retry covers a run that failed; this covers one that ran and wrote nothing. The six cases are the brief's,
in its order. Offline: the state and the answered set are handed in.
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(ROOT, "delvetown_monitor.py")
T0 = 1792800000.0


def load():
    spec = importlib.util.spec_from_file_location("mon_reoffer", MONITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def note(rkey, reason="reply"):
    return {"reason": reason, "author": "selene.delve.town", "author_did": "did:plc:selene",
            "uri": "at://did:plc:selene/town.delve.feed.post/" + rkey, "text": "a question " + rkey,
            "indexed_at": "2026-10-24T02:00:00.000Z"}


class Reoffer(unittest.TestCase):

    def setUp(self):
        self.m = load()
        self.m.SETTLE_S, self.m.MAX_WAIT_S = 600, 2700

    def tick(self, st, now, items=None, answered=(), failed_at=None):
        return self.m.wake_output(items if items is not None else [], st, now, quiet=set(), replies=[],
                                  answered=set(answered), failed_at=failed_at)

    def fire(self, rkeys, at=T0):
        items = [note(r) for r in rkeys]
        _, st = self.tick({}, at - 600, items)
        out, st = self.tick(st, at, items)
        self.assertTrue(out.startswith("wake=%d" % len(rkeys)), out)
        return out, st

    def test_a_batch_whose_run_answered_nothing_is_reoffered_once_after_the_grace(self):
        first, st = self.fire(["a", "b"])
        early, st = self.tick(st, T0 + self.m.REOFFER_AFTER_S - 1)
        self.assertEqual(early, first, "not before the grace: a slow run is not a dead one")
        out, st = self.tick(st, T0 + self.m.REOFFER_AFTER_S)
        self.assertTrue(out.startswith("wake=2 reoffer id="), out)
        self.assertIn("a: a question a", out)
        self.assertIn("dt.py decline", out)

    def test_a_batch_whose_run_answered_one_item_is_never_reoffered(self):
        first, st = self.fire(["a", "b", "c"])
        for now in range(int(T0), int(T0 + self.m.REOFFER_WINDOW_S + 600), 300):
            out, st = self.tick(st, now, answered=[note("b")["uri"]])
            self.assertEqual(out, first, "one answer: the run read the batch and judged the rest")

    def test_a_decline_counts_as_the_run_having_judged(self):
        first, st = self.fire(["a"])
        out, _ = self.tick(st, T0 + self.m.REOFFER_AFTER_S, answered=[note("a")["uri"]])
        self.assertEqual(out, first)

    def test_an_item_older_than_the_window_is_never_reoffered(self):
        first, st = self.fire(["a"])
        out, st = self.tick(st, T0 + self.m.REOFFER_WINDOW_S + 1)
        self.assertEqual(out, first, "history stays parked")
        self.assertEqual(st["recent"], [], "and is pruned from the state")

    def test_an_item_is_reoffered_at_most_once(self):
        _, st = self.fire(["a"])
        out, st = self.tick(st, T0 + self.m.REOFFER_AFTER_S)
        self.assertIn("reoffer", out)
        for now in range(int(T0 + self.m.REOFFER_AFTER_S), int(T0 + self.m.REOFFER_WINDOW_S), 300):
            again, st = self.tick(st, now)
            self.assertEqual(again, out, "the same bytes: no second wake")

    def test_the_daily_cap_holds(self):
        """Thirteen candidates, twelve wakes: thirteen silent batches ten minutes apart."""
        st = {"recent": [{"at": T0 + i * 600, "items": [note("x%d" % i)]} for i in range(13)]}
        outs = []
        for now in range(int(T0 + self.m.REOFFER_AFTER_S), int(T0 + 12 * 600 + self.m.REOFFER_AFTER_S + 1), 600):
            out, st = self.tick(st, now)
            outs.append(out)
        self.assertEqual(len(st["reoffers"]), self.m.REOFFER_MAX)
        self.assertEqual(len(set(outs)), self.m.REOFFER_MAX, "twelve changes of output, then none")
        self.assertNotIn(note("x12")["uri"], st["reoffered"])

    def test_an_old_state_file_loads_and_behaves_as_today(self):
        """No `recent`, `reoffered` or `reoffers` keys: the history it holds is never re-offered."""
        old = {"fired": {note("a")["uri"]: T0}, "pending": {}, "last": "wake=1 reply=1 id=x\nold",
               "base": "wake=1 reply=1 id=x\nold", "fired_at": T0, "batch": [note("a")["uri"]]}
        out, st = self.tick(old, T0 + self.m.REOFFER_AFTER_S + 60)
        self.assertEqual(out, old["last"])
        self.assertEqual((st["recent"], st["reoffered"], st["reoffers"]), ([], {}, []))

    def test_a_failed_run_is_the_retrys_not_the_reoffers(self):
        first, st = self.fire(["a"])
        self.m.RETRY_MAX = 0                             # the retry has given up; the operator was alerted
        out, _ = self.tick(st, T0 + self.m.REOFFER_AFTER_S, failed_at=T0 + 60)
        self.assertEqual(out, first)

    def test_the_next_wake_says_how_the_previous_batch_went(self):
        _, st = self.fire(["a", "b", "c"])
        items = [note("d")]
        _, st = self.tick(st, T0 + 300, items, answered=[note("a")["uri"], note("b")["uri"]])
        out, st = self.tick(st, T0 + 900, items, answered=[note("a")["uri"], note("b")["uri"]])
        self.assertIn("previous batch of 3: 2 answered, 0 declined, 0 abstained, 1 untouched", out)


if __name__ == "__main__":
    unittest.main()
