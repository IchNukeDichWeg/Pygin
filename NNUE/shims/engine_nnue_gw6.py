"""NNUE/shims/engine_nnue_gw6.py -- gw6: gen200m, seed 6, --wd 1e-4.

seed 6 with --wd 1e-4. Its twin gw6r -- the SAME invocation run again --
produced a BYTE-IDENTICAL net (sha256 6fb70e9fe4e6...), proving the
trainer is bit-reproducible on this hardware and that the ~31 Elo spread
between seeds is the SEED, not trainer noise. gw6r therefore needs no
A/B of its own: it is the same file.

Recipe otherwise IDENTICAL to s1-s4/m1-m4/g1-g3: 40-epoch cosine, d2 16,
lambda 0.75, so this pools with every earlier screen.

    python3 match.py NNUE/shims/engine_nnue_gw6.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Trained val 0.059052 at epoch 39 of 40. Val is 0-for-11 as an Elo predictor
here and eval_bench MAE calibrated NEGATIVE -- only the A/B decides.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_gw6_6fb70e9fe4e6.nnue"
    LAZY_NNUE = True     # matches the baseline; identical on both sides
