"""NNUE/shims/engine_lmr170.py -- LMR_DIV = 170 (HEAD ships 200).

A/B arm against cengine.py (HEAD). One variable. More LMR. Same reasoning as
engine_lmr230.py: 170 read NULL (+0.69) in the HCE era, never under the net.

    python3 match.py NNUE/shims/engine_lmr170.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    LMR_DIV = 170
