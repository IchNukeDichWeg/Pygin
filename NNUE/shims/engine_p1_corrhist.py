"""NNUE/shims/engine_p1_corrhist.py -- P1/S1: correction history under NNUE.

v61 exactly, plus CORR_HIST on (FI-109 re-arm). The machinery is fully built
and dormant; agent-verified 2026-08-29 (apply csearch.c:4195-4205, update
:4550-4558, g_corr[2][16384] grain 32 clamp 512, cap 64). The HCE-era closure
docstring itself resets on an eval-family change, and nnue_v12's measured
endgame bias (+400cp on textbook draws) is the systematic-error shape a
pawn-key corrector exists to cancel.

    python3 match.py NNUE/shims/engine_p1_corrhist.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Known coverage caveats (verified, accepted for this A/B): the update site
skips PV nodes under shipped defaults (needs improving armed for PV coverage)
and skips lazy-eval nodes (!lazy_used gate) -- both mean the corrector learns
from a subset of nodes. That is the recipe being measured, on purpose.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline
    CORR_HIST = True      # P1: the one variable
