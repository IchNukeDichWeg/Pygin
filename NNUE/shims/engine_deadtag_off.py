"""NNUE/shims/engine_deadtag_off.py -- FI-115 A/B, tag OFF.

Identical in every respect except TT_DEADTAG. Same net (v12, what v60 ships),
same TT size, same search -- the ONLY variable is which entry a store evicts.

FI-115: an entry whose stored piece count exceeds the root's is provably
unreachable (material is irreversible), so it is taken first; a still-reachable
old entry is depth-protected instead of clobbered on age alone. First
measurement, 24-ply self-play at depth 10 with a 12 MB table: -13.7% nodes for
the same moves. That is a tree-quality change and needs games, not node counts.

TT_BITS 20 deliberately: the tag matters most under replacement pressure, and
a 200 MB table has little. This pairs with the hash-size test.
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_BITS = 20
    TT_DEADTAG = False
