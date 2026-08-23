"""NNUE/shims/engine_nnue_s1.py -- seed 1 on gen_only, for the variance screen.

Identical to every other net in this batch except `train.py --seed 1`:
gen_only, 40-epoch cosine, d2 16, lambda 0.75. v10's corpus -- the one that ACCEPTED at +39.86.

This exists to answer ONE question -- how far apart do nets land when nothing
but the seed changes? v10 and v4 shared corpus, recipe and dimensions (both at
train.py's default seed 0) and still finished ~40 Elo apart, which is wide
enough to explain every single-net verdict on this ladder. Four seeds per
corpus turns "one net said so" into a spread that can be compared against the
gap BETWEEN corpora.

    python3 match.py NNUE/shims/engine_nnue_s1.py NNUE/shims/engine_nnue_v12.py \
        2500 0 --workers 48 --tc 10+0.1 --seed 59

SCREEN INSTRUMENT, 10+0.1 -- deliberately NOT the 50+0.5 the ledger is built
on, and its numbers never pool with it. 5x cheaper per game, ~+/-9.6 Elo over
5,000 games, which resolves a 40-point spread easily and a 5-point one not at
all. A screen this size is a KILL FILTER: it can say "clearly worse", it
cannot say "slightly better".

Trained val 0.066541 at epoch 13 of 40. Val has been an inverted
signal seven times here, so it is recorded, not relied on.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_s1_7b27370522dc.nnue"
    LAZY_NNUE = True     # matches the baseline; identical on both sides
