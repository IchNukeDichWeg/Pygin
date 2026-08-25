"""NNUE/shims/engine_nnue_g1.py -- seed 1 on gen200m, the volume+tablebase corpus.

200,000,080 positions at 5,000 nodes (3.1M self-play games), with every
position of 5 men or fewer labelled from Syzygy instead of the search: BOTH
score and result, so LAMBDA does not keep 75% of a wrong number. ~8.6M records
carry tablebase truth, 53.3% of them decisive. Recipe is otherwise IDENTICAL
to the s1-s4 seed screen -- 40-epoch cosine, d2 16, lambda 0.75 -- so g vs s
is a clean read on the corpus, with the seed spread measured on both sides.

    python3 match.py NNUE/shims/engine_nnue_g1.py NNUE/shims/engine_nnue_v12.py \
        2500 0 --workers 0 --tc 10+0.1 --seed 59 --sprt --sprt-min-pairs 1500

Trained val 0.059137 at epoch 35 of 40 (crashed at epoch 36 on the all-val-chunk bug fixed in 4906878; best.pt recovered, and 35 was its best epoch anyway).
Val is NOT a strength signal here: it has been inverted or inert 8 times, and
eval_bench MAE calibrated NEGATIVE over 12 nets. Only the A/B decides.

What the corpus demonstrably fixed: the train/val overfit gap at epoch 9 was
10.9% against s1's 22.0%, and these nets were still setting best-val past
epoch 30 where all eight of s1-s4/m1-m4 peaked by epoch 17.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_g1_7d4762b73c55.nnue"
    LAZY_NNUE = True     # matches the baseline; identical on both sides
