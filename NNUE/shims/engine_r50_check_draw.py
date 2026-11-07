"""NNUE/shims/engine_r50_check_draw.py -- bug hunt 07.11.2026 A/B, R50_CHECK_DRAW ON.

Identical to cengine.py except R50_CHECK_DRAW (the in-check 50-move draw).
Base arm: cengine.py itself.

At hmc >= 100 an in-check node with a legal evasion scores the draw instead
of playing on. Bench 593,598 either way; the population is late endgames only,
so expect a NULL unless the phantom-mate lines show up in games.

PENDING. Verdict goes here and into the toggle's comment in cengine.py.
"""

import cengine


class Engine(cengine.Engine):
    R50_CHECK_DRAW = True
