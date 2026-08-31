"""NNUE/shims/engine_b05_deadtag_all.py -- the COMPLETED FI-115 rule.

The current build, for A/B against the frozen v61 snapshot. Both halves of the
FI-115 repair are C changes, so no class attribute can isolate them -- the
baseline has to be a different BINARY, which is what Old Engine/61 is.

    python3 match.py NNUE/shims/engine_b05_deadtag_all.py "Old Engine/61/engine61.py" \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

WHAT IS BEING MEASURED. Two commits ship as one feature completion, and they
cannot be split into separate A/Bs because the second depends on the first:
  * the piece-count tag was stamped from a DESCENDANT's board (75.9% of
    end-of-node stores measured wrong, one-directional), so the rule was
    reading garbage;
  * the rule itself ran only at the qsearch store -- the negamax main store
    and the null-move store still clobbered reachable old entries on age
    alone (30,690 such clobbers counted in one instrumented search).
Fixing the tags without extending the rule leaves the two big store sites
unprotected; extending the rule while the tags are wrong protects the wrong
entries. Measured together, 28-ply d10 self-play at 12 MiB: 3,195,225 ->
2,094,812 nodes, -34.4% for the same game.

WHY THE SHIPPED +15.89 DOES NOT PRICE THIS. That A/B measured the half-build
(qsearch-only rule, mis-stamped tags) against no rule at all. It stands as a
verdict on what shipped and says nothing about the completed rule, which is
why this needs its own slot rather than being assumed positive.

HASH MATTERS HERE, more than anywhere else on the board. The rule only
engages when the table is under replacement pressure AND the game has passed
a capture with the table retained; at 192 MiB in a 10+0.1 game the effect is
much smaller than the 12 MiB audit figure. Run it at the shipped default --
the question is whether it pays in the shipped configuration, not whether it
can be made to look good at a small hash.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 default; the C-side rule is what changed
