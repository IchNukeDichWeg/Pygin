"""engine_nnue_v4.py -- engine_nnue_lazy.py with the v4 net instead of v3.

Identical dimensions to v3 (6144-256-16-8, d2 16, D3 32), so the NPS cost is
unchanged and the only difference between this and engine_nnue_lazy.py is the
weights. v4 is the same dataset and the same architecture trained with a
cosine LR schedule: held-out val 0.066663 against v3's 0.074417.

THIS IS NOT THE CONFIG THAT SHIPPED AS v58 (found 2026-08-09). This file sets
LAZY_NNUE = True and it is the engine the +19.11 +/- 7.8 was measured on
(sprt_engine_nnue_v4_vs_engine57_tc50+0.2.json, engine1 = engine_nnue_v4).
The release has LAZY_NNUE = False. So the headline number describes NNUE WITH
lazy evaluation, while the shipped engine runs NNUE WITHOUT it, and the gap
between them is unmeasured -- FI-106 has never been isolated. See cengine.py's
v58 entry.

    python3 match.py engine_nnue_v4.py "Old Engine/57/engine57.py" 1000 \
        --workers 0 --tc 50+0.2 --sprt

Its own file rather than a one-line edit of engine_nnue_lazy.py on purpose:
the state file is auto-named from the engine names, so sharing a name would
let a v3 tranche and a v4 tranche pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v4_6f910e35bb1e.nnue"
    LAZY_NNUE = True
