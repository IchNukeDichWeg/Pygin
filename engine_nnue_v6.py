"""engine_nnue_v6.py -- the v6 net against the shipped v58 (v4 net).

The FIRST net A/B that varies only the net. LAZY_NNUE is deliberately NOT
set here: it inherits cengine's False, which matches Old Engine/58, so the
one difference between the two sides is the weights. The v4 and v5 shims
both set LAZY_NNUE = True against a False baseline and measured net + FI-106
together (see their docstrings); this file exists to not do that.

v6 = the first net trained after the phantom-repetition fix (fc82cb7 /
550a9e0), on gen10k: 800,000 self-play games relabelled at 10,000 nodes
(double v4's 5,000) with correct WDL targets. Same dimensions as v4
(6144-256-16-32, d2 16), same 8-epoch cosine recipe, so NPS is unchanged
and a timed run is fair. Held-out val 0.064895 -- NOT comparable to v4's
0.066663 or v5's 0.063676, because deeper labels are a different target
distribution; only the A/B decides.

    python3 match.py engine_nnue_v6.py "Old Engine/58/engine58.py" 5000 0 \
        --workers 48 --tc 50+0.5 --seed 58 --sprt

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v6_b3b80ac18e2f.nnue"
