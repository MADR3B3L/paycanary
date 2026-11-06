import json, unittest
from app import reporting as R
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal, pay

DAY = 86400.0
NOW = 1_800_000_000.0


def detail(ts, amount, email="aws@example.com", code="T0001", custom="", txid=None):
    iso = __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"transaction_info": {"transaction_id": txid or "T%d" % int(ts), "transaction_event_code": code, "transaction_initiation_date": iso,
                                 "transaction_amount": {"currency_code": "USD", "value": "%.2f" % amount}, "custom_field": custom},
            "payer_info": {"email_address": email} if email else {}}


class FakeReporting:
    def __init__(self, details, status=200, per_page=2):
        self.details, self.status, self.per_page, self.calls = details, status, per_page, []
    def request(self, method, path, body=None, request_id=None):
        self.calls.append(path)
        if self.status != 200: return self.status, {"name": "NOT_AUTHORIZED"}
        page = int(path.split("page=")[-1]); lo = (page - 1) * self.per_page
        return 200, {"transaction_details": self.details[lo:lo + self.per_page], "total_pages": max(1, -(-len(self.details) // self.per_page))}


class Normalize(unittest.TestCase):
    def test_only_real_outgoing_payments_to_someone_survive(self):
        ok = R.normalize(detail(NOW - DAY, -85.0))
        self.assertEqual((ok["recipient"], ok["amount"]), ("aws@example.com", 85.0))
        for bad in [detail(NOW, 5000.0, code="T1900"), detail(NOW, -1.25, code="T1503"), detail(NOW, 1.25, code="T1105"),
                    detail(NOW, -10.0, email=""), detail(NOW, -10.0, custom="pay-abc123"), detail(NOW, 20.0)]:
            self.assertIsNone(R.normalize(bad))                                   # fees, reserves, inflows, no counterparty, and PayCanary's own payments

    def test_identity_fields_survive_ingest_and_the_lossy_mode_drops_them(self):
        d = detail(NOW - DAY, -85.0); d["payer_info"]["payer_name"] = {"alternate_full_name": "Amazon Web Services, Inc."}
        d["transaction_info"]["transaction_note"] = "October compute"; d["transaction_info"]["transaction_amount"]["currency_code"] = "eur"
        d["cart_info"] = {"item_details": [{"item_name": "Cloud hosting"}]}
        full = R.normalize(d)
        self.assertEqual((full["payee_name"], full["memo"], full["currency"], full["category"]), ("Amazon Web Services, Inc.", "October compute", "EUR", "Cloud hosting"))
        lossy = R.normalize(d, keep_identity=False)
        self.assertEqual((lossy["payee_name"], lossy["memo"], lossy["category"], lossy["currency"]), ("", "", "", "USD"))
        d2 = detail(NOW, -5.0); d2["shipping_info"] = {"name": "Doe, John"}; self.assertEqual(R.normalize(d2)["payee_name"], "Doe, John")

    def test_malformed_rows_are_skipped_not_fatal(self):
        self.assertIsNone(R.normalize({})); self.assertIsNone(R.normalize({"transaction_info": {"transaction_amount": {"value": "x"}}}))


class Refresh(unittest.TestCase):
    def test_paginates_and_stores_and_is_idempotent(self):
        rows = [detail(NOW - i * DAY, -80 - i, txid="X%d" % i) for i in range(1, 6)]
        st, fp = Store(), FakeReporting(rows)
        out = R.refresh(st, fp, days=10, now=NOW)
        self.assertEqual((out["available"], out["fetched"], out["new"]), (True, 5, 5)); self.assertGreater(len(fp.calls), 1)
        self.assertEqual(R.refresh(st, FakeReporting(rows), days=10, now=NOW)["new"], 0)           # same transactions again add nothing

    def test_unavailable_reporting_degrades_without_raising(self):
        st = Store()
        for fp in (FakeReporting([], status=403), type("Boom", (), {"request": lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))})()):
            out = R.refresh(st, fp, days=10, now=NOW)
            self.assertFalse(out["available"]); self.assertIn("reason", out)
        self.assertEqual(st.ext_rows(0), [])


def svc_with(history, use, named=True):
    t = [NOW]; pp = FakePayPal()
    svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, use_reporting=use)
    NAMES = {"aws@example.com": "AWS", "gh@example.com": "GitHub"}
    svc.store.ext_add([{"ref": "R%d" % i, "ts": ts, "recipient": rc, "amount": a, "currency": "USD", "payee_name": NAMES.get(rc, "") if named else ""}
                       for i, (ts, rc, a) in enumerate(history)])
    return svc, pp, t


HISTORY = [(NOW - (40 - i) * DAY, "aws@example.com", 80 + (i % 5) * 5) for i in range(0, 36, 3)] + [(NOW - (39 - i) * DAY, "gh@example.com", 20 + i % 3) for i in range(0, 36, 4)]


