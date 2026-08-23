"""engine_nnue_v12.py -- the v12 net against Old Engine/59, BOTH lazy.

The first net trained on REAL GAME labels rather than generated
self-play. ab_logs is 24,825,823 positions harvested from match.py A/B
battle logs by NNUE/logs_to_pygdata.py: 24,587,037 from the pre-v49
campaigns plus 238,786 from two 2026-08-09 fixed-node screens. Its labels
come from actual search at depth 12-16, against the 5,000-node (v9/v10)
and 10,000-node (v11) labels everything else on this ladder used.

    python3 match.py NNUE/shims/engine_nnue_v12.py "Old Engine/59/engine59.py" 5000 0 \
        --workers 0 --tc 50+0.5 --seed 59 --sprt

TRAINED AT LAMBDA 1.0 -- search scores alone, game result unused -- and
that is not a tuning choice, it is a correctness one. These logs predate
the phantom-repetition fix (fc82cb7), which ended a game the moment a
repetition became AVAILABLE rather than reached. Replay says 1,479/1,479
and 2,215/2,215 of the repetition draws in two sampled logs were phantom,
zero genuine, and it shows in the distribution: W/D/L 39.0/41.9/19.1
against a clean corpus's 41.7/18.6/39.7 -- both inflated and skewed. The
cp column is sound, the result column is not, so lambda 1.0 drops it.

Its val of 0.122853 is NOT comparable to any other net here: lambda 1.0
is a different target (pure cp/400, range +/-5) and a wider target gives
a larger MSE for free. On common holdouts at pure-cp targets v12 wins its
own distribution decisively (R^2 0.8410 on ab_logs, where the next best
is v11's 0.7919) and loses away from it (0.8553 on gen10k against v11's
0.9254). Every net in this batch won its own distribution; v12 just has
the most distinct one.

The version gate: every contributing side predates FI-29 (CYCLE_DETECT,
v49), whose cycle bound draw-flattens scores through path history that
position-only features cannot represent, undetectably after the fact.

Its own file rather than an edit of an older shim on purpose: the SPRT
state file is auto-named from the engine names, so a shared name would let
two different nets pool into one LLR without complaint.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True     # matches Old Engine/59, the other side -- the
                         # toggle is identical on both sides by design
