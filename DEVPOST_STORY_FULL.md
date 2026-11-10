Inspiration

This started with the hackathon prompt: PayPal + AI. We asked the obvious question — where does that combination actually matter? — and landed on something real that lines up with work we’re already doing: AI agents are about to start spending real money on people’s behalf, and almost nothing is watching those payments. The usual guard is a spending limit, but a limit only stops what you already thought to forbid. It does nothing about the payment that’s inside every rule and still wrong — a payment to someone the agent was never told to pay, a brand-new account with no history, a slow drain trained to look normal. We wanted the thing that catches that, in the moment, before the money’s gone. So in the hackathon window, we built it.

What it does

PayCanary is a platform that sits between a payment and the money actually moving. Every payment passes through it first.

The normal ones go straight through — handled by fast, fixed rules, no AI, no cost, no delay.
Anything that looks off is held and handed to an LLM, which investigates it (how old is this account, has anyone flagged it, does it match what the agent said it would do), explains in plain language why it’s worth a look, and surfaces it for a decision.
A held payment is cleared only by a person, or by a strict rule that re-checks the evidence — never by the AI.
Every decision is written to a tamper-evident record — a hash-chained log where any edit, reorder, or deletion shows.

The AI reviews and explains; it can never move money on its own. That’s on purpose, and it’s the whole point: you can’t prompt-inject PayCanary into releasing a payment, because the AI has no power to release one. The call to let money move is always a fixed rule or a human — never the model. For anyone scared of handing an AI the keys to a payment system, that’s the answer: this one doesn’t hold the keys.

How we built it

A Python service that sits in the payment path, in front of PayPal Payouts. Fixed rules make every money decision and run in a fraction of a second; a short Claude (Haiku) call explains the rules' decisions and can flag a payment for a human — it sits outside the money-authorizing path entirely, so it can add friction but can never release a payment. The agent that pays can’t reconfigure its own guard or release its own held payment; only an operator can. A questionable payee is checked against public signals (how old its domain is, whether it’s on scam lists, and — where a sanctions list is wired in — whether a payee’s exact ID is on it), and fraud signals are shared across customers — a bad account flagged by one is caught for the next — without ever sharing anyone’s private transaction data. Every decision lands in that same tamper-evident log. The review screen is built with AG Grid. It runs on PayPal’s sandbox: Payouts to move money, Webhooks for events, Reporting to learn a customer’s normal, and the PayPal Agent Toolkit for the paying agent’s tools.

Who clears a held payment

PayCanary hands a held payment and its evidence to an operator — and the operator is whoever owns the account: for a person, themselves; for a business or a bank, their fraud team, their advisors, their platform, or their own AI. It doesn’t force a human into the loop — it plugs the decision into whatever a customer already uses, and the scaffolding is built to grow that way.

What we learned
The AI belongs on the hard cases, not every payment. Putting a model in the path of every transaction is slow, costly, and less safe. The fast fixed rules do the heavy lifting and make every money decision; the AI only reviews the few that are already flagged. That split is the design.
Test it against real life, not your own imagination. We benchmarked against fraud modeled on real, documented attacks and against real-world-shaped test customers across eight different lives — a student’s $6 coffee, a family’s cross-border remittance, a business’s six-figure invoice, and more. Testing it that honestly is the only way to trust the result — and the result we’re proudest of is that it never once blocked a real, normal payment.
For a fraud tool, not blocking real people is the whole game. A tool that freezes a real customer’s payment gets turned off. Zero false alarms across every customer type was the bar we held to.
The challenges we faced

We red-teamed our own build and fixed what we found, in the open:

An attacker could have reconfigured the guard or released their own held payment through unprotected controls. We split authority so the paying side can’t touch either.
A race condition could have double-sent a payment under load. Fixed with a lock across the decision.
A drain spread across many small payments to many new accounts could slip under every limit. We tightened the cap on money to new, unproven accounts and have the LLM check the evidence on them — so it catches a fraudster fanning out to many mules without blocking a business paying several real new vendors.
A multi-currency bug would have frozen a legitimate ¥520,000 payment against a dollar limit. Fixed by normalizing currency before the check.

And the honest limit we don’t hide: a patient, well-funded attacker who buys a clean-looking identity and stays small can get one payment through — the floor every fraud system has. We bound how much that can cost and catch it the moment it grows.

By design, this already covers payments an AI agent makes — whether it’s acting on its own, talking to other agents, or compromised — because every payment runs the same gauntlet no matter who or what started it.

What’s next

The same engine points at a bigger problem than one payment rail: watching a person’s or a business’s money across their accounts and catching what’s wrong before it clears. (One thing we’re deliberately not rushing: letting the system clear a held payment with less human involvement. Every shortcut there is a new way to be tricked, so a person stays in the decision until we can remove them without opening a hole.)
