"""engine_nnue_v10.py -- the v10 net against Old Engine/59, BOTH lazy.

The reproducibility control. v4 is the only net that ever paid (+19.11 +/-
7.8), and every attribution rung since has assumed v4's result was a
property of its DATA. v10 tests the cheaper explanation first: it retrains
v4's own corpus (gen_only, 57.8M positions, 5,000-node labels from the
pre-fix July pipeline) with v4's own 40-epoch cosine recipe and the same
dims, differing only in seed. If v10 lands near v4, training here is
reproducible and the pipeline really is the variable. If it lands short,
v4 was a lucky draw and every single-net verdict on this ladder is
shakier than it looks.

    python3 match.py engine_nnue_v10.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 0 --tc 50+0.5 --seed 59 --sprt

Held-out val 0.066733 (epoch 21 of 40) against v4's 0.066663 -- a delta of
0.00007, so by val the recipe reproduces v4 almost exactly. That is a fact
about val, not a prediction: val has been an INVERTED signal on this
ladder four times running (v6 and v8 hold the two best val numbers and
both were rejected; v4 has a mediocre one and is the only winner).

Two measurements recorded so nobody re-derives them. Its export printed a
float-vs-int quantization MAE of 62.64 cp against the documented ~17.4 --
that is gen_only's sharper positions inflating an absolute-cp metric, not
a broken export: on a common holdout the real gap to v9 is 1.13x, not
3.7x, and R^2 0.9255 is a healthy net. And on both holdouts scored,
v10 placed LAST of the four nets trained on this box (R^2 0.9101 on
gen10k, 0.7552 on ab_logs, pure-cp targets) -- which sits oddly beside
its near-exact val reproduction of v4, and is one more reason to trust
only the A/B.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v10_c15e2c5b4a66.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
