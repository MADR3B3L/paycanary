"""Where the audit log's HMAC key and anchor file live: OUTSIDE the SQLite database, so a person or process that can only read/write the DB file cannot
forge the chain or hide a truncation. Custody, in order of strength:
  1. PAYCANARY_AUDIT_KEY (>=16 chars) from a secret manager / the service's environment: the key never touches disk next to the data.
  2. PAYCANARY_AUDIT_KEY_FILE, or the default ~/.paycanary/audit.key (created 0600 in a 0700 dir on first run): separate from the DB directory and from backups of it.
The anchor (seq + row MAC + line MAC per append) goes to PAYCANARY_AUDIT_ANCHOR or ~/.paycanary/anchors/<hash of the DB path>.log. For the strongest guarantee
ship that file's lines to storage the PayCanary host cannot rewrite. An in-memory database is not keyed (nothing persists, nothing to protect)."""
import hashlib
import os
import secrets


def _home_dir():
    return os.path.join(os.path.expanduser("~"), ".paycanary")


def load(env, db_path):
    """-> (key bytes | None, anchor path | None) for this process. Never prints or logs the key."""
    if not db_path or db_path == ":memory:" or str(db_path).startswith("file::memory:"):
        return None, None
    key = (env.get("PAYCANARY_AUDIT_KEY") or "").strip().encode() or None
    if key is not None and len(key) < 16:
        raise ValueError("PAYCANARY_AUDIT_KEY must be at least 16 characters")
    if key is None:
        kf = env.get("PAYCANARY_AUDIT_KEY_FILE") or os.path.join(_home_dir(), "audit.key")
        if os.path.exists(kf):
            key = open(kf, "rb").read().strip()
            if len(key) < 16:
                raise ValueError("the audit key file is too short; refusing to use a weak key")
        else:
            os.makedirs(os.path.dirname(kf) or ".", mode=0o700, exist_ok=True)
            key = secrets.token_hex(32).encode()
            try:
                fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:                                   # another process booted first: adopt THEIR key (read it back; never overwrite)
                import time
                for _ in range(50):
                    key = open(kf, "rb").read().strip()
                    if len(key) >= 16:
                        break
                    time.sleep(0.05)                                  # the winner may still be writing it
                if len(key) < 16:
                    raise ValueError("the audit key file is too short; refusing to use a weak key")
            else:
                try:
                    os.write(fd, key)
                finally:
                    os.close(fd)
    anchor = env.get("PAYCANARY_AUDIT_ANCHOR") or os.path.join(_home_dir(), "anchors", hashlib.sha256(os.path.abspath(str(db_path)).encode()).hexdigest()[:16] + ".log")
    return key, anchor
