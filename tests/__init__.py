"""Test-suite resource hygiene.

Every sqlite connection the code under test opens (app.dbsafe.LockedDB: the Store, the reputation store, the score_* runners
the benchmark tests call) is closed when the test that opened it finishes, instead of being left for the garbage collector,
which prints a ResourceWarning ("unclosed database") for each one. Nothing is silenced: the connection is really closed.

Mechanism: while a test runs, new LockedDB objects register their own close() as a cleanup of that test (addCleanup), so it runs
after tearDown. A connection opened outside any test (import time, setUpClass) is held and closed at interpreter exit.
close_when_done(obj) does the same for any other closable a test creates (e.g. the HTTPError objects the fake HTTP openers raise).
"""
import atexit
import unittest

from app import dbsafe

_current = None          # the TestCase being run right now, or None
_orphans = []            # closables created outside any test; closed at exit


def _close(obj):
    try:
        obj.close()
    except Exception:    # already closed / never fully constructed: nothing left to release
        pass


def close_when_done(obj):
    """Close `obj` when the current test finishes (or at exit if called outside a test). Returns obj."""
    if _current is not None:
        _current.addCleanup(_close, obj)
    else:
        _orphans.append(obj)
    return obj


def read_file(path, mode="r"):
    """Read a whole file and close it (a bare open(path).read() leaves the file for the garbage collector: ResourceWarning)."""
    with open(path, mode) as fh:
        return fh.read()


_orig_init, _orig_run = dbsafe.LockedDB.__init__, unittest.TestCase.run


def _init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    close_when_done(self)


def _run(self, result=None):
    global _current
    previous, _current = _current, self
    try:
        return _orig_run(self, result)
    finally:
        _current = previous


dbsafe.LockedDB.__init__ = _init
unittest.TestCase.run = _run
atexit.register(lambda: [_close(o) for o in _orphans])
