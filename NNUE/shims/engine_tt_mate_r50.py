"""NNUE/shims/engine_tt_mate_r50.py -- bug hunt 21.11.2026 A/B, TT_MATE_R50 ON.

Identical to cengine.py except TT_MATE_R50 (a TT mate that no longer fits before the 50-move draw does not cut).
Base arm: cengine.py itself.

Stored mates were replayed at any 50-move clock, so a defender near hmc 100
could take a stored "mated in k" over the draw. Only fires late in long
games with a warm table, so the A/B needs whole games: --nodes across the
game or TIMED, not a bench.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    TT_MATE_R50 = True
