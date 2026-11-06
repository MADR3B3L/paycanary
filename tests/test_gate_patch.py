import unittest

from app import gate as G
from tests.gate_patch import gate_overrides


class GatePatch(unittest.TestCase):
    def test_restores_constants_and_constructor_defaults_even_when_the_body_raises(self):
        before = (G.PROBATION_ABS, G.AMOUNT_Z_FREEZE, G.History.__init__.__defaults__)
        with self.assertRaises(RuntimeError):
            with gate_overrides(PROBATION_ABS=1.0, AMOUNT_Z_FREEZE=0.1):
                self.assertEqual(G.History().probation_abs, 1.0)            # default arguments follow the override, by parameter name
                self.assertEqual(G.PROBATION_ABS, 1.0)
                raise RuntimeError("boom")
        self.assertEqual((G.PROBATION_ABS, G.AMOUNT_Z_FREEZE, G.History.__init__.__defaults__), before)
        self.assertEqual(G.History().probation_abs, before[0])

    def test_unrelated_defaults_that_happen_to_equal_the_overridden_value_are_left_alone(self):
        d = G.History.__init__.__defaults__
        with gate_overrides(PROBATION_MULT=99.0):
            h = G.History()
            self.assertEqual(h.probation_mult, 99.0)
            self.assertEqual(h.probation_abs, G.PROBATION_ABS)
            self.assertEqual(h.new_payee_floor, G.NEW_PAYEE_FLOOR)
        self.assertEqual(G.History.__init__.__defaults__, d)


if __name__ == "__main__":
    unittest.main()
