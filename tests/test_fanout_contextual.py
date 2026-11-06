import unittest
from tests.test_cooloff import hist_rows
from tests.test_enrichment import Net, svc, NOW
from tests.test_service import pay


def agent(net=None, enrich=True, cap=1000):
    s, pp, t, sent = svc(net if net is not None else Net(age=4000, mx=True), enrich=enrich)
    hist_rows(s); s.set_agent("bill-bot", hard_cap=cap)
    return s, pp, t, sent


def onboard(s, t, n, amount=60.0, prefix="Vend", domain="biz", start=0):
    outs = []
    for i in range(start, start + n):
        t[0] += 300
        outs.append(pay(s, "%s%d" % (prefix, i), "%.2f" % (amount + i * 0.37), email="billing@%s%d.com" % (domain, i)))
    return outs


class LegitOnboarding(unittest.TestCase):
    def test_five_real_businesses_onboarded_in_a_day_all_go_through_flagged_not_frozen(self):
        s, pp, t, sent = agent()
        outs = onboard(s, t, 5)
        self.assertTrue(all(o["status"] == "sent" for o in outs), [(o["status"], o["reasons"][0][:90]) for o in outs])
        self.assertEqual(len(pp.calls), 5)
        tripped = [o for o in outs if "fan-out onboarding" in o["reasons"][0]]
        self.assertGreaterEqual(len(tripped), 1)                                                   # the counter DID trip; the evidence on the set let them pass, flagged

    def test_two_vendors_never_trip_the_counter_and_three_are_at_most_flagged(self):
        s, pp, t, sent = agent(); outs = onboard(s, t, 2)
        self.assertTrue(all("fan-out" not in o["reasons"][0] for o in outs))
        s2, pp2, t2, sent2 = agent(); outs2 = onboard(s2, t2, 3)
        self.assertTrue(all(o["status"] == "sent" for o in outs2))


class MulesStillFreeze(unittest.TestCase):
    def test_thirty_mules_with_cheap_footprints_freeze_after_a_handful(self):
        s, pp, t, sent = agent(Net(age=2, mx=False))                                                # days-old domains, no mail
        outs = onboard(s, t, 30, prefix="Mule", domain="mule", amount=90.0)
        n_sent = sum(o["status"] == "sent" for o in outs)
        self.assertLessEqual(n_sent, 4); self.assertEqual(outs[-1]["status"], "frozen"); self.assertIn("structuring guard (fan-out)", outs[-1]["reasons"][0])
        self.assertIn("mule", outs[-1]["reasons"][0])                                              # the decision names the evidence, not just the count

    def test_one_mule_among_real_businesses_is_enough_to_freeze_the_set(self):
        s, pp, t, sent = agent(Net(age=4000, mx=True, feed_hosts=["biz4.com"]))                      # the 5th vendor's domain is in a phishing feed
        outs = onboard(s, t, 6)
        frozen = [o for o in outs if o["status"] == "frozen"]
        self.assertTrue(frozen); self.assertTrue(any("structuring guard (fan-out)" in o["reasons"][0] for o in frozen))


class UnknownIsNotClean(unittest.TestCase):
    def test_without_enrichment_a_tripped_fanout_is_held_as_before(self):
        s, pp, t, sent = agent(enrich=False)
        outs = onboard(s, t, 8)
        self.assertEqual(outs[-1]["status"], "frozen"); self.assertIn("structuring guard (fan-out)", outs[-1]["reasons"][0]); self.assertIn("could not vet", outs[-1]["reasons"][0])

    def test_a_dead_enrichment_backend_holds_never_allows(self):
        s, pp, t, sent = agent(Net(fail=("rdap", "mx", "feed")))
        outs = onboard(s, t, 8)
        self.assertEqual(outs[-1]["status"], "frozen"); self.assertLessEqual(sum(o["status"] == "sent" for o in outs), 4)

    def test_approved_payees_never_count(self):
        s, pp, t, sent = agent(enrich=False); s.set_agent("bill-bot", hard_cap=1000, approved_payees=["Vend%d" % i for i in range(12)])
        outs = onboard(s, t, 12); self.assertTrue(all("fan-out" not in o["reasons"][0] for o in outs))


if __name__ == "__main__":
    unittest.main()
