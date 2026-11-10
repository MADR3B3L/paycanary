"""The goalie (feature B) + canary scoring (feature A): a pure, deterministic classifier, no network, NO LLM.

classify(txn, history) -> (tier, score_kind, reason) with tier in allow | review | freeze.
FREEZE is structural and in-path. The LLM only narrates AFTER a decision (freeze-then-judge); it never decides.
Thresholds are first guesses (untuned) — real agent-payment volume would calibrate them."""
import math
import re
import time

AMOUNT_Z_REVIEW = 3.0
SD_FLOOR_FRAC = 0.05          # amount spread is never judged tighter than 5% of the mean
AMOUNT_Z_FREEZE = 6.0
NEW_PAYEE_CEIL_MULT = 2.0
MAX_MULT_FREEZE = 5.0
VELOCITY_N = 5
NEW_PAYEE_VELOCITY_N = 2       # O6: a not-yet-trusted payee has no business being paid twice inside the velocity window
FANOUT_K = 3.0                # structuring BREADTH: money to not-yet-trusted, non-approved payees in 24h may not exceed K x the agent's typical (independent of hard_cap)
COOLOFF_MULT = 1.5             # O3 (opt-in per agent: cooldown_s > 0): a FIRST payment to a new payee above this x the agent's typical is held for a cooling-off window
PROBATION_ABS, PROBATION_MULT = 500.0, 3.0   # O2: a single payment to an unproven payee above min(abs, mult x the agent's typical) is a review
VELOCITY_WINDOW_S = 60.0
# --- anti-poisoning (red-team gun 4: an agent TRAINS the gate with ALLOWED, individually-small steps) ---
RATCHET_PER_DAY = 0.05        # the trusted ceiling may grow at most 5%/day, however many payments are made
RATCHET_REVIEW_MULT = 1.5     # TRUSTED payee: amount > 1.5x its own ratcheted ceiling -> review
RATCHET_FREEZE_MULT = 2.0     #                  amount > 2x  its own ratcheted ceiling -> freeze
UNTRUSTED_REVIEW_MULT = 1.25  # payee not yet trusted (needs time AND count): tighter tolerances
UNTRUSTED_FREEZE_MULT = 2.0
TRUST_MIN_COUNT = 3           # a payee is only "trusted" after BOTH this many executed payments ...
TRUST_MIN_AGE_S = 7 * 86400   # ... AND this much time since its first one (count alone is farmable in an hour)
FAST_ALPHA, SLOW_ALPHA = 0.5, 0.05      # two-timescale per-payment EWMAs of what this agent pays (fast ~last 2-3, slow ~last 20)
DIVERGE_REVIEW, DIVERGE_FREEZE, DIVERGE_MIN_N = 1.6, 2.5, 5
# --- timing / cadence (a bill paid on its usual cycle is MORE normal; the same payee paid off-cycle is stranger) ---
CADENCE_MIN_PAYMENTS = 3      # prior payments needed before a payee has a cadence at all (>= 2 intervals)
CADENCE_WINDOW = 7            # use the last N payments, so a payee whose rhythm genuinely changes re-learns it
CADENCE_REGULAR_SHARE = 0.6   # a rhythm is regular when this share of recent gaps sit within CADENCE_BAND of the median gap (robust to a burst)
CADENCE_BAND = (0.65, 1.5)
CADENCE_MIN_PERIOD_S = 86400  # sub-daily rhythms say nothing about "on cycle"
ON_CYCLE = (0.7, 1.35)        # gap / typical interval inside this band = on cadence
OFF_CYCLE_EARLY = 0.5         # gap < 0.5 x typical interval = paid too soon (e.g. a monthly bill 3 times in a week)
# --- structuring / smurfing: a per-payment cap is not a cap without a rolling total (red-team round 2, finding 3) ---
WINDOW_MULT = ((3600.0, 2.0), (86400.0, 4.0), (604800.0, 10.0))    # agent-wide executed spend per window <= mult x hard_cap
PAYEE_WINDOW_MULT = ((86400.0, 2.0), (604800.0, 5.0))              # ... and per payee
COLD_START_PAYMENTS = 5       # below this many executed payments the agent has no baseline: the flat floor below is all there is
UNTRUSTED_PAYEE_BUDGET_MULT = 2.0   # an untrusted payee may receive at most max(1.5 x floor, 2 x the agent's typical amount) in 24h (2x that in 7d)
UNTRUSTED_BUDGET_FLOOR_MULT = 1.5
OTHER_PAYEE_MULT, OTHER_PAYEE_MIN_N = 4.0, 5   # an unapproved payee may not be paid > 4 x what this agent pays its OTHER payees (needs 5+ of those)
DUP_WINDOW_S = 300.0          # an identical (payee, amount) payment inside this window is a possible replay / double-fire
BURST_WINDOW_S, BURST_MULT = 60.0, 2.5   # money that left in the last minute + this payment > 2.5 x the agent's historical max => a drain, stop it
NEW_PAYEE_FLOOR = 100.0       # a payee never paid before (and not operator-approved) above this amount FREEZES, however the agent "stated" it
# Memo cues: language that usually accompanies invoice fraud / social engineering. REVIEW only (the memo is agent-written text).
MEMO_CUES = re.compile(r"\b(gift ?cards?|crypto\w*|bitcoin|btc|western union|moneygram|prepaid|urgent\w*|do not tell|don'?t tell|keep (it )?(quiet|secret)|"
                       r"(new|changed|updated) (bank|account|iban|routing)|personal account|wire (me|asap|today)|asap)\b", re.I)
