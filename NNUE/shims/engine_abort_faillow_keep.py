"""NNUE/shims/engine_abort_faillow_keep.py -- bug hunt 07.11.2026 A/B, ABORT_FAILLOW_KEEP ON.

Identical to cengine.py except ABORT_FAILLOW_KEEP (keep the previous best on an aborted fail-low iteration).
Base arm: cengine.py itself.

An aborted iteration whose searched moves all failed low played the partial
move with the highest upper bound. ~2% of node-limited searches; 7 of 8 played
moves scored worse at d13. Fires on aborts only, so --nodes play reaches it
directly.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    ABORT_FAILLOW_KEEP = True
