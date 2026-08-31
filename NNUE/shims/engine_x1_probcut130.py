"""NNUE/shims/engine_x1_probcut130.py -- X1: ProbCut margin under the NNUE eval.

v61 exactly, with PROBCUT_MARGIN 200 -> 130. One variable.

WHY THIS IS A NEW EXPERIMENT, not a re-run. ProbCut shipped as v56
(2026-07-30); the net arrived at v58 (2026-08-03). Its margin was therefore
chosen while prune_eval was the Texel HCE, and prune_eval has been NNUE
output ever since -- a different scale and a different noise distribution.
Verified 2026-08-29: PROBCUT_MARGIN has ZERO mentions in improvements.md, so
unlike the nine P-26 knobs it has never been swept, screened or parked, and
the 2026-07-21 "parameter plateau is REAL" finding does not cover it (that
sweep predates ProbCut existing).

WHY DOWN, NOT UP. The margin is how far above beta prune_eval must sit before
the probe is attempted -- it is a trust threshold on the static eval. A
sharper eval justifies probing on weaker evidence, which is why strong
engines pair good nets with tight ProbCut margins. 130 tests exactly that:
"the net lets us probe more often". If it reads negative the same logic runs
upward (300) as the follow-up, but never both in one campaign.

    python3 match.py NNUE/shims/engine_x1_probcut130.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Honest prior: a margin knob, not a mechanism -- expect 0 to +3, and a null is
the most likely single outcome. It earns its slot on being unmeasured and
free, not on a strong prior.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True      # v61 baseline
    PROBCUT_MARGIN = 130   # X1: the one variable (shipped 200)
