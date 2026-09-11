"""NNUE/shims/engine_pc300.py -- PROBCUT_MARGIN = 300 (HEAD ships 200).

A/B arm against cengine.py (HEAD). One variable. The upward follow-up that
engine_x1_probcut130.py named: 130 read NULL (LLR +0.37), so the other side of
the margin gets its slot. Chosen under the HCE, never swept under the net.

    python3 match.py NNUE/shims/engine_pc300.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    PROBCUT_MARGIN = 300
