# Install and first run

PayCanary is a small Python web app (FastAPI + SQLite). This page has three ways in, from "see it running in a minute" to "run it against the PayPal sandbox". Each says plainly what it needs and what it does not.

**Honest scope:** PayPal sandbox and Stripe test mode only. This is a hackathon build, tested on macOS with Python 3.13 and 3.14. Python 3.11 is not supported (one hardening test fails there: very deeply nested JSON sent to `/webhooks/mock` returns a 500). Other operating systems are untried. It is not production-proven.

**Judges and reviewers: do Tier 1 and stop there.** It is the full product with the real gate making real decisions and simulated payouts. It needs no accounts, no keys, and no PayPal account. Tiers 2 and 3 are optional.

---

## Tier 1: see it running (offline demo, one command)

**Needs:** Python 3.13 or newer, `bash`, and a network connection once, for `pip install`. **Does not need:** any keys, any accounts, or Docker.

From the project folder:

```bash
./run.sh          # or: make demo
```

That creates a `.venv`, installs the two runtime packages, prints the URL and your operator token, and serves the demo on <http://127.0.0.1:8000/>. Open it, paste the token it printed into the **Operator token** box, and you are looking at a populated review queue: payments that passed, payments that were flagged, and payments that were frozen. Every payout in the demo is simulated (ids look like `SIM-PP-...`): no money moves and no PayPal call is made. Use `PORT=8765 ./run.sh` if port 8000 is busy.

The same thing by hand, if you prefer to see each step:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PAYCANARY_DEMO=1 PAYCANARY_DEMO_OFFLINE=1 uvicorn app.main:app
```

### With Docker (provided, first run still pending)

```bash
docker compose up
```

This is meant to build a `python:3.13-slim` image and serve the same offline demo on `127.0.0.1:8000` only, printing the operator token in the log as `Operator token (paste it into the dashboard): ...`. **Honest status: the Dockerfile and compose file have not been built or run yet** (Docker was not available where they were written). Treat them as provided, not tested. If you hit a problem, the non-Docker path above is the tested one.

If it does work as intended, these are the details to know:

- The operator token lives at `/app/.paycanary_tokens.json` inside the container, not in the data volume, so a **new container gets a new token**. To read it later: `docker compose exec paycanary cat /app/.paycanary_tokens.json`.
- The demo database lives in the named volume at `/data/paycanary.sqlite3`.

---

## Where the operator token comes from

You do not create it. The first time the app starts, it writes a file called **`.paycanary_tokens.json`** in the project folder (next to `app/`), readable only by you (mode `0600`). It holds two random tokens:

- `operator`: the account owner's credential. This is the one the dashboard asks for.
- `agent`: a shared credential for an AI agent that calls `/pay` and `/intent`. The operator can issue each agent its own token later; once an agent has its own, the shared one stops working for it.

`./run.sh` and the Docker entrypoint print the operator token for you before the server starts. To read it yourself at any time:

```bash
python3 -c "import json; print(json.load(open('.paycanary_tokens.json'))['operator'])"
```

Paste it into the **Operator token** box at the top of the dashboard. If you open the dashboard before pasting it, you will see "Operator token needed"; that is expected.

Good to know:

- You can choose your own instead: set `PAYCANARY_OPERATOR_TOKEN` (and optionally `PAYCANARY_AGENT_TOKEN`) in the environment before starting. If `PAYCANARY_OPERATOR_TOKEN` is set, the file is not used.
- The file is gitignored. Never commit it, and never give the operator token to the agent. The agent should only ever hold the agent token.
- If you delete the file and restart, new tokens are generated. The shared agent token changes; agent tokens the operator already issued through the API are stored separately (as hashes) and are not in this file.
- The first start also creates an audit key and an anchor folder under `~/.paycanary/` (the audit log is keyed so that edits are detectable). Those files stay on your machine.

---

## Tier 2: dev setup (run the tests, change the code)

**Needs:** Python 3.13 or newer, `pip`. No keys.

```bash
./run.sh test        # or: make test
```

That installs the dev requirements into `.venv` and runs the whole unittest suite. A recent fresh-clone run reported 467 tests OK on Python 3.14 and 3.13. Or by hand:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -t .
```

`./run.sh dev` is the same as the demo, with the dev packages installed.

What each requirements file is for:

| File | Contents | Why |
|---|---|---|
| `requirements.txt` | `fastapi`, `uvicorn` (pinned) | Everything the app imports from outside the standard library. |
| `requirements-dev.txt` | the above plus `httpx` | FastAPI's test client needs `httpx`. Without it the HTTP/auth tests are **skipped** (they say so), and the rest still run. |

