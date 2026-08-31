"""NNUE/shims/engine_x2_lazy300.py -- X2: the lazy-NNUE trust margin.

v61 exactly, with LAZY_NNUE_MARGIN 200 -> 300. One variable.

WHAT THE KNOB DOES. With LAZY_NNUE armed (v59, +13.84 isolated), a node whose
cheap material+PST eval lands more than MARGIN outside [alpha,beta] skips the
net entirely and uses the cheap bound. At the shipped 200 the repo measured
HALF of all NN evals skippable, with 1.72% landing on the wrong side
(csearch.c:1573-1575). The toggle was confirmed; the MARGIN itself has never
been swept -- verified 2026-08-29, and it postdates the P-26 sweep entirely,
so no plateau finding covers it.

WHY UP, NOT DOWN. Lowering the margin buys NPS by skipping more evals; the
NPS lane converts at only ~1.16 Elo per 1%. Raising it buys eval ACCURACY on
prune_eval -- and prune_eval is consumed by four pruning mechanisms at once
(RFP, null, frontier futility, ProbCut), so a wrong-side bound is amplified
four ways. This engine's record says accuracy has paid better than speed
(search lane 2-for-7, NPS conversion measured minor), which is the case for
testing the conservative direction first. 300 should cut the wrong-side rate
substantially while giving back part of the skip population.

    python3 match.py NNUE/shims/engine_x2_lazy300.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

TIMED instrument, mandatory: this trades nodes-per-second against per-node
quality, so a --nodes run would see only the quality half and flatter it.
If this reads positive, 400 is the next point; if negative, 120 tests the
speed direction instead. One direction per campaign.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True         # v61 baseline
    LAZY_NNUE_MARGIN = 300    # X2: the one variable (shipped 200)
