# Devpost corrections — round 2 (paste before Thu noon Pacific)

Round 1 (tamper-proof ×2, sanctions) is DONE and live. These are the remaining
wording tightenings from the second review. All honest, all small. The README +
code are already correct on every one of these — this is only the Devpost story page.

Edit at: https://devpost.com/software/paycanary (your login) → Edit → the story box.

---

## A. Benchmark wording — don't let it read as real production data

The entry says the eight customer types were "real customer behavior." The README is
explicit that these are synthetic / real-world-SHAPED test sets we wrote, not live data.

FIND:
> against real customer behavior across eight different lives

REPLACE WITH:
> against real-world-shaped test customers across eight different lives

(Optional, even clearer — if you want to add one clause at the end of that paragraph:)
> These are our own test corpora, reproducible from the repo — not live-money evidence.

---

## B. AI scope — it can also narrate ALLOWED payments

The entry says the AI "only looks at the small slice the rules have already flagged."
The code can also narrate an allowed payment. The accurate strength is that the AI is
OUTSIDE the money-authorizing path — it can add friction, never release.

FIND:
> a short Claude (Haiku) call only looks at the small slice the rules have already flagged — so the AI is used for judgment, not put in front of every transaction

REPLACE WITH:
> a short Claude (Haiku) call explains the rules' decisions and can flag a payment for a human — it sits outside the money-authorizing path entirely, so it can add friction but can never release a payment

---

## C. Release captions — a human OR an authorized system OR an opt-in cooldown

Some image captions say "Only a human can release." The code also supports an authorized
system operator and an optional deterministic cooldown auto-release. The true invariant is
that the AI narrator can never release — not that ONLY a human can.

FIND (in the captions — may appear more than once):
> Only a human can release a held payment — the AI never can.

REPLACE WITH:
> A held payment is released only by the operator (a person or an authorized system) or a strict re-checking rule — the AI never can.

---

## Already correct, no change needed
- "tamper-evident" (both spots) — fixed in round 1.
- Sanctions line — fixed in round 1.
- PayPal live-proof: the entry does not claim a live PayPal payout, and the README says
  plainly it's pending a funded sandbox account. Nothing to change.
- The human-releases / AI-can't-move-money spine, the prompt-injection point, the
  fraud-sharing "without sharing private transaction data," and the red-team fixes are accurate.
