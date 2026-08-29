"""NNUE/shims/engine_v13s1.py -- v13 seed 1: the 105M 25,000-node corpus.

v61 exactly, with the net swapped. The ONLY variable is the net, so a gain
here is the corpus, not the search.

    python3 match.py NNUE/shims/engine_v13s1.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

RUN ALL THREE SEEDS OR NONE. The trainer is bit-reproducible (gw6 == gw6r
byte-identical), so the ~31 Elo spread between nets IS the seed. One net
cannot clear that band and its result would mean nothing on its own.

WHAT IS NEW, and it is only one thing. Volume is measured dead (gen200m,
200M records, three seeds, all lost) and the epoch schedule is measured dead
at this size too (2026-08-29 sweep: val worsens 0.0641 -> 0.0652 -> 0.0671
across 8/20/40, reproducing the closed ~82M sweep on a corpus 1.3x larger).
What has never been tested for STRENGTH is label DEPTH: 25,000 nodes per
move, the calibrated knee, against the 5,000 that trained every earlier
corpus -- plus Syzygy endgame labels, where 31% of <=5-man positions in
gen_only carried a label the tablebase contradicts.

PRIOR IS POOR AND SHOULD BE SAID OUT LOUD: thirteen straight nets have
failed to beat v12. This is the last untested axis of the corpus lane, not
a favourite.

Trained on this Mac, 8 epochs, cosine, --chunk 2000000, seed 1.
val 0.064237, quant MAE 21.26 cp. Val ranks NOTHING here -- it is
0-for-11 as an Elo predictor and the g-nets sat in a 0.0001 val range while
being 31 Elo apart."""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v13s1_5642e72cb34a.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 default -- held constant, the net is the variable