LEARNED_HOURS_MIN_N, LEARNED_HOURS_MAX_DISTINCT = 20, 14       # learn an agent's active window only from enough, narrow-enough data


_HOMOGLYPH = str.maketrans({"1": "l", "i": "l", "0": "o", "5": "s", "$": "s", "@": "a", "|": "l", "!": "l", "3": "e", "4": "a", "7": "t"})      # i, l, 1, | and ! all fold together
_GENERIC = ("billing", "payments", "payment", "support", "services", "service", "official", "accounting", "invoices", "invoice", "group",
            "team", "corp", "inc", "llc", "ltd", "co", "ap", "pay")


def skeleton(name):
    """Canonical form for impersonation checks: generic corporate words dropped, then homoglyphs folded (1/i/l, 0/o, rn/m ...), punctuation removed."""
    toks = [t for t in "".join(c if c.isalnum() else " " for c in str(name).lower()).split() if t]
    keep = [t for t in toks if t not in _GENERIC] or toks
    return "".join(keep).replace("rn", "m").replace("vv", "w").translate(_HOMOGLYPH)


def _edit1(a, b):
    """True if the strings differ by at most one insertion, deletion or substitution."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1; j += 1
    return diff + (len(b) - j) + (len(a) - i) <= 1


def lookalike_of(payee, known):
    """A known payee this one imitates (never the identical string), else None."""
    sp = skeleton(payee)
    for k in known:
        if k == payee:
            continue
        sk = skeleton(k)
        if sp == sk:
            return k                                                  # same after folding homoglyphs/case/generic words: A1ice vs Alice, AWS-Billing vs AWS
        if len(sk) >= 5 and len(sp) >= 5 and str(payee).isalpha() and str(k).isalpha() and _edit1(sp, sk):
            return k                                                  # one typo apart, letters only (Amazom vs Amazon); numbered payees (Neo638/Neo648) are NOT lookalikes
    return None


class History:
    """What THIS agent has actually paid. Built from executed (allow/review) payments only — a frozen payment did not
    happen, so it must not teach the baseline. Velocity counts every ATTEMPT, frozen or not."""

    def __init__(self, hard_cap=None, active_hours=None, approved_payees=None, new_payee_floor=NEW_PAYEE_FLOOR, probation_abs=PROBATION_ABS, probation_mult=PROBATION_MULT, cooldown_s=0.0, fanout_k=None, probation_hold=False):
        self.fanout_k = float(fanout_k) if fanout_k else FANOUT_K
        self.probation_hold = bool(probation_hold)             # opt-in (default OFF): cooling-off also covers a first payment above the probation ceiling
        self.probation_abs, self.probation_mult, self.cooldown_s = probation_abs, probation_mult, float(cooldown_s or 0.0)
        self.payee_attempts = {}                        # payee -> attempt timestamps in the velocity window (frozen or not)
        self.payees, self.amounts, self.attempt_ts = {}, [], []
        self.first_ts = {}                      # payee -> ts of its first executed payment
        self.pceil, self.pceil_ts = {}, {}      # per-PAYEE ratcheted ceiling (grows <= RATCHET_PER_DAY, not per payment)
        self.fast = self.slow = None
        self.hard_cap = hard_cap
        self.active_hours = set(active_hours) if active_hours else None       # None => learn the window from executed payments
        self.pts, self.hour_counts = {}, {}
        self.external_n = 0
        self.payee_recipients = {}                      # payee -> destination emails it has actually been paid at
        self.by_payee = {}                              # payee -> executed amounts
        self.spend = []                                 # (ts, payee, amount) of money that actually moved or is in flight (incl. released freezes)
        self.approved = set(approved_payees or [])      # operator-approved payees: externally anchored, unlike a self-asserted intent
        self.new_payee_floor = new_payee_floor

    def observe_identity(self, payee, ts=None, recipient=None):
        """Register that this payee/recipient is KNOWN, without feeding any amount into the baseline.
        Used for history in a different currency: the identity carries across currencies (so a payee already
        paid in USD is not treated as a brand-new, never-seen recipient when paid in EUR), but its amount
        range does NOT (¥520,000 must never be compared against a dollar baseline). The payee becomes 'seen'
        (not first_seen) but gains no amount trust: its ceiling/averages in the new currency are still empty,
        so first-amount caution in the new currency still applies."""
        self.payees.setdefault(payee, 0)                    # known, but count stays 0 until a same-currency payment is observed
        if ts is not None:
            self.first_ts.setdefault(payee, ts)
        if recipient:
            self.payee_recipients.setdefault(payee, set()).add(recipient)

    def observe(self, payee, amount, ts=None, hour=None, recipient=None):
        """Feed executed payments in time order."""
        amount = float(amount)
        if ts is not None:
            self.pts.setdefault(payee, []).append(ts)
            h = hour if isinstance(hour, int) else time.gmtime(ts).tm_hour
            self.hour_counts[h] = self.hour_counts.get(h, 0) + 1
        self.payees[payee] = self.payees.get(payee, 0) + 1
        self.by_payee.setdefault(payee, []).append(amount)
        if recipient:
            self.payee_recipients.setdefault(payee, set()).add(recipient)
        self.amounts.append(amount)
        if ts is not None:
            self.first_ts.setdefault(payee, ts)
            if payee not in self.pceil:
                self.pceil[payee] = amount
            elif len(self.pts.get(payee, ())) <= TRUST_MIN_COUNT:
                self.pceil[payee] = max(self.pceil[payee], amount)      # calibration: a payee's first few payments set its natural range (bounded by the untrusted-payee budget); the ratchet starts after
            else:
                grow = self.pceil[payee] * (1 + RATCHET_PER_DAY) ** (max(0.0, ts - self.pceil_ts[payee]) / 86400.0)
                self.pceil[payee] = max(self.pceil[payee], min(amount, grow))   # an ALLOWED payment cannot drag a payee's ceiling up faster than time allows
            self.pceil_ts[payee] = ts
        self.fast = amount if self.fast is None else self.fast + FAST_ALPHA * (amount - self.fast)
        self.slow = amount if self.slow is None else self.slow + SLOW_ALPHA * (amount - self.slow)

    def ceiling_now(self, now, payee):
        if payee not in self.pceil:
            return None
        return self.pceil[payee] * (1 + RATCHET_PER_DAY) ** (max(0.0, now - self.pceil_ts[payee]) / 86400.0)

    def seed_external(self, rows):
        """Real PayPal history (payee, amount, ts), oldest first. Teaches the baseline like executed payments but does NOT count as this
        agent's recent spend (it was not moved by this agent through PayCanary)."""
        for payee, amount, ts, recipient in rows:
            self.observe(payee, amount, ts, recipient=recipient)
        self.external_n = len(rows)

    def add_spend(self, ts, payee, amount):
        self.spend.append((ts, payee, float(amount)))

    def other_mean(self, payee):
        """Mean of what this agent pays everyone EXCEPT `payee` (None until there are enough such payments)."""
        xs = [a for p, v in self.by_payee.items() if p != payee for a in v]
        return (sum(xs) / len(xs)) if len(xs) >= OTHER_PAYEE_MIN_N else None

    def exec_count(self):
        return len(self.amounts)

    def recent_duplicate(self, now, payee, amount):
        """Seconds since an identical executed payment, if one is inside the replay window."""
        gaps = [now - t for t, p, a in self.spend if p == payee and abs(a - amount) < 0.005 and 0 <= now - t <= DUP_WINDOW_S]
        return min(gaps) if gaps else None

    def untrusted_window_total(self, now, secs=86400.0):
        """Money that moved, inside the window, to payees that are BRAND NEW to this agent (first paid inside the window), neither trusted nor
        operator-approved: the fan-out axis of structuring (many different new payees). Repeat traffic to a payee that is merely not-yet-trusted is
        the per-payee budget's job, not this guard's, so a young agent's ordinary vendors do not trip it."""
        return sum(a for t, p, a in self.spend if now - secs <= t <= now and p not in self.approved and not self.trusted(p, now)
                   and self.first_ts.get(p, t) >= now - secs)

    def fanout_payees(self, now, secs=86400.0):
        """The brand-new, not-yet-trusted, unapproved payees the fan-out counter counts (the SET whose evidence decides a tripped counter)."""
        return sorted({p for t, p, a in self.spend if now - secs <= t <= now and p not in self.approved and not self.trusted(p, now)
                       and self.first_ts.get(p, t) >= now - secs})

    def window_total(self, now, secs, payee=None):
        return sum(a for t, p, a in self.spend if now - secs <= t <= now and (payee is None or p == payee))

    def cadence(self, payee, now):
        """Rhythm of this payee's payments from this agent: None until there is enough history. Pure function of the baseline."""
        ts = self.pts.get(payee, [])
        if len(ts) < CADENCE_MIN_PAYMENTS:
            return None
        ts = ts[-CADENCE_WINDOW:]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        med = sorted(gaps)[len(gaps) // 2]
        share = sum(1 for g in gaps if med > 0 and CADENCE_BAND[0] <= g / med <= CADENCE_BAND[1]) / len(gaps)
        gap = now - ts[-1]
        regular = share >= CADENCE_REGULAR_SHARE and med >= CADENCE_MIN_PERIOD_S
        ratio = gap / med if med > 0 else None
        return {"n": len(self.pts[payee]), "gap_days": round(gap / 86400, 2), "typical_days": round(med / 86400, 2), "regular_share": round(share, 2),
                "regular": regular, "ratio": None if ratio is None else round(ratio, 2),
                "on_cycle": bool(regular and ratio is not None and ON_CYCLE[0] <= ratio <= ON_CYCLE[1]),
                "too_soon": bool(regular and ratio is not None and ratio < OFF_CYCLE_EARLY)}

    def hour_unusual(self, hour):
        if not isinstance(hour, int):
            return False
        if self.active_hours is not None:
            return hour not in self.active_hours
        if sum(self.hour_counts.values()) < LEARNED_HOURS_MIN_N or len(self.hour_counts) > LEARNED_HOURS_MAX_DISTINCT:
            return False                                            # not enough / too spread to claim a "normal window"
        window = {(h + d) % 24 for h in self.hour_counts for d in (-1, 0, 1)}
        return hour not in window

    def trusted(self, payee, now):
        return self.payees.get(payee, 0) >= TRUST_MIN_COUNT and now - self.first_ts.get(payee, now) >= TRUST_MIN_AGE_S

    def divergence(self, amount):
        """fast/slow ratio if this payment were added; None until there is enough history."""
        if len(self.amounts) < DIVERGE_MIN_N or not self.slow:
            return None
        f = self.fast + FAST_ALPHA * (amount - self.fast)
        sl = self.slow + SLOW_ALPHA * (amount - self.slow)
        return f / sl if sl > 0 else None

    def note_attempt(self, ts, payee=None):
        if payee is not None:
            self.payee_attempts.setdefault(payee, []).append(ts)
        self.attempt_ts.append(ts)

    def attempts_in_window(self, now):
        return [t for t in self.attempt_ts if now - t <= VELOCITY_WINDOW_S]

    def mean_sd(self):
        n = len(self.amounts)
        if n == 0:
            return 0.0, 0.0
        m = sum(self.amounts) / n
        if n < 2:
            return m, 0.0
        return m, math.sqrt(sum((a - m) ** 2 for a in self.amounts) / (n - 1))


def _probation_threshold(hist, m_usd):
    """The USD amount above which a first payment to an unproven payee is out of the probation band (shared by the review and the opt-in hold)."""
    return min(hist.probation_abs, hist.probation_mult * m_usd)


def fanout_trips(txn, hist):
    """(trips, ref, total_usd): does this payment trip the fan-out COUNTER? Single source of truth for the gate and for the service's evidence pre-pass."""
    payee, ts = txn.get("payee"), txn.get("ts", 0.0)
    if payee in hist.approved or hist.trusted(payee, ts):
        return False, 0.0, 0.0
    fx = float(txn.get("fx") or 1.0)
    m = hist.mean_sd()[0]
    ref = m * fx if m > 0 else hist.new_payee_floor
    tot = hist.untrusted_window_total(ts) + float(txn.get("amount") or 0) * fx
    return (ref > 0 and tot > hist.fanout_k * ref), ref, tot


def _classify_base(txn, hist):
    payee, amount = txn.get("payee"), txn.get("amount")
    ts, hour = txn.get("ts", 0.0), txn.get("hour")
    if not isinstance(payee, str) or isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return "freeze", "novelty", "unparseable/invalid txn (fail-toward-freezing)"
    new_payee = payee not in hist.payees
    m, sd = hist.mean_sd()
    sd = max(sd, SD_FLOOR_FRAC * m)                  # a bill that is always ~$100.0x must not make $101 look like 10 SD
    fx = float(txn.get("fx") or 1.0)                 # USD per unit of this payment's currency (1.0 for USD): caps and windows are in USD, baselines in native units
    amount_usd, m_usd = amount * fx, m * fx
    shown = f"{amount:g}" if fx == 1.0 else f"{amount:g} {txn.get('currency') or ''} (~${amount_usd:,.0f})"
    z = 0.0 if sd <= 0 else (amount - m) / sd         # signed: paying LESS than usual is never the danger
    intent = txn.get("stated_intent_payees")
    # --- FREEZE: deterministic, in-path ---
    if hist.hard_cap is not None and amount_usd > hist.hard_cap:
        return "freeze", "degradation", f"amount {shown} is over this agent's hard cap {hist.hard_cap:g}"
    if hist.hard_cap is not None:                               # cumulative: sum what already left + this payment
        for secs, mult in WINDOW_MULT:
            tot = hist.window_total(ts, secs) + amount_usd
            if tot > mult * hist.hard_cap:
                return "freeze", "degradation", f"cumulative {tot:g} in {secs / 3600:g}h would exceed {mult * hist.hard_cap:g} ({mult:g}x this agent's cap): structuring guard"
        for secs, mult in PAYEE_WINDOW_MULT:
            tot = hist.window_total(ts, secs, payee) + amount_usd
            if tot > mult * hist.hard_cap:
                return "freeze", "degradation", f"cumulative {tot:g} to '{payee}' in {secs / 3600:g}h would exceed {mult * hist.hard_cap:g}: structuring guard"
    rcpt, seen_at = txn.get("recipient"), hist.payee_recipients.get(payee)
    if not new_payee and rcpt and seen_at and rcpt not in seen_at:
        return "freeze", "novelty", f"known payee '{payee}' is being paid at a NEW destination ({rcpt}); it has only been paid at {len(seen_at)} other address(es) (changed-bank-details / redirect guard)"
    dup = hist.recent_duplicate(ts, payee, amount_usd)
    if dup is not None:
        return "freeze", "degradation", f"possible replay: an identical {amount:g} payment to '{payee}' went out {dup:.0f}s ago (idempotency guard)"
    if new_payee and payee not in hist.approved:
        imitated = lookalike_of(payee, set(hist.payees) | set(hist.approved))
        if imitated:
            return "freeze", "novelty", f"payee '{payee}' imitates the known payee '{imitated}' (lookalike / impersonation guard)"
    trips, ref, tot = fanout_trips(txn, hist)
    if trips:
        fe, hint, why = txn.get("fanout_evidence"), None, ""
        if fe is not None:                                       # CONTEXTUAL: the counter is only the trigger; the evidence on the SET of new payees decides
            try:
                from . import external_evidence as E
                hint, why, _items = E.fanout_verdict(fe)
            except Exception:
                hint = None                                      # evidence layer unavailable: fall back to the blind counter (fail-safe)
        if hint == "review":
            txn["_fanout_note"] = f"fan-out onboarding: {why} (cumulative {tot:g} to new payees in 24h exceeds {hist.fanout_k:g}x typical {ref:g}; evidence on every new payee is clean)"
        else:
            tail = (": " + why) if why else ""
            return "freeze", "degradation", (f"structuring guard (fan-out): cumulative {tot:g} to not-yet-trusted payees in 24h would exceed {hist.fanout_k:g}x this agent's "
                                              f"typical ({hist.fanout_k * ref:g}); trusted and operator-approved payees do not count" + tail)
    if new_payee and payee not in hist.approved and hist.exec_count() < COLD_START_PAYMENTS and amount_usd > hist.new_payee_floor:
        return "freeze", "novelty", f"NEW payee '{payee}' for {amount:g} (> {hist.new_payee_floor:g} floor for a payee nobody has approved or paid before, agent has no baseline yet)"
    if payee not in hist.approved and not hist.trusted(payee, ts):
        budget = max(UNTRUSTED_BUDGET_FLOOR_MULT * hist.new_payee_floor, UNTRUSTED_PAYEE_BUDGET_MULT * m_usd)
        for secs, bmult in ((86400.0, 1.0), (604800.0, 3.0)):
            prior = hist.window_total(ts, secs, payee)
            if prior > 0 and prior + amount_usd > bmult * budget:
                return "freeze", "novelty", f"cumulative {prior + amount:g} in {secs / 86400:g} day(s) to '{payee}', a payee not yet trusted (needs {TRUST_MIN_COUNT} payments over {TRUST_MIN_AGE_S // 86400} days or operator approval); budget {bmult * budget:g}: structuring guard"
    om = hist.other_mean(payee)
    first = hist.by_payee[payee][0] if payee in hist.by_payee else None
    outgrew = first is None or first <= 2.0 * (om or 0)       # entered at its peers' level and has since outgrown them (a payee that started big is just a big vendor)
    if om and outgrew and payee not in hist.approved and amount > OTHER_PAYEE_MULT * om:
        return "freeze", "degradation", f"amount {amount:g} to '{payee}' is {amount / om:.1f}x what this agent pays its other payees (typical {om:.0f}); a payee that outgrows them needs operator approval"
    burst_ref = max(hist.amounts) if hist.amounts else 0.0
    if burst_ref > 0:
        out60 = hist.window_total(ts, BURST_WINDOW_S)
        if out60 > 0 and out60 + amount_usd > BURST_MULT * burst_ref * fx:
            return "freeze", "degradation", f"drain guard: {out60 + amount:g} would leave in {BURST_WINDOW_S:.0f}s, more than {BURST_MULT:g}x this agent's largest payment {burst_ref:g}"
    if txn.get("require_intent") and not (isinstance(intent, list) and intent):
        return "freeze", "novelty", "no live stated intent, and this agent is configured to require one (fail-toward-freezing)"
    if isinstance(intent, list) and intent and payee not in intent:
        return "freeze", "novelty", f"payee '{payee}' is NOT in the agent's stated intent {intent} (intent mismatch)"
    n_recent = len(hist.attempts_in_window(ts))
    if payee not in hist.approved and not hist.trusted(payee, ts):
        n_payee = len([t for t in hist.payee_attempts.get(payee, ()) if ts - t <= VELOCITY_WINDOW_S])
        if n_payee >= NEW_PAYEE_VELOCITY_N:
            return "freeze", "degradation", f"new-payee burst: {n_payee} payment attempts to '{payee}' in {VELOCITY_WINDOW_S:.0f}s, and it is not yet a trusted payee"
    if n_recent > VELOCITY_N:
        return "freeze", "degradation", f"rapid-fire: {n_recent} payment attempts in {VELOCITY_WINDOW_S:.0f}s"
    if new_payee and m > 0 and amount > NEW_PAYEE_CEIL_MULT * m:
        return "freeze", "novelty", f"NEW payee '{payee}' for {amount:g} (> {NEW_PAYEE_CEIL_MULT:g}x this agent's typical {m:.3g})"
    hist_max = max(hist.amounts) if hist.amounts else 0.0
    if hist_max > 0 and amount > MAX_MULT_FREEZE * hist_max:
        return "freeze", "degradation", f"amount {amount:g} is {amount / hist_max:.0f}x this agent's historical max {hist_max:.0f}"
    ceil = hist.ceiling_now(ts, payee)
    trusted = hist.trusted(payee, ts)
    fmult, rmult = (RATCHET_FREEZE_MULT, RATCHET_REVIEW_MULT) if trusted else (UNTRUSTED_FREEZE_MULT, UNTRUSTED_REVIEW_MULT)
    if ceil is not None and amount > fmult * ceil:
        return "freeze", "degradation", f"amount {amount:g} is {amount / ceil:.1f}x what '{payee}' has been paid (ratcheted ceiling {ceil:.0f}; may grow {RATCHET_PER_DAY:.0%}/day{'' if trusted else '; payee not yet trusted'})"
    div = hist.divergence(amount)
    if div is not None and div >= DIVERGE_FREEZE and not hist.trusted(payee, ts):      # a TRUSTED payee's spike-then-growth is a review (its own ratchet still freezes real jumps)
        return "freeze", "degradation", f"escalation: recent payments run {div:.1f}x the long-run level (fast/slow divergence)"
    if sd > 0 and z >= AMOUNT_Z_FREEZE:
        return "freeze", "degradation", f"amount {amount:g} is {z:.1f} SD from this agent's mean {m:.0f}"
    # --- HOLD / REVIEW for an unproven payee ---
    # cooling-off HOLD (opt-in: cooldown_s > 0). Its default band is a first payment above COOLOFF_MULT x typical. With the per-agent flag
    # `probation_hold` ON it also holds a first payment above the probation ceiling, and is checked BEFORE the probation review, so such a payment is
    # held instead of being sent flagged. With the flag OFF (default) this is byte-for-byte the previous order: probation review first, then the 1.5x hold.
    cooling = hist.cooldown_s > 0 and new_payee and payee not in hist.approved and m > 0 and not txn.get("cooloff_released")
    over_prob = payee not in hist.approved and not hist.trusted(payee, ts) and m > 0 and amount_usd > _probation_threshold(hist, m_usd)
    held_msg = (f"cooling-off: a first payment of {amount:g} to the new payee '{payee}' is held for {hist.cooldown_s / 60:g} min while it is checked; "
                f"it releases itself if nothing adverse turns up, and an operator can deny it any time")
    if cooling and hist.probation_hold and (amount > COOLOFF_MULT * m or over_prob):
        return "freeze", "novelty", held_msg
    # --- REVIEW: proceeds, flagged for the operator ---
    if over_prob:
        return "review", "novelty", f"probation cap: {amount:g} is large for an unproven payee '{payee}' (above min({hist.probation_abs:g}, {hist.probation_mult:g}x this agent's typical {m:.0f}))"
    if cooling and amount > COOLOFF_MULT * m:
        return "freeze", "novelty", held_msg
    cue = MEMO_CUES.search(txn.get("memo") or "")
    if cue:
        return "review", "novelty", f"memo contains pressure / redirection language ('{cue.group(0)}'): worth a human look before it is trusted"
    if new_payee:
        return "review", "novelty", f"first-ever payee '{payee}' for this agent (amount {amount:g}, within norms)"
    cad = hist.cadence(payee, ts) if not new_payee else None
    on_cycle = bool(cad and cad["on_cycle"])
    if cad and cad["too_soon"]:
        return "review", "novelty", (f"off-cycle: '{payee}' was last paid {cad['gap_days']:g} days ago; it is usually paid about every "
                                      f"{cad['typical_days']:g} days")
    if hist.hour_unusual(hour):
        return "review", "novelty", f"unusual time for this agent: hour {hour} UTC is outside the hours it normally pays"
    if ceil is not None and amount > rmult * ceil:
        return "review", "degradation", f"amount {amount:g} is {amount / ceil:.1f}x what '{payee}' has been paid (ceiling {ceil:.0f})"
    if on_cycle and amount <= rmult * (ceil or amount):          # a regular bill on its usual cycle at its usual size: leave it alone
        return "allow", "none", f"on cadence: '{payee}' is paid about every {cad['typical_days']:g} days (last {cad['gap_days']:g} days ago), amount {amount:g} in line"
    if div is not None and div >= DIVERGE_REVIEW:
        return "review", "degradation", f"escalating: recent payments run {div:.1f}x the long-run level"
    if sd > 0 and z >= AMOUNT_Z_REVIEW:
        return "review", "degradation", f"amount {amount:g} is {z:.1f} SD from this agent's mean {m:.0f}"
    return "allow", "none", f"known payee '{payee}', amount {amount:g} within this agent's norm"


def classify(txn, hist):
    """Per-agent gate, then two optional ADDITIVE layers whose inputs arrive pre-resolved in the txn (the gate does no I/O):
      1. txn["network"]  - cross-agent payee reputation (app/reputation.py);
      2. txn["evidence"] - external evidence about a new/unproven payee (app/external_evidence.py: offline local checks plus any cached
         network evidence). Soft signals raise review/hold (a HOLD is held for an informed human ask, mapped to freeze); only a hard feed hit
         can freeze on its own. Evidence only ever ADDS friction: it never releases a per-agent freeze and never relaxes a tier.
    Without either key this is exactly the per-agent gate."""
    tier, kind, reason = _classify_base(txn, hist)
    if txn.get("_fanout_note"):
        if tier == "allow":
            tier, kind, reason = "review", "novelty", txn["_fanout_note"]    # clean-evidence onboarding passes, but flagged: never a silent allow
        elif tier == "review":
            reason = txn["_fanout_note"] + " | " + reason                    # keep the other reason; the operator sees that the counter tripped too
    ts, payee = txn.get("ts", 0.0), txn.get("payee")
    net = txn.get("network")
    if net:
        try:
            from . import reputation as rep
            known = {}
            for peer in net.get("skeleton_peers") or []:
                if rep.trusted_by_network(peer, ts):
                    known[net.get("skeleton_hash")] = peer.get("recipient_key")
            hint, why = rep.reputation_checks(net, now=ts, new_payee=payee not in hist.payees, amount=txn.get("amount"), agent_typical=hist.mean_sd()[0],
                                              name_skeleton=net.get("skeleton_hash"), recipient_key=net.get("recipient_key"), known_high_rep=known)
            new = rep.apply(tier, hint)
            if new != tier:
                tier, kind, reason = new, "novelty", why
        except Exception:
            pass                                          # module absent or erroring: the per-agent verdict stands (network signal is additive only)
    ev = txn.get("evidence")
    if ev and payee not in hist.approved and (payee not in hist.payees or not hist.trusted(payee, ts)):
        try:
            from . import external_evidence as E
            hint, why, items = E.score_evidence(ev)
            new = E.apply(tier, hint)
            if new != tier:
                tier, kind = ("freeze" if new in ("hold", "freeze") else new), "novelty"
                lines = "; ".join(i.get("detail", "") for i in items)
                reason = ("held for an informed review: " if new == "hold" else "") + why + (": " + lines if lines else "")
        except Exception:
            pass
    return tier, kind, reason


IDENTITY_MARKERS = ("imitates the known payee", "NEW destination", "is NOT in the agent's stated intent")


def is_identity_freeze(reason):
    """True for freezes that are about WHO the payee is (lookalike, changed destination, payee outside the stated plan): the only gate freezes
    that may become cross-agent negative evidence. Cap / velocity / amount / cumulative freezes describe an agent, not a payee."""
    return any(m in (reason or "") for m in IDENTITY_MARKERS)


def risk_score(tier, kind, llm_flag=False, on_cycle=False):
    """0-100 display heuristic for the dashboard (the decision is the tier, not this number). A bill on its usual cycle scores lowest."""
    base = {"allow": 3 if on_cycle else 10, "review": 50, "freeze": 90}.get(tier, 90)
    bump = {"degradation": 8, "novelty": 4}.get(kind, 0)
    return min(100, base + bump + (10 if llm_flag else 0))
