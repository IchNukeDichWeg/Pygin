"""NNUE/shims/engine_ssu100.py -- X-13: SOFT_STOP_UNSTABLE_FRAC = 1.00 (HEAD ships 0.80).

A/B arm against cengine.py (v63). An iteration where the best move just changed
never soft-stops at all: the hard time limit is the only thing that ends it.
0.90 screened +6.95 +/- 4.7 (near-bound accept, f9272d5) and 0.70 was flat, so
this is the far end of the direction that pays. Clock-shaped: TIMED only.

    python3 match.py NNUE/shims/engine_ssu100.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    SOFT_STOP_UNSTABLE_FRAC = 1.00
