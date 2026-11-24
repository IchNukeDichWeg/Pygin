"""NNUE/shims/engine_deadtag_pc_fix.py -- bug hunt 07.11.2026 A/B, DEADTAG_PC_FIX ON.

Identical to cengine.py except DEADTAG_PC_FIX (FI-115's piece-count tag recounted at each store).
Base arm: cengine.py itself.

FI-115's tag carried the last descendant's piece count on 66% of negamax
stores. It only decides under replacement pressure, so like FI-115 itself this
needs the shipped table at 50+0.5 TIMED, not a --nodes screen.

REJECTED 24.11.2026: timed 50+0.5, 64 workers on 2x EPYC 7543, SPRT [0,4]
normalized ACCEPT H0 at 6,046 games, -7.87 +/- 5.3 Elo (stopped early, so the
size is biased; the sign is the verdict). State file
NNUE/campaigns/sprt_deadtag_pc_fix_tc50+0.5.json. FI-115 was measured and won
with the low tag; the true count evicts entries it should keep.
"""

import cengine


class Engine(cengine.Engine):
    DEADTAG_PC_FIX = True
