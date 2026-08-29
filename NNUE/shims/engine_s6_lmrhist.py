"""NNUE/shims/engine_s6_lmrhist.py -- S6: history-fed LMR at a LIVE divisor.

v61 exactly, plus LMR_HIST = 512. The toggle IS the divisor (csearch.c:4448
footgun): R -= clamp(g_history/512, +/-1). Divisor chosen by MEASUREMENT,
2026-08-29 on this Mac (kiwipete, fixed depth, fresh process per config):
the audit proposed '8192-class' but 8192 is the repo's recorded dead gate,
and 2048 measured node-identical to baseline here even at depth 11
(390,817 = off) -- dead on the NNUE tree too. 512 engages from depth ~11
(391,211 vs 390,817; identical at depth 9), 128 engages by depth 9. 512 is
the strongest divisor that both engaged historically and verifiably fires
under the current tree at match depths.

    python3 match.py NNUE/shims/engine_s6_lmrhist.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

History honestly stated: the quiet-signal vein is 0-for-4 here (FI-04 dead
gate, live-divisor rerun -1.51 CLOSED) -- every read was a LONE signal on the
pre-v56 tree. This is the R10 cheapest-falsifier slot, not a high prior.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline
    LMR_HIST = 512        # S6: the one variable (0 = off; the value IS the divisor)
