import contextlib, io, unittest
from app import gate as G
from tests.gate_patch import gate_overrides
from tools import score as S


def run_quiet():
    with contextlib.redirect_stdout(io.StringIO()):
        return S.main()


class Scorer(unittest.TestCase):
    def test_gate_passes_the_corpora(self):
        self.assertEqual(run_quiet(), 0)

    def test_the_gauge_can_fail(self):                      # a scorer that cannot fail proves nothing
        with gate_overrides(VELOCITY_N=10**6, BURST_MULT=1e9, DUP_WINDOW_S=0.0, UNTRUSTED_PAYEE_BUDGET_MULT=1e9, OTHER_PAYEE_MULT=1e9):
            self.assertEqual(run_quiet(), 1)                # no burst-type rule at all (velocity, drain, replay) => rapid-fire recall collapses
        with gate_overrides(AMOUNT_Z_FREEZE=10**6, MAX_MULT_FREEZE=10**6, RATCHET_FREEZE_MULT=10**6, UNTRUSTED_FREEZE_MULT=10**6, DIVERGE_FREEZE=10**6, OTHER_PAYEE_MULT=10**6, BURST_MULT=10**6):
            self.assertEqual(run_quiet(), 1)                # no amount rule => amount-spike recall collapses

    def test_a_trigger_happy_gate_trips_the_false_nag_gauge(self):
        with gate_overrides(AMOUNT_Z_FREEZE=0.3, RATCHET_FREEZE_MULT=1.01, UNTRUSTED_FREEZE_MULT=1.01):
            self.assertEqual(run_quiet(), 1)


if __name__ == "__main__":
    unittest.main()
