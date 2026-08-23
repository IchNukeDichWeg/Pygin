"""engine_nnue_lazy.py -- the NNUE engine with FI-106 lazy eval armed.

The B arm of the FI-106 screen. A is engine_nnue.py: SAME net, same nodes,
same everything -- the only difference between the two processes is that this
one is allowed to skip the net when a free bound already answers the search's
question. That isolation is the point; a screen that also changed the net
would not be measuring lazy eval.

    python3 match.py engine_nnue_lazy.py engine_nnue.py 1000 --workers 0 \
        --nodes 1750000 --sprt --sprt-resume fi106.json

What it is expected to buy, from FI-105's 281,700-eval measurement: margin
200 skipped 50.9% of NN evaluations, worth ~10.9% of NPS back, ~+12.6 Elo at
the repo's 1.16 Elo/%NPS. That figure is a CEILING -- it charges nothing for
the 1.72% of skips where the net disagreed about which side of the window the
position fell on, and those are real pruning decisions going the wrong way.
The screen exists to price exactly that difference.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v3_d16_2880b51afe28.nnue"
    LAZY_NNUE = True
    # LAZY_NNUE_MARGIN stays at cengine's 200 -- FI-105's best skip-to-wrong
    # ratio. Sweeping it belongs after a screen says the mechanism pays at
    # all; P-26 spent a campaign learning that tuning a parameter whose
    # mechanism has not been shown to work is how a plateau gets re-measured.
