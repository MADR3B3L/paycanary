"""Standing persona x attacker-tier matrix: the honest gradient a partner fraud team can trust, not a blended number.
Asserts the invariants: ZERO false-nags across ALL 8 lifestyle personas (the product-survival metric, and the selling
point for remittance/gig/student/HNW that naive systems abuse); dumb-dumb and dumb fully FROZEN; and NO silent-allow
through the 'smart' tier (medium + smart are at least flagged -- only 'way-smarter', the irreducible floor, slips)."""
import contextlib, io, importlib.util, json, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def _regen():
    spec = importlib.util.spec_from_file_location("make_p", ROOT / "tests" / "corpus" / "make_persona_corpus.py")
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


class PersonaMatrix(unittest.TestCase):
    def test_zero_false_nags_every_persona_and_the_honest_attacker_gradient(self):
        _regen()
        from tools import score_persona_service as S
        res = S.run()
        labels = json.loads((ROOT / "tests" / "corpus" / "persona_labels.json").read_text())
        frozen = lambda t: res[t][1] == "frozen"
        review = lambda t: res[t][0] == "review"

        benign = [x for x in labels if x["label"].startswith("benign:")]
        by_tier = {}
        for x in labels:
            if x["label"].startswith("attack:"):
                by_tier.setdefault(x["label"].split(":", 1)[1], []).append(x)

        # 0 false-nags across EVERY persona
        nags = [x for x in benign if frozen(x["tid"])]
        self.assertEqual(nags, [], "benign false-nags: %s" % [res[x["tid"]][2][:70] for x in nags])

        # dumb-dumb and dumb are fully frozen (we crush the obvious ones)
        for tier in ("dumb-dumb", "dumb"):
            self.assertTrue(all(frozen(x["tid"]) for x in by_tier[tier]), "%s not fully frozen" % tier)

        # NO silent-allow through 'smart' (medium + smart are at least flagged; only way-smarter slips)
        for tier in ("medium", "smart"):
            silent = [x for x in by_tier[tier] if not frozen(x["tid"]) and not review(x["tid"])]
            self.assertEqual(silent, [], "%s has silent-allows: %s" % (tier, [res[x['tid']][2][:60] for x in silent]))

        # way-smarter is the HONEST FLOOR: it is allowed to have silent misses (we do not over-claim). Just assert it is
        # not somehow fully caught (which would mean the corpus isn't actually modeling the patient attacker).
        ws = by_tier["way-smarter"]
        silent_ws = [x for x in ws if not frozen(x["tid"]) and not review(x["tid"])]
        self.assertTrue(silent_ws, "way-smarter fully caught -- corpus not modeling the patient floor honestly")


if __name__ == "__main__":
    unittest.main()
