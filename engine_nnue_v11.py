"""engine_nnue_v11.py -- the v11 net against Old Engine/59, BOTH lazy.

Deep labels, second attempt -- this time with the recipe that v4 used.
v6 was gen10k (52.1M positions, 10,000-node labels) trained on the
8-epoch cosine, and it was REJECTED at -10.51 +/- 9.5 (LLR -2.944). But
every 8-epoch net on this ladder has lost, and the only winner ever
trained was 40-epoch, so v6's rejection never separated "deep labels are
wrong" from "the recipe was wrong". v11 is v6's exact dataset and dims on
the 40-epoch schedule, so against v6 it varies the recipe alone.

    python3 match.py engine_nnue_v11.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 0 --tc 50+0.5 --seed 59 --sprt

Held-out val 0.067995 (epoch 9 of 40) against v6's 0.064895 on the same
corpus -- the longer schedule trains a WORSE net by val, which is now the
pattern on every corpus tested (gen5k_mix: v8 0.063325 at 8 epochs vs v9
0.066154 at 40; gen10k: v6 0.064895 vs v11 0.067995). Since the 8-epoch
nets holding the best val numbers are exactly the ones that lost their
A/Bs, val is running anti-correlated with Elo here and is recorded as an
observation, not a forecast.

Of the four nets trained in this batch, v11 was the most ROBUST on
held-out scoring: 1st on the gen10k holdout (R^2 0.9254) and 2nd on the
ab_logs holdout (0.7919), pure-cp targets, where every other net won only
on its own distribution. Holdout R^2 has never been calibrated against
Elo, so this ranks the candidates rather than predicting any of them.
Quantization MAE 15.37 cp, squarely normal.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v11_553e11f26755.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
