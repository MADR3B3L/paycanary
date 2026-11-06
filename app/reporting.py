"""Optional baseline source: the account's REAL PayPal outflow history via GET /v1/reporting/transactions, so "normal for this
agent" is grounded in history from day one instead of only in what PayCanary itself has seen.

GRACEFUL by design (the Reporting API was slow to enable and not every account has it): every failure returns
{"available": False, "reason": ...} and the gate simply falls back to PayCanary's own records. Nothing here is on the /pay path:
refresh() runs out-of-band and stores rows in SQLite; the gate reads the stored copy.
Rows PayCanary itself sent are excluded (their custom_field is our payment id, 'pay-...'): those are already in our own records."""
import datetime as dt
import time

WINDOW_DAYS = 31                 # the API accepts at most 31 days per request
FEE_OR_RESERVE_PREFIXES = ("T11", "T15")      # balance/fee/reserve movements are not payments to anyone


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def _parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _name(detail):
    """Best available counterparty NAME in a reporting row: business name, then person name, then the shipping/store name. '' if none."""
    pi = detail.get("payer_info") or {}
    pn = pi.get("payer_name") or {}
    cands = [pn.get("alternate_full_name"), " ".join(x for x in (pn.get("given_name"), pn.get("surname")) if x),
             (detail.get("shipping_info") or {}).get("name"), (detail.get("store_info") or {}).get("store_name")]
    for c in cands:
        if isinstance(c, str) and c.strip():
            return " ".join(c.split())[:80]
    return ""


def normalize(detail, keep_identity=True):
    """One reporting row -> {ref, ts, recipient, amount, currency, payee_name, memo, category} for an OUTGOING payment to someone, else None.
    keep_identity=False reproduces the earlier lossy ingest (email + amount + ts only); it exists so the benchmark can measure what the
    name/currency/memo/category fields are worth."""
    ti = detail.get("transaction_info") or {}
    code = str(ti.get("transaction_event_code") or "")
    if code.startswith(FEE_OR_RESERVE_PREFIXES):
        return None
    try:
        amt = float((ti.get("transaction_amount") or {}).get("value"))
        ts = _parse(ti.get("transaction_initiation_date"))
    except (TypeError, ValueError):
        return None
    email = ((detail.get("payer_info") or {}).get("email_address") or "").strip().lower()
    custom = str(ti.get("custom_field") or "")
    if amt >= 0 or not email or custom.startswith("pay-"):        # inflow / no counterparty / sent by PayCanary itself
        return None
    row = {"ref": ti.get("transaction_id"), "ts": ts, "recipient": email, "amount": -amt, "currency": "USD", "payee_name": "", "memo": "", "category": ""}
    if keep_identity:
        items = (detail.get("cart_info") or {}).get("item_details") or [{}]
        row.update(currency=str((ti.get("transaction_amount") or {}).get("currency_code") or "USD").upper()[:3], payee_name=_name(detail),
                   memo=" ".join(str(ti.get("transaction_note") or ti.get("transaction_subject") or "").split())[:120],
                   category=" ".join(str((items[0] or {}).get("item_name") or "").split())[:60])
    return row


def fetch(paypal, start_ts, end_ts, max_pages=40):
    rows, pages = [], 0
    lo = start_ts
    while lo < end_ts:
        hi = min(end_ts, lo + WINDOW_DAYS * 86400 - 1)
        page = 1
        while True:
            st, body = paypal.request("GET", "/v1/reporting/transactions?start_date=%s&end_date=%s&fields=all&page_size=100&page=%d" % (_iso(lo), _iso(hi), page))
            if st != 200 or not isinstance(body, dict):
                raise RuntimeError("reporting HTTP %s" % st)
            for d in body.get("transaction_details", []):
                n = normalize(d)
                if n:
                    rows.append(n)
            pages += 1
            if page >= int(body.get("total_pages") or 1) or pages >= max_pages:
                break
            page += 1
        lo = hi + 1
    return rows


def refresh(store, paypal, days=90, now=None):
    """Pull the last `days` of outflows into the store. Never raises."""
    now = now or time.time()
    try:
        rows = fetch(paypal, now - days * 86400, now)
    except Exception as err:                                     # unavailable scope, 403, network, malformed: fall back silently
        return {"available": False, "reason": str(err)[:120]}
    new = store.ext_add(rows)
    return {"available": True, "fetched": len(rows), "new": new}
