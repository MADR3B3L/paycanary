import os, unittest
from app import fx
from app.service import Service, fmt_amount
from app.store import Store
from tests.test_service import FakePayPal

NOW = 1_800_000_000.0
DAY = 86400.0


def mk(cap=20000.0, **kw):
    t = [NOW]; pp = FakePayPal()
    s = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=cap, **kw)
    return s, pp, t


def hist(s, payee, currency, amounts, agent="a", email=None):
    for i, a in enumerate(amounts):
        s.store.insert_payment({"payment_id": "%s%d" % (payee, i), "agent_id": agent, "ts": NOW - (60 - 5 * i) * DAY, "payee": payee, "recipient": email or payee.lower() + "@h.example",
                                "amount": float(a), "currency": currency, "memo": "", "hour": 10, "intent_id": None, "verdict": "allow", "score_kind": "none", "reason": "h",
                                "status": "sent", "paypal_batch_id": "H", "paypal_status": "S", "decided_by": "h", "event_json": "{}", "narration": None, "risk": 0, "llm_flag": None,
                                "mode": "enforce", "enforced": 1, "release_at": None})


def pay(s, payee, amount, currency, email=None, agent="a"):
    return s.pay({"agent_id": agent, "payee": payee, "recipient_email": email or payee.lower() + "@h.example", "amount": str(amount), "currency": currency, "memo": ""})


class HardCapIsInUsd(unittest.TestCase):
    def test_a_520000_yen_payment_is_not_frozen_by_a_20000_dollar_cap(self):
        s, pp, t = mk(); hist(s, "Tokyo", "JPY", [500000, 520000, 480000, 510000, 495000, 505000, 515000, 490000])
        out = pay(s, "Tokyo", 520000, "JPY")
        self.assertNotIn("hard cap", out["reasons"][0]); self.assertNotEqual(out["status"], "frozen"); self.assertEqual(len(pp.calls), 1)
        self.assertEqual(pp.calls[0][1], "520000"); self.assertEqual(pp.calls[0][2], "JPY")                      # PayPal gets a whole-number yen amount

    def test_the_same_payment_above_the_usd_equivalent_of_the_cap_still_freezes_and_says_so(self):
        s, pp, t = mk(); hist(s, "Tokyo", "JPY", [500000, 520000, 480000, 510000, 495000, 505000, 515000, 490000])
        out = pay(s, "Tokyo", 4_000_000, "JPY")                                                                  # about $26,800 > $20,000
        self.assertEqual(out["status"], "frozen"); self.assertIn("hard cap", out["reasons"][0]); self.assertIn("JPY", out["reasons"][0]); self.assertIn("~$", out["reasons"][0])
        self.assertEqual(pp.calls, [])

    def test_euros_and_pounds_use_their_own_rates(self):
        s, pp, t = mk(cap=1000.0); hist(s, "EuroCo", "EUR", [800, 820, 790, 810, 805, 795, 815, 800]); hist(s, "UkCo", "GBP", [780, 790, 800, 770, 785, 795, 780, 790])
        self.assertIn("hard cap", pay(s, "EuroCo", 950, "EUR")["reasons"][0])          # 950 EUR ~ $1,026 > $1,000
        self.assertNotIn("hard cap", pay(s, "UkCo", 700, "GBP")["reasons"][0])         # 700 GBP ~ $889 < $1,000, though 700 raw would not matter either way
        self.assertIn("hard cap", pay(s, "UkCo", 790, "GBP")["reasons"][0])           # 790 GBP ~ $1,003 > $1,000 (both are judged in USD, not as raw numbers)


class CumulativeWindowsPoolAcrossCurrencies(unittest.TestCase):
    def test_usd_and_euro_spend_add_up_toward_the_same_hourly_window(self):
        s, pp, t = mk(cap=300.0)                                   # hourly window = 2 x cap = $600
        s.set_agent("a", hard_cap=300, approved_payees=["U", "E"])
        for i, (p, a, c) in enumerate([("U", 250, "USD"), ("E", 200, "EUR")]):          # $250 + EUR 200 (~$216) = $466
            t[0] += 120; self.assertNotEqual(pay(s, p, a, c)["status"], "frozen", (p, c))
        t[0] += 120; out = pay(s, "E", 150, "EUR")                                       # +~$162 = $628 > $600
        self.assertEqual(out["status"], "frozen"); self.assertIn("structuring guard", out["reasons"][0])


class BaselinesStayPerCurrency(unittest.TestCase):
    def test_yen_history_does_not_make_a_normal_dollar_payment_look_tiny_or_huge(self):
        s, pp, t = mk(); hist(s, "Tokyo", "JPY", [500000] * 8); hist(s, "Acme", "USD", [55, 60, 58, 62, 57, 59, 61, 60])
        out = pay(s, "Acme", 60, "USD")
        self.assertNotEqual(out["status"], "frozen"); self.assertEqual(out["decision"], "allow")


class RatesAreCoarseAndOperatorOverridable(unittest.TestCase):
    def test_override_known_currencies_only_and_ignore_garbage(self):
        old = os.environ.get("PAYCANARY_FX_JSON")
        try:
            os.environ["PAYCANARY_FX_JSON"] = '{"JPY": 0.01, "XYZ": 5, "EUR": -3}'
            self.assertEqual(fx.to_usd_rate("JPY"), 0.01); self.assertIsNone(fx.to_usd_rate("XYZ")); self.assertEqual(fx.to_usd_rate("EUR"), fx.USD_PER_UNIT["EUR"])
            os.environ["PAYCANARY_FX_JSON"] = "{not json"; self.assertEqual(fx.to_usd_rate("JPY"), fx.USD_PER_UNIT["JPY"])
        finally:
            if old is None: os.environ.pop("PAYCANARY_FX_JSON", None)
            else: os.environ["PAYCANARY_FX_JSON"] = old

    def test_amount_formatting_for_paypal(self):
        self.assertEqual((fmt_amount(520000, "JPY"), fmt_amount(12.5, "USD"), fmt_amount("7", "EUR")), ("520000", "12.50", "7.00"))


if __name__ == "__main__":
    unittest.main()