There is no Python `anthropic` package in the picture: the optional Claude Haiku narration calls the Anthropic HTTP API with the standard library. You do not install anything extra to turn it on, only supply a key (Tier 3).

Optional: `cp .env.example .env` and fill it in when you move to Tier 3. The offline demo still reads `.env` for non-network settings (caps, mode, DB path), but it makes **no outbound calls**: the Haiku narrator, enrichment lookups (RDAP/DoH) and outbound alerts are all forced off in offline mode even if `.env` turns them on. `.env` is gitignored: keep it that way.

---

## Tier 3 (optional): live, with YOUR OWN PayPal sandbox keys

Nothing here is required to run or evaluate the demo. It is for sending real payouts to PayPal's **sandbox**, using **your own** sandbox account; the author's credentials are never part of this repo.

**Needs:** a PayPal **sandbox** app of your own (Client ID, Secret, optionally a Webhook ID and a sandbox personal account email for test payouts) and, if you want the AI narration, an Anthropic API key. Python setup is the same as Tier 1, with no additional Python packages.

```bash
cp .env.example .env     # then add YOUR sandbox credentials; never commit it
python3 tools/probe_payouts.py          # does the sandbox token + Payouts work?
PAYCANARY_DEMO=1 uvicorn app.main:app   # seeds a populated review queue, then open http://127.0.0.1:8000/
```

What each setting enables (from `.env.example` and the README):

| Setting | What it turns on | If you leave it out |
|---|---|---|
| `PAYCANARY_PAYPAL_ENV=sandbox`, `PAYCANARY_PAYPAL_CLIENT_ID`, `PAYCANARY_PAYPAL_CLIENT_SECRET` | real PayPal **sandbox** Payouts through `/pay` | the PayPal provider cannot send; the offline/mock demo still works |
| `PAYCANARY_PAYPAL_WEBHOOK_ID` | the PayPal webhook (payment-status events) setting | the webhook setting is empty; check the README's Webhooks notes before relying on status events |
| `PAYOUT_RECIPIENT_EMAIL` | with it set, `tools/probe_payouts.py` sends ONE $1.00 sandbox payout to that sandbox account | the probe does not send a payout |
| `ANTHROPIC_API_KEY` | Claude Haiku explains flagged payments in plain language (it costs tokens; there is a daily budget, 200 calls and 60,000 tokens by default) | raw narration; everything else works. The AI never moves money. |
| `PAYCANARY_STRIPE_SECRET_KEY` | the Stripe **test-mode** provider | not used. A live Stripe key is refused: the app will not start with it. |
| `PAYCANARY_ENRICHMENT=1` | optional public lookups on a payee's domain (age, mail records, phishing feeds); only the domain leaves the machine | offline checks only |

Notes:

- Sandbox only. Do not put production credentials anywhere near this.
- Keep the PayPal credentials in the proxy's `.env`, never in the environment of the AI agent that calls PayPal through PayCanary. The protection only holds if the agent cannot read them (the README's "Security model" section explains why).
- `PAYCANARY_DEMO=1` without `PAYCANARY_DEMO_OFFLINE=1` still seeds the review queue with simulated payouts; the live provider is used for whatever you send through `/pay` afterwards.
- With Docker, pass these as environment variables (for example a compose override); the image defaults to the offline demo. **Not tested.**

---

## Troubleshooting

- **The dashboard says "Operator token needed" or API calls return 401.** Paste the operator token (above). It is not the agent token.
- **The HTTP/auth tests show as skipped.** `pip install -r requirements-dev.txt` (they need `httpx`).
- **`ModuleNotFoundError: fastapi` or `uvicorn`.** The virtual environment is not active, or the install did not run in it. `./run.sh` handles this for you.
- **A hardening test fails on Python 3.11.** Use 3.13 or newer.
- **Port already in use.** `PORT=8765 ./run.sh`, or `--port 8001` on the `uvicorn` command.
- **The review queue is empty.** The demo seeds only an *empty* database; your data lives in `paycanary.sqlite3` in the project folder (gitignored). Do not delete that file casually: the keyed audit log is anchored under `~/.paycanary/`, and a database that no longer matches its anchors will fail the audit check. The README's "Run it" section covers reseeding (`python3 -m app.seed`, and `POST /demo/seed` when `PAYCANARY_DEMO=1`).

## Next

See the README for what PayCanary does, how a payment is judged and held, and "Integrate in five minutes" for calling `/pay` from an agent.
