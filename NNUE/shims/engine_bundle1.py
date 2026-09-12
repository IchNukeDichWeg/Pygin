"""NNUE/shims/engine_bundle1.py -- the NULL bundle, armed as ONE arm.

The three independent settings from docs/null_bundle.md, each of which screened
POSITIVE against v63 but never approached a bound on its own:

    SEE_SCALED_K            50 -> 75    +2.74 +/- 4.7   (sprt_see75)
    SOFT_STOP_STABLE_FRAC   0.40 -> 0.45 +2.54 +/- 4.7  (sprt_ss045)
    SOFT_STOP_STABLE_ITERS  2 -> 3      +1.01 +/- 4.7   (sprt_ssi3)

One prunes captures, two shape the clock, so they do not confound each other.
Their point estimates sum to about +6.3, which a 10,000-game screen can resolve
if the effect is additive -- no single one can be resolved at any budget worth
buying. A verdict here prices the BUNDLE, never its members (FI-24 precedent):
if it accepts it ships as one change and takes one ledger line.

    python3 match.py NNUE/shims/engine_bundle1.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500
"""
import cengine


class Engine(cengine.Engine):
    SEE_SCALED_K = 75
    SOFT_STOP_STABLE_FRAC = 0.45
    SOFT_STOP_STABLE_ITERS = 3
