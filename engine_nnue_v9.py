"""engine_nnue_v9.py -- the RECIPE net vs the v4 net, both lazy.

The fourth rung of the attribution ladder, after v6, v7 and v8 all
rejected. v9's dataset is byte-identical to v8's -- gen5k_mix, 800k random
games + 7.5M endgame + 5M UHO positions at 5,000 nodes, seeds 4/5/6 -- and
so are its dimensions (6144-256-16-32, d2 16). The ONLY difference from v8
is the schedule: a 40-epoch cosine instead of an 8-epoch one. So vs v8 this
varies the training recipe alone, and vs v4 it varies {WDL fix, labeler
era, fresh games} with depth, composition AND recipe now all held.

    python3 match.py engine_nnue_v9.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 64 --tc 50+0.5 --seed 59 --sprt

The hypothesis under test: v4 was never the product of the 8-epoch recipe
that trained v6/v7/v8. It was the epoch-15 checkpoint of a 40-epoch cosine,
and the whole ladder so far has been comparing nets trained a different way
to a net whose schedule nobody replicated.

Reads: ACCEPT -> the recipe was the lever all along and the corrected
pipeline is fine. REJECT/NULL -> the recipe is not it either, and after
composition, label depth and schedule have each been eliminated, what is
left is the teacher itself -- the labeler era or the result mix.

VAL WARNING, and it cuts against the hypothesis before a single game is
played. On this identical dataset the 8-epoch run (v8) reached val
0.063325; the 40-epoch run bottomed at 0.066154 (epoch 17 of 40) and then
drifted UP to 0.066900 by epoch 39 while train fell to 0.047075. The longer
schedule trains a measurably worse net by held-out loss. That is a real
comparison -- same data, same deterministic split, same dims -- not a
cross-distribution one. But v8 held the lowest val of any net yet and
rejected, so on this ladder val has been an inverted signal four times
running. It is recorded here as a fact, not as a prediction; only the A/B
decides.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v9_33c885b405d9.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
