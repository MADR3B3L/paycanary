# Devpost corrections — paste before Thu noon Pacific

Edit at: https://devpost.com/software/paycanary (your login) → Edit → the story fields.
These make the submission match what the build actually does (README + code are already correct).
Honest, plain, three swaps. Everything else on the page is accurate — leave it.

---

## 1. "What it does" section

FIND:
> Every decision is written to a tamper-proof record.

REPLACE WITH:
> Every decision is written to a tamper-evident record — a hash-chained log where any edit, reorder, or deletion shows.

---

## 2. "How we built it" section — the sanctions line

FIND:
> how old its domain is, whether it's on scam or sanctions lists), and fraud signals are shared across customers

REPLACE WITH:
> how old its domain is, whether it's on scam lists, and — where a sanctions list is wired in — whether a payee's exact ID is on it), and fraud signals are shared across customers

(True to the build: the gate hard-freezes on an exact sanctions-ID match, but no sanctions
data feed ships yet. "where a sanctions list is wired in" says that honestly without a footnote.)

---

## 3. "How we built it" section — second tamper mention

FIND:
> Every decision lands in a tamper-proof log.

REPLACE WITH:
> Every decision lands in that same tamper-evident log.

---

Nothing else over-claims. The human-releases / AI-can't-move-money spine, the
"can't prompt-inject into releasing," the fraud-sharing "without sharing private
transaction data," the zero-false-alarm benchmark, and all four red-team fixes
are accurate as written.
