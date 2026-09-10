"""NNUE/shims/engine_q50.py -- FI-38 QUIETS half, K2=50, on top of captures K=0.

A/B arm against cengine.py: the ONLY difference is SEE_QUIET_K2=50 (a quiet move at
depth <= 4 is pruned when see_quiet < -50 * depth^2). Used if the K=50 captures confirm REJECTS; K2=50 grew the kiwipete tree 36% at d12.

    python3 match.py NNUE/shims/engine_q50.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500

A SCREEN, not a measurement: ACCEPT earns a 50+0.5 slot; a flat LLR at the
10,000-game cap closes the quiets half.
"""
import cengine


class Engine(cengine.Engine):
    SEE_SCALED_K = 0
    SEE_QUIET_K2 = 50
