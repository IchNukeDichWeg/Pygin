"""NNUE/shims/engine_nulldiv7.py -- null-move divisor NULL_DIV = 7 (HEAD ships 6).

A/B arm against cengine.py (v63). The depth-scaled part of the null-move
reduction: csearch.c computes R = NULL_BASE + depth / NULL_DIV, so 7 is LESS
reduction as depth grows (Mac kiwipete d12: 579285 nodes against HEAD's 675,783). The P-26 sweep that found the null knobs flat ran 2026-07-21 on the
Texel HCE; prune_eval has been NNUE output since v58, a different scale and a
different noise distribution, so the plateau finding does not carry.

    python3 match.py NNUE/shims/engine_nulldiv7.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    NULL_DIV = 7
