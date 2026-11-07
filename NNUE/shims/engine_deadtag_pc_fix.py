"""NNUE/shims/engine_deadtag_pc_fix.py -- bug hunt 07.11.2026 A/B, DEADTAG_PC_FIX ON.

Identical to cengine.py except DEADTAG_PC_FIX (FI-115's piece-count tag recounted at each store).
Base arm: cengine.py itself.

FI-115's tag carried the last descendant's piece count on 66% of negamax
stores. It only decides under replacement pressure, so like FI-115 itself this
needs the shipped table at 50+0.5 TIMED, not a --nodes screen.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    DEADTAG_PC_FIX = True
