"""Benchmarks with cooling-off turned ON for every agent (what assigning a preset does), with the opt-in probation_hold flag off or on.
The headline benchmarks run with cooldown off (the shipped default for agents with no profile); this shows the cooldown-on cost honestly.
Usage:  python3 tools/score_cooldown_on.py [--hold]      (--hold = probation_hold ON). Cooling-off holds count as freezes (and as nags when benign)."""
import os
import runpy
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import gate as G  # noqa: E402

HOLD = "--hold" in sys.argv
_orig = G.History.__init__


def _init(self, *a, **k):
    k["cooldown_s"] = k.get("cooldown_s") or 900.0
    k["probation_hold"] = HOLD
    _orig(self, *a, **k)


G.History.__init__ = _init
print("cooldown_s=900 on every agent; probation_hold=%s" % ("ON" if HOLD else "OFF"))
here = os.path.dirname(os.path.abspath(__file__))
for tool in ("score_benchmark.py", "score_realworld_service.py"):
    print("== " + tool)
    sys.argv = [tool]
    try:
        runpy.run_path(os.path.join(here, tool), run_name="__main__")
    except SystemExit:
        pass
