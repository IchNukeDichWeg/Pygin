"""NNUE/shims/engine_lazy150.py -- LAZY_NNUE_MARGIN = 150 (HEAD ships 200).

A/B arm against cengine.py (HEAD). One variable. The downward side of the
lazy-NNUE trust margin: engine_x2_lazy300.py read NULL at 300. Lower skips more
net evals (NPS) at the cost of more wrong-side bounds. NPS-moving, so TIMED.

    python3 match.py NNUE/shims/engine_lazy150.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    LAZY_NNUE_MARGIN = 150
