"""engine_nnue_v5.py -- the v5 net against the shipped v58 (v4 net).

Same dataset and same dimensions as v4, so the NPS is unchanged. v5 came out
of an 8-epoch cosine schedule where v4 used 40: held-out val 0.066663 ->
0.063676.

CONFOUNDED -- READ THE VERDICT WITH CARE (found 2026-08-09). This file sets
LAZY_NNUE = True; the baseline it was measured against, Old Engine/58, has it
False. So the +3.09 +/- 4.5 null over 10,000 games (LLR flat +0.838 ->
+0.871, sprt_v5_epyc_*.json) is a measurement of NET **plus** FI-106 lazy
evaluation versus neither -- NOT of the weights alone, which is what an
earlier version of this docstring claimed.

The size of the correction is unknown, because LAZY_NNUE has never been
measured in isolation on either architecture: the numbers recorded for FI-106
in cengine.py (+19.30 arm64, +5.91 x86) come from engine_nnue_lazy.py -- the
v3 net WITH lazy -- against pre-NNUE HCE baselines (Old Engine/56, v55), so
they price the whole NNUE-plus-lazy package, not the toggle.

What survives: v5 was rejected and stays rejected, since a null on the
package is no reason to adopt it, and the "validation loss is a weak
predictor near the floor" lesson holds -- the package with the better val
number still failed to beat v58. What does not survive is reading +3.09 as
"the v5 weights are worth nothing".

    python3 match.py NNUE/shims/engine_nnue_v5.py "Old Engine/58/engine58.py" 1000 \
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
