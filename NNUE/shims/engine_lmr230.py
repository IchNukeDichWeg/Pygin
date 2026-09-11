"""NNUE/shims/engine_lmr230.py -- LMR_DIV = 230 (HEAD ships 200).

A/B arm against cengine.py (HEAD). One variable. Less LMR. The P-26 sweep that found
LMR flat near 200 (2026-07-21) ran on the Texel HCE; the net changed move
ordering quality and eval noise since, so the plateau finding does not carry.

    python3 match.py NNUE/shims/engine_lmr230.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    LMR_DIV = 230
