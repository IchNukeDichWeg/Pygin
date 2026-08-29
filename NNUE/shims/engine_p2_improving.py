"""NNUE/shims/engine_p2_improving.py -- P2/S4: arm improving. DO NOT A/B YET.

v61 exactly, plus g_improving armed (there is no class attr; the sole push
site is the literal ('set_improving', 0) in cengine's config tuple, so this
shim re-pushes 1 after construction).

BLOCKED, and the block is measured, not speculative (agent-verified
2026-08-29, both from the code):
  1. The frontier-futility improving leg is SIGN-INVERTED: it adds
     g_rfp_margin/2 to the margin when NOT improving, making the prune
     HARDER -- declining nodes prune FEWER quiets, the inverse of the
     documented intent (csearch.c:3081-3082 vs :4421-4425; the engine.py
     port carries the same inversion).
  2. g_seval mixes value families under NNUE+lazy: root seeds HCE
     eval_full_stm, tree plies record nn_eval, and lazy-fired nodes record
     the cheap window-relative BOUND with no guard -- so the two-ply trend
     compares numbers on different scales.
An A/B of this shim today measures that defective recipe, not the improving
idea. Fix both (a sign flip and a lazy/root guard, each node-identical while
improving stays off) and re-verify before spending games. The +0.38 +/-6.8
dead null on record is v34-era and does not price the fixed version.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._lib.set_improving(1)   # P2: the one variable (no attr exists)
