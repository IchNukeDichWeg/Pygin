"""NNUE/shims/engine_v14net.py -- the v14 net against the shipped tree.

The net is the only difference, so the screen prices the net.

    python3 match.py NNUE/shims/engine_v14net.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v14_959f679e272e.nnue"
