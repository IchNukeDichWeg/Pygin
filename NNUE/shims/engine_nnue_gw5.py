"""NNUE/shims/engine_nnue_gw5.py -- gw5: gen200m, seed 5, --wd 1e-4.

seed 5 with --wd 1e-4. The weight-decay arm: it did NOT fix int8
saturation (6.745% of w1 pinned at the clip vs the no-decay g-nets'
7.30%, against v12's 0.023%) -- 1e-4 is ~two orders too weak against
clip_weights(). Val was unharmed and is the best of any gen200m net,
so this measures the CORPUS again with a near-null decay, not the fix.

Recipe otherwise IDENTICAL to s1-s4/m1-m4/g1-g3: 40-epoch cosine, d2 16,
lambda 0.75, so this pools with every earlier screen.

    python3 match.py NNUE/shims/engine_nnue_gw5.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Trained val 0.058947 at epoch 38 of 40. Val is 0-for-11 as an Elo predictor
here and eval_bench MAE calibrated NEGATIVE -- only the A/B decides.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_gw5_cd166880cd6d.nnue"
    LAZY_NNUE = True     # matches the baseline; identical on both sides
