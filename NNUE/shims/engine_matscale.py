"""NNUE/shims/engine_matscale.py -- E-04 material-scaled NNUE output.

A/B arm against cengine.py (HEAD). The ONLY difference is NNUE_MATSCALE: the
net's value is scaled by (96 + phase) / 120, so 1.0 with full material and 0.8
in a bare endgame. Both arms load the same csearch.so, and the switch is
node-exact when off.

Instrument, per the audit: FIXED-NODE, because the change shapes pruning and
TT evals rather than costing NPS, and --nodes is contention-immune.

    python3 match.py NNUE/shims/engine_matscale.py cengine.py 5000 0 \\
        --nodes 1750000 --workers 0 --seed 62 \\
        --sprt --sprt-min-pairs 1500 --tag n08_matscale

Prior +0-3: at 10,000 games a screen resolves only about +5, so a flat LLR at
the cap is the likely outcome and closes the HCE-leaf variant. It does NOT
transfer if E-07 (net at the qsearch leaves) ever lands.
"""
import cengine


class Engine(cengine.Engine):
    NNUE_MATSCALE = True
