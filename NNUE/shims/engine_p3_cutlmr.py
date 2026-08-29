"""NNUE/shims/engine_p3_cutlmr.py -- P3: cutnode LMR on the post-NNUE tree.

v61 exactly, plus CUTNODE_LMR on: R += (cutnode && !is_pv) at csearch.c:4444.
FI-103 closed 'consistently positive, never confirmable' at +2.84 +/-4.6 over
22,000 TIMED games with an explicit re-look-if-the-tree-gets-cheaper clause.
Agent-verified 2026-08-29: that closure ran on the POST-ProbCut tree already
(shipped 2026-07-30, closed 07-31) -- the changes that satisfy the clause are
NNUE v58, lazy-NNUE v59 and dead-tag v61, which is still a different tree.

    python3 match.py NNUE/shims/engine_p3_cutlmr.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Sequencing: run THIS before engine_p4_ttpvlmr.py -- P4's value is conditional
on P3 landing (SF pairs them as opposing forces, never batched).
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline
    CUTNODE_LMR = True    # P3: the one variable
