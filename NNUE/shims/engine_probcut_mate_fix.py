"""NNUE/shims/engine_probcut_mate_fix.py -- bug hunt 21.11.2026 A/B, PROBCUT_MATE_FIX ON.

Identical to cengine.py except PROBCUT_MATE_FIX (a confirmed ProbCut returns a mate score unchanged).
Base arm: cengine.py itself.

ProbCut returned v - (pbeta - beta) for mate scores as well, a fake mate
distance that parent TT entries carry as a mate-range bound. Bench 593,598
either way: it only fires where ProbCut confirms a mate, so a --nodes screen
reaches it directly.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    PROBCUT_MATE_FIX = True
