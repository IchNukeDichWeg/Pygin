"""engine_nnue_v5.py -- the v5 net against the shipped v58 (v4 net).

Same dataset and same dimensions as v4, so the NPS is unchanged and the only
difference between this and Old Engine/58 is the weights. v5 came out of an
8-epoch cosine schedule where v4 used 40: held-out val 0.066663 -> 0.063676.

    python3 match.py engine_nnue_v5.py "Old Engine/58/engine58.py" 1000 \
        --workers 0 --sprt

The epoch sweep behind it is CLOSED, 8 is the minimum (all full data, same
val set): 6 = 0.063989, 8 = 0.063676, 12 = 0.063858, 16 = 0.064264,
40 = 0.066663. Longer runs drive train loss down and val loss up, and the
quantization error grows with them (12.49 cp at 8 epochs, 26.88 at 16), so
there is nothing left to win by training this architecture for longer.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v5_aa89303bc7e9.nnue"
    LAZY_NNUE = True
