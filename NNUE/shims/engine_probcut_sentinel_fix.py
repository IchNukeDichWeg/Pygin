"""NNUE/shims/engine_probcut_sentinel_fix.py -- bug hunt 07.11.2026 A/B, PROBCUT_SENTINEL_FIX ON.

Identical to cengine.py except PROBCUT_SENTINEL_FIX (ProbCut stage 2 passes the capture, not the null sentinel).
Base arm: cengine.py itself.

ProbCut's verification search told its child the parent played a null move,
blocking null move there (448 of 1,359 bench stage-2 searches). Bench 593,597
on vs 593,598 off.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    PROBCUT_SENTINEL_FIX = True
