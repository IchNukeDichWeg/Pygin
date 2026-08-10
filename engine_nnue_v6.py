"""engine_nnue_v6.py -- the v6 net against the v4 net, BOTH with lazy eval.

A single-variable net A/B on the post-FI-106 baseline. Lazy NNUE was
isolated and ACCEPTED on 2026-08-09 (engine_nnue_v4 vs Old Engine/58, LLR
+2.950 at 2,264 pairs), so the engine's future config runs LAZY_NNUE = True
-- measuring v6 with lazy OFF would price a config that will never ship.
Both sides of this A/B therefore run lazy, and the only difference is the
weights. The baseline is engine_nnue_v4.py, which is byte-equivalent to
shipped cengine plus the lazy flip, i.e. the v59 config:

    python3 match.py engine_nnue_v6.py engine_nnue_v4.py 5000 0 \
        --workers 48 --tc 50+0.5 --seed 59 --sprt

v6 = the first net trained after the phantom-repetition fix (fc82cb7 /
550a9e0), on gen10k: 800,000 self-play games relabelled at 10,000 nodes
(double v4's 5,000) with correct WDL targets. Same dimensions as v4
(6144-256-16-32, d2 16), same 8-epoch cosine recipe, so NPS is unchanged
and a timed run is fair. Held-out val 0.064895 -- NOT comparable to v4's
0.066663 or v5's 0.063676, because deeper labels are a different target
distribution; only the A/B decides.

    python3 match.py engine_nnue_v6.py "Old Engine/58/engine58.py" 5000 0 \
        --workers 48 --tc 50+0.5 --seed 59 --sprt

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v6_b3b80ac18e2f.nnue"
    LAZY_NNUE = True     # matches engine_nnue_v4.py, the other side -- the
                         # toggle is identical on both sides by design
