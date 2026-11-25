"""NNUE/shims/engine_abort_faillow_keep.py -- bug hunt 07.11.2026 A/B, ABORT_FAILLOW_KEEP ON.

Identical to cengine.py except ABORT_FAILLOW_KEEP (keep the previous best on an aborted fail-low iteration).
Base arm: cengine.py itself.

An aborted iteration whose searched moves all failed low played the partial
move with the highest upper bound. ~2% of node-limited searches; 7 of 8 played
moves scored worse at d13. Fires on aborts only, so --nodes play reaches it
directly.

NULL 25.11.2026: --nodes 1750000, 127 workers on 2x EPYC 7543, full 10,000
games, -0.97 +/- 4.2 Elo, SPRT [0,4] normalized LLR -1.187, no bound. Not a
positive NULL, so it does not go to docs/null_bundle.md. State file
NNUE/campaigns/sprt_abort_faillow_keep_nodes1.75M.json.
"""

import cengine


class Engine(cengine.Engine):
    ABORT_FAILLOW_KEEP = True