class Seeded(unittest.TestCase):
    def test_cold_agent_with_real_history_knows_its_vendors(self):
        cold, pp, t = svc_with(HISTORY, use=False); real, pp2, t2 = svc_with(HISTORY, use=True)
        a = pay(cold, "AWS", "95.00", email="aws@example.com"); b = pay(real, "AWS", "95.00", email="aws@example.com")
        self.assertIn("first-ever payee", a["reasons"][0])                                   # without reporting: a stranger
        self.assertNotIn("first-ever", b["reasons"][0]); self.assertEqual(b["decision"], "allow")      # with it: a known vendor at a normal amount

    def test_real_history_makes_a_big_first_payment_to_a_stranger_freeze_for_the_right_reason(self):
        cold, *_ = svc_with(HISTORY, use=False); real, *_ = svc_with(HISTORY, use=True)
        self.assertEqual(pay(cold, "NewCo", "450.00", email="new@example.com")["decision"], "freeze")      # cold floor
        out = pay(real, "NewCo", "450.00", email="new@example.com")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("typical", out["reasons"][0])       # now judged against a real baseline (~$60 typical), not just the flat cold-start floor

    def test_a_known_vendor_at_an_attacker_address_freezes(self):
        real, pp, t = svc_with(HISTORY, use=True)
        out = pay(real, "AWS", "95.00", email="attacker@example.com")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("NEW destination", out["reasons"][0]); self.assertEqual(pp.calls, [])

    def test_name_similarity_never_lets_a_lookalike_inherit_real_history(self):
        for named in (True, False):
            real, pp, t = svc_with(HISTORY, use=True, named=named)
            out = pay(real, "AWS-Billing", "95.00", email="aws-billing@example.com")                 # imitates the vendor, different address
            self.assertNotEqual(out["decision"], "allow", named)                                     # whitewashed as a known payee would be an allow
            if named: self.assertEqual(out["decision"], "freeze"); self.assertIn("imitates", out["reasons"][0])

    def test_nameless_history_still_teaches_the_payee_in_hand_by_exact_address_only(self):
        real, pp, t = svc_with(HISTORY, use=True, named=False)
        self.assertEqual(pay(real, "AWS", "95.00", email="aws@example.com")["decision"], "allow")   # same address as the history: known
        self.assertNotEqual(pay(real, "AWS", "95.00", email="attacker@example.com")["decision"], "allow")

    def test_other_currency_history_is_never_pooled_into_a_usd_baseline(self):
        t = [NOW]; svc = Service(Store(), FakePayPal(), clock=lambda: t[0], default_require_intent=False, use_reporting=True)
        svc.store.ext_add([{"ref": "E%d" % i, "ts": NOW - (30 - i) * DAY, "recipient": "tokyo@example.com", "amount": 90000.0 + i, "currency": "JPY", "payee_name": "Tokyo Supply"} for i in range(8)])
        h = svc._history("bill-bot", NOW, ("tokyo@example.com", "Tokyo Supply", "USD"))
        self.assertEqual(h.external_n, 0); self.assertEqual(h.amounts, [])

    def test_external_history_is_not_agent_spend(self):
        real, pp, t = svc_with(HISTORY, use=True)
        h = real._history("bill-bot", t[0])
        self.assertEqual(h.window_total(t[0], 7 * DAY), 0.0); self.assertGreater(h.external_n, 5)

    def test_flag_off_means_ignore_the_stored_history(self):
        off, *_ = svc_with(HISTORY, use=False)
        self.assertEqual(off._history("bill-bot", NOW).external_n, 0)


class IngestAcceptance(unittest.TestCase):
    """The one-line proof the ingest fix landed: a payee known ONLY from real PayPal history (PayCanary has never paid it) must have both
    a homoglyph of its name and a bank-redirect on it FREEZE. Under the old email-only ingest both slipped."""
    ROWS = [(NOW - (60 - 7 * i) * DAY, 3000 + i * 25) for i in range(8)]

    def _svc(self, keep_identity):
        t = [NOW]; pp = FakePayPal()
        svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=10000, use_reporting=True)
        for i, (ts, amt) in enumerate(self.ROWS):
            d = detail(ts, -amt, email="billing@alicedesign.example", txid="H%d" % i)
            d["payer_info"]["payer_name"] = {"alternate_full_name": "Alice Design"}
            svc.store.ext_add([R.normalize(d, keep_identity=keep_identity)])
        return svc, pp

    def test_homoglyph_and_bank_redirect_on_a_history_only_payee_freeze(self):
        svc, pp = self._svc(True)
        legit = pay(svc, "Alice Design", "3100.00", email="billing@alicedesign.example")
        homoglyph = pay(svc, "A1ice Design", "3100.00", email="billing@a1icedesign.example")
        redirect = pay(svc, "Alice Design", "3100.00", email="payments@alice-design-llc.example")
        self.assertNotEqual(legit["decision"], "freeze", legit)                          # the real vendor at its real address is paid
        self.assertEqual((homoglyph["decision"], redirect["decision"]), ("freeze", "freeze"), (homoglyph, redirect))
        self.assertIn("imitates", homoglyph["reasons"][0]); self.assertIn("NEW destination", redirect["reasons"][0])
        self.assertEqual(len(pp.calls), 1)                                                # only the legitimate payment reached PayPal

    def test_the_same_attacks_slip_through_the_old_lossy_ingest(self):
        svc, pp = self._svc(False)                                                        # email + amount + ts only (the earlier behaviour)
        homoglyph = pay(svc, "A1ice Design", "3100.00", email="billing@a1icedesign.example")
        redirect = pay(svc, "Alice Design", "3100.00", email="payments@alice-design-llc.example")
        self.assertNotIn("imitates", homoglyph["reasons"][0]); self.assertNotIn("NEW destination", redirect["reasons"][0])   # the guards were starved


class Destination(unittest.TestCase):
    def test_own_history_binds_payee_to_destination_too(self):
        t = [NOW]; pp = FakePayPal(); svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False)
        for i in range(3):
            t[0] += 20 * 3600; self.assertNotEqual(pay(svc, "AWS", "60.00", email="aws@example.com")["decision"], "freeze")
        t[0] += 20 * 3600
        out = pay(svc, "AWS", "60.00", email="aws-payments@evil.example.com")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("NEW destination", out["reasons"][0])


if __name__ == "__main__":
    unittest.main()
