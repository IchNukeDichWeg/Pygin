"""NNUE/shims/engine_ssu090.py -- X-13: SOFT_STOP_UNSTABLE_FRAC = 0.90 (HEAD ships 0.80).

A/B arm against cengine.py (HEAD). The ONLY difference is the soft-stop
fraction used on an iteration where the best move just changed.
Untested, no sign prior, so both sides of HEAD get a slot. Clock-shaped:
TIMED screen only, a fixed-node run is blind to time policy.

    python3 match.py NNUE/shims/engine_ssu090.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    SOFT_STOP_UNSTABLE_FRAC = 0.90
