"""engine_nnue_v8.py -- the 5,000-node CONTROL net vs the v4 net, both lazy.

The attribution experiment after v6 and v7 both rejected. v8's dataset is
the corrected pipeline at v4's ORIGINAL label depth: 800k random games +
7.5M endgame + 5M UHO positions, all labelled at 5,000 nodes (gen5k_mix,
seeds 4/5/6), so vs v7 it varies label depth alone and vs v4 it varies
{WDL fix, labeler era, fresh games} with depth and composition held. Same
dims (6144-256-16-32, d2 16), same 8-epoch cosine recipe, trained
uninterrupted with --cache-chunks (identical math, cached prepares).

    python3 match.py engine_nnue_v8.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 64 --tc 50+0.5 --seed 59 --sprt

Reads: ACCEPT or NULL -> the 10k label depth was the poison (v6/v7's only
remaining shared variable vs this net); the deep-label lane closes.
REJECT -> the corrected pipeline's teacher itself (labeler era or result
mix) makes worse nets, and depth was never the story.

Held-out val 0.063325 is the lowest of any net yet, on yet another
distribution -- if this net rejects, that is strike four for val as a
cross-distribution predictor.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v8_5e455e8e2a14.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
