"""engine_nnue_v7.py -- the v7 net against the v4 net, BOTH with lazy eval.

The composition-repair follow-up to v6's rejection (-10.51 +/- 9.5, LLR
-2.944). v6's dataset was 100% random self-play; v4's had 10.4% endgame
starts and 5.9% UHO book starts, slices my generation spec dropped. v7 is
trained on gen10k_mix: the same 52.1M random positions PLUS 7.5M endgame
and 5.0M UHO positions, all at 10,000-node labels (64,592,198 records,
verify_labels 53/53 exact). Same dims as v4/v6 (6144-256-16-32, d2 16),
same 8-epoch cosine recipe, trained uninterrupted -- so vs v4 this varies
{label depth, WDL fix} with composition now restored, and vs v6 it varies
composition alone. Held-out val 0.065110: fourth distribution, fourth
incomparable number; only the A/B judges.

    python3 match.py engine_nnue_v7.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 48 --tc 50+0.5 --seed 59 --sprt

Reads: ACCEPT -> composition was the poison, deeper labels survive, v7 is
the v60 candidate. REJECT/NULL -> composition was not (all of) it, and the
5,000-node control on the corrected pipeline becomes the next experiment.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v7_27116d81d89f.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
