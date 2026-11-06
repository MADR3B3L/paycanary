"""Standing real-world benchmark: regenerate the documented-pattern corpus and score it through the real Service.pay.
Unlike the synthetic benchmark's hard PASS gate, this one RECORDS the current numbers and asserts only the invariants we
have decided hold (recall floor, and that the two known false-nag causes are the ONLY ones) so a regression that adds a NEW
false-nag class, or drops recall, trips the test. The two known false-nag causes (multi-currency flat hard-cap; fast/slow
divergence on a true-up-then-growth sequence) are reported to infra; when fixed, tighten the asserts."""
import contextlib
import io
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _regen():
    spec = importlib.util.spec_from_file_location("make_rw", ROOT / "tests" / "corpus" / "make_realworld_corpus.py")
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


class RealWorldBenchmark(unittest.TestCase):
    def test_numbers_and_known_false_nag_causes(self):
        _regen()
        from tools import score_realworld_service as S
        res = S.run(enrich=True)        # enrichment ON = the shipped recommendation (the contextual fan-out needs it)
        labels = json.loads((ROOT / "tests" / "corpus" / "realworld_labels.json").read_text())
        benign = [x for x in labels if x["label"] == "benign"]
        attacks = [x for x in labels if x["label"] != "benign"]
        caught = sum(res[x["tid"]][1] == "frozen" for x in attacks)
        nags = [x for x in benign if res[x["tid"]][1] == "frozen"]

        # recall floor: the real-shaped corpus should not drop below the synthetic 73.7%
        self.assertGreaterEqual(caught / len(attacks), 0.73)

        # ZERO false-nags now (both earlier causes resolved: the "multi-currency" ones were a corpus-generator bug, not a
        # gate gap -- the gate FX-normalizes via the service-injected fx rate; the divergence spike was softened to a
        # review for trusted payees). Any benign freeze now is a REGRESSION to chase, not an accepted cause.
        self.assertEqual(nags, [], "benign false-nag(s): %s" % [res[x["tid"]][2][:80] for x in nags])

        # precision: every freeze on this corpus is a real attack (no benign freezes at all)
        lab = {x["tid"]: x["label"] for x in labels}
        self.assertTrue(all(lab[t] != "benign" for t, v in res.items() if v[1] == "frozen"))


if __name__ == "__main__":
    unittest.main()
