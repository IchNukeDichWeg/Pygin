"""NNUE/shims/engine_v15net.py -- the v15 net against the shipped tree.

The net is the only difference, so the screen prices the net.

    python3 match.py NNUE/shims/engine_v15net.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 64 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v15_4f85b4f247cd.nnue"
