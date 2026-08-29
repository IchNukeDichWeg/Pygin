"""NNUE/shims/engine_p4_ttpvlmr.py -- P4: ttPv LMR discount. RUN AFTER P3.

v61 exactly, plus TTPV_LMR on: R -= 1 at nodes whose sticky ttpv bit is set
(TT d2 bit 18, sticky OR at probe; agent-verified 2026-08-29). This is SF's
designated counterweight to cutnode reduction -- its job is making P3's
harder pruning safe in once-important lines, so its verdict only means
something measured ON TOP of a landed P3, never batched with it and never
first. The 2026-07-29 pair screen (-0.69, EBF -7.49%) predates v56/NNUE.

    python3 match.py NNUE/shims/engine_p4_ttpvlmr.py NNUE/shims/engine_p3_cutlmr.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

NOTE the baseline in that command is the P3 shim, not v61 -- if P3 is
rejected, this shim is moot (its lone value read -0.76 in the old tranches).
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline
    CUTNODE_LMR = True    # carries P3, per the pairing doctrine
    TTPV_LMR = True       # P4: the added variable vs engine_p3_cutlmr
