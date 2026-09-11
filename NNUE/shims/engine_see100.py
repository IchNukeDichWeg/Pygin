"""NNUE/shims/engine_see100.py -- FI-38 captures half at K=100, against the K=50 default.

A/B arm against cengine.py (HEAD, SEE_SCALED_K = 50 since e9bc9e2). The only
difference is K: a SEE-losing capture at depth <= 6 is pruned below -100 per ply
instead of -50. A sweep around the confirmed setting, screened at 10+0.1.
"""
import cengine


class Engine(cengine.Engine):
    SEE_SCALED_K = 100
