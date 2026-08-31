"""NNUE/shims/engine_b06_pcstamp.py -- pointer to the combined FI-115 A/B.

The piece-count stamping fix is a C change with no toggle of its own, and it
is not independently measurable: with correct tags but the rule still confined
to qsearch, the two highest-volume store sites remain unprotected, so the
isolated number would price neither the bug nor the fix.

Use NNUE/shims/engine_b05_deadtag_all.py, which measures both halves against
the frozen v61 snapshot -- see that file for the full rationale and the
instrumented before/after figures.

This shim exists so the reference in the stamping commit resolves to
something, and so nobody wires up a misleading solo run.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True
