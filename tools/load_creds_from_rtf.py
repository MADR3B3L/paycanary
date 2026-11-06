#!/usr/bin/env python3
"""Writes a gitignored .env from the two RTF files you saved on the Desktop — WITHOUT printing any secret.
Run it yourself:  python3 tools/load_creds_from_rtf.py
  - "PayCanary Paypal API Credentials.rtf"        -> PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET
  - "PayCanary Sandbox fake user login.rtf"       -> PAYOUT_RECIPIENT_EMAIL (the e-mail only; the password is never read into .env)
If a value can't be found automatically it asks (hidden input). .env is created with mode 600."""
import getpass
import os
import re
import sys
from pathlib import Path

DESK = Path.home() / "Desktop"
ROOT = Path(__file__).resolve().parent.parent
CREDS = DESK / "PayCanary Paypal API Credentials.rtf"
USER = DESK / "PayCanary Sandbox fake user login.rtf"


def plain(path: Path) -> str:
    """Very small RTF -> text (control words dropped)."""
    if not path.is_file():
        return ""
    t = path.read_text(errors="ignore")
    t = re.sub(r"\\'[0-9a-fA-F]{2}", "", t)
    t = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", t)
    t = re.sub(r"[{}]", " ", t)
    return t


def after_label(text: str, label_re: str):
    m = re.search(label_re + r"[\s:=\-\\]*([A-Za-z0-9_\-]{20,})", text, re.I)
    return m.group(1) if m else None


def main() -> int:
    t = plain(CREDS)
    cid = after_label(t, r"client\s*id")
    sec = after_label(t, r"secret(?:\s*key)?\s*\d*")
    if not cid:
        cid = getpass.getpass("PAYPAL_CLIENT_ID (hidden): ").strip()
    if not sec:
        sec = getpass.getpass("PAYPAL_CLIENT_SECRET (hidden): ").strip()
    mail = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", plain(USER))
    email = mail.group(0) if mail else input("PAYOUT_RECIPIENT_EMAIL (sandbox personal account e-mail): ").strip()
    env = ROOT / ".env"
    env.write_text("PAYPAL_ENV=sandbox\nPAYPAL_CLIENT_ID=%s\nPAYPAL_CLIENT_SECRET=%s\nPAYOUT_RECIPIENT_EMAIL=%s\nANTHROPIC_API_KEY=\n" % (cid, sec, email))
    os.chmod(env, 0o600)
    print(".env written (mode 600). client id ends …%s; recipient %s…; nothing secret was printed." % (cid[-4:], email[:3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
