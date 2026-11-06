"""One safe way for a test to override gate constants. Restoration is guaranteed (including on error), the History constructor's default arguments (bound
at definition time) are re-pointed by PARAMETER NAME rather than by value, and a lock stops two overrides from interleaving."""
import contextlib
import inspect
import threading

from app import gate as G

_LOCK = threading.RLock()


@contextlib.contextmanager
def gate_overrides(**consts):
    with _LOCK:
        saved = {k: getattr(G, k) for k in consts}
        init = G.History.__init__
        saved_defaults = init.__defaults__
        try:
            for k, v in consts.items():
                setattr(G, k, v)
            params = [p for p in inspect.signature(init).parameters.values() if p.default is not inspect.Parameter.empty]
            init.__defaults__ = tuple(consts.get(p.name.upper(), p.default) for p in params)
            yield
        finally:
            for k, v in saved.items():
                setattr(G, k, v)
            init.__defaults__ = saved_defaults
