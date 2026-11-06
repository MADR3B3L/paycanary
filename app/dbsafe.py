"""A sqlite3 connection shared across threads, made safe: every execute runs under one re-entrant lock and its rows are fetched before the lock is
released. The stores open one connection with check_same_thread=False and used it from the request threads, the cooling-off sweeper and background
enrichment threads; reads outside the lock raced with writes (sqlite3.InterfaceError / 'recursive use of cursors'). Callers keep using
db.execute(...).fetchone()/fetchall()/iteration/.rowcount/.lastrowid unchanged."""
import sqlite3
import threading


class _Result:
    __slots__ = ("_rows", "rowcount", "lastrowid", "_i")

    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid, self._i = rows, rowcount, lastrowid, 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None

    def fetchall(self):
        out, self._i = self._rows[self._i:], len(self._rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class LockedDB:
    def __init__(self, path, lock=None):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.lock = lock or threading.RLock()

    def execute(self, sql, args=()):
        with self.lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if cur.description else []
            return _Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script):
        with self.lock:
            self._conn.executescript(script)

    def commit(self):
        with self.lock:
            self._conn.commit()

    def close(self):
        with self.lock:
            self._conn.close()
