"""NNUE/shims/engine_ss045.py -- X-13: SOFT_STOP_STABLE_FRAC = 0.45 (HEAD ships 0.40).

A/B arm against cengine.py (HEAD). The ONLY difference is the soft-stop
fraction used once the best move has been stable, which the audit measured
governing 26 of 30 moves while the tuned base fraction governs 4. Untested,
no sign prior, so 0.35 and 0.45 each get a slot. Clock-shaped: TIMED screen
only, a fixed-node run is blind to time policy.

    python3 match.py NNUE/shims/engine_ss045.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    SOFT_STOP_STABLE_FRAC = 0.45
