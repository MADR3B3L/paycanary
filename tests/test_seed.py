import unittest
from app import seed as S
from app.store import Store


class Seed(unittest.TestCase):
    def test_seed_populates_a_queue_with_real_freezes_waiting_for_a_human(self):
        st, pp = Store(), S.OfflinePayPal()
        res = S.seed(st, pp, now=1_800_000_000.0)
        rows = st.payments(None, None, 500)
        self.assertTrue(res["seeded"]); self.assertEqual(len(rows), 72)
        frozen = [r for r in rows if r["status"] == "frozen"]
        self.assertEqual(sorted(r["payee"] for r in frozen), ["Priya Raman", "QuickTools LLC", "Veloxa Payments", "Veloxa Payments"])
        self.assertTrue(all(r["paypal_batch_id"] is None for r in frozen))                 # no PayPal call for a freeze
        self.assertEqual(pp.n, len(rows) - len(frozen))
        self.assertGreaterEqual(sum(1 for r in rows if r["verdict"] == "review"), 3)
        self.assertGreaterEqual(sum(1 for r in rows if r["verdict"] == "allow"), 40)
        self.assertLess(1_800_000_000.0 - max(r["ts"] for r in rows), 1500)                  # last row ~18 min old: live traffic is not velocity-tripped

    def test_seed_is_idempotent_unless_forced(self):
        st = Store(); S.seed(st, now=1_800_000_000.0)
        self.assertFalse(S.seed(st, now=1_800_000_000.0)["seeded"]); self.assertEqual(len(st.payments(None, None, 500)), 72)


if __name__ == "__main__":
    unittest.main()
