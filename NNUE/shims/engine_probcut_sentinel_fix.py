"""NNUE/shims/engine_probcut_sentinel_fix.py -- bug hunt 07.11.2026 A/B, PROBCUT_SENTINEL_FIX ON.

Identical to cengine.py except PROBCUT_SENTINEL_FIX (ProbCut stage 2 passes the capture, not the null sentinel).
Base arm: cengine.py itself.

ProbCut's verification search told its child the parent played a null move,
blocking null move there (448 of 1,359 bench stage-2 searches). Bench 593,597
on vs 593,598 off.

NULL 25.11.2026: --nodes 1750000, 127 workers on 2x EPYC 7543, full 10,000
games, -0.31 +/- 4.3 Elo, SPRT [0,4] normalized LLR -0.827, no bound. Not a
positive NULL, so it does not go to docs/null_bundle.md. State file
NNUE/campaigns/sprt_probcut_sentinel_fix_nodes1.75M.json.
"""

import cengine


class Engine(cengine.Engine):
    PROBCUT_SENTINEL_FIX = True
