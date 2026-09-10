"""NNUE/shims/engine_see50.py -- FI-38 depth-scaled SEE pruning, captures half, K=50.

A/B arm against cengine.py (HEAD, the v63 candidate). The ONLY difference is
SEE_SCALED_K: both arms load the same csearch.so, and K=0 is node-exact to the
pre-FI-38 core, so the baseline is HEAD itself rather than a snapshot. FI-21
stays ON in both arms -- OPEN 1 accepted it (2026-09-10), so a result here
transfers straight into v63. The audit's arm turned FI-21 off only because it
was unconfirmed when the audit was written.

    python3 match.py NNUE/shims/engine_see50.py cengine.py 5000 0 \
        --workers $(($(nproc)/2)) --tc 10+0.1 --seed 62 \
        --sprt --sprt-min-pairs 1500

A SCREEN, not a measurement: ACCEPT here earns a 50+0.5 slot for a number;
a flat LLR at the 10,000-game cap closes the FI-18/23/64/38 family for good.
K=50 prunes a depth-6 SEE-losing capture below -300 cp.
"""
import cengine


class Engine(cengine.Engine):
    SEE_SCALED_K = 50
