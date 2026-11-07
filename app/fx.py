"""Coarse, static, operator-overridable reference rates used ONLY to compare a foreign-currency payment against caps and windows that the
operator sets in USD. Baselines and ratchets are judged per currency in that currency's own units (a JPY payment is compared with JPY history);
this table exists so that a Y520,000 payout (about $3,500) is not frozen against a $20,000 cap just because 520000 > 20000 as raw numbers.
It is NOT live FX and is not used to move money: PayPal converts at its own rate. Override per currency with PAYCANARY_FX_JSON
(e.g. '{"JPY": 0.0065}'). A currency not in the table is refused."""
import json
import math
import os

USD_PER_UNIT = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "CAD": 0.73, "AUD": 0.65}
ZERO_DECIMAL = {"JPY"}                       # PayPal takes whole numbers only for these


def table():
    t = dict(USD_PER_UNIT)
    raw = os.environ.get("PAYCANARY_FX_JSON", "")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                cur = k.upper()
                if cur == "USD":
                    continue                                              # USD is the unit of account: it is ALWAYS 1.0 and can never be redefined (an override of {"USD":0.01} would let every cap be bypassed)
                if cur in t:                                              # overrides only adjust KNOWN currencies; they cannot add new ones
                    rate = float(v)
                    if math.isfinite(rate) and rate > 0:                  # a rate must be a finite positive number; NaN/Inf/<=0 is ignored, never trusted
                        t[cur] = rate
        except (ValueError, TypeError, AttributeError):
            pass                                                          # a malformed override is ignored, never trusted
    t["USD"] = 1.0                                                        # belt-and-suspenders: USD stays fixed whatever the override tried
    return t


def supported():
    return set(USD_PER_UNIT)


def to_usd_rate(currency):
    return table().get(str(currency or "USD").upper())
