"""NNUE/shims/engine_tempo15.py -- E-05 NNUE tempo +15 cp on top of v65.

The shipped tree with NNUE_TEMPO = 15, so the screen prices the term alone.
v15's export already carries a +29 cp stm offset; this asks whether more
helps.

    python3 match.py NNUE/shims/engine_tempo15.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 65 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    NNUE_TEMPO = 15
