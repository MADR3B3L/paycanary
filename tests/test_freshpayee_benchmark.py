"""Standing guard: the O9 fresh-payee benchmark must stay green — every cheap/lazy mule caught, zero false-nags on the
legit-new set (the cold-start acceptance bar), and the bought-clean-footprint residue honestly uncaught. Runs the real
tools/score_freshpayee.py over the generated corpus."""
import contextlib
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FreshPayeeBenchmark(unittest.TestCase):
    def test_mules_caught_zero_false_nags_residue_honest(self):
        # regenerate the corpus deterministically, then score
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_fp", ROOT / "tests" / "corpus" / "make_freshpayee_corpus.py")
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        from tools import score_freshpayee as S
        with contextlib.redirect_stdout(io.StringIO()):
            rc = S.main()
        self.assertEqual(rc, 0)   # 9/9 mules caught, 0/6 legit false-nags, residue uncaught as expected


if __name__ == "__main__":
    unittest.main()
