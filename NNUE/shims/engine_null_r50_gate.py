"""NNUE/shims/engine_null_r50_gate.py -- bug hunt 21.11.2026 A/B, NULL_R50_GATE ON.

Identical to cengine.py except NULL_R50_GATE (no null move at a 50-move clock of 90 or more).
Base arm: cengine.py itself.

The null child searched with a 50-move clock of 0, hiding the 50-move draw
below every null move. Fires only at hmc >= 90, i.e. late in long games, so
the A/B needs whole games (--nodes or TIMED), not a bench.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    NULL_R50_GATE = True
