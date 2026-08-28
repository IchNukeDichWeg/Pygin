"""NNUE/shims/engine_grow_on.py -- FI-116, the GROWING transposition table.

Identical to the shipped engine in every respect except TT_GROW. Same v12 net,
same search, same 192 MiB allocation; the only variable is whether the table is
indexed with all 23 bits from move one or with a window that starts at 20 bits
(24 MiB) and doubles each time it passes 75% full.

WHY THIS IS NOT THE hash_small TEST. A STATIC 24 MiB table measured NULL over
the full 10,000 games @10+0.1 (2026-08-28 campaign): the +11.2-12.3% NPS a
small table wins on the piece-count sweep is handed straight back in evictions,
because a small table is still small once the position count exceeds it.
Growing is the asymmetry -- while the table is underfilled a narrow window
costs NOTHING, since every entry still fits. It only widens once they stop
fitting, so the cache/TLB win is taken exactly and only where it is free.

THE RAMP IS THE WHOLE STORY, and it is TC-dependent. Active window bits by ply,
measured over a real game (cs_tt_active_bits is the oracle):

    0.15s/move (~10+0.1)   20 -> 21 @ply23 -> 22 @ply31   never reaches 192 MiB
    1.4s/move  (~50+0.2)   20 -> 21 @ply17 -> 22 @ply19 -> 23 @ply21

At 10+0.1 the engine runs at 24-96 MiB for the ENTIRE game against a baseline
pinned at 192 MiB. At 50+0.2 the ramp is over by ply 21 and most of the game
is played at parity, so the gain should shrink as TC grows -- the reverse of
most search items. This pair is for 10+0.1; 50+0.5 needs its own measurement
and should be expected to measure smaller, not larger.

INSTRUMENT CAVEAT, read before spending a slot. At the calibrated ~1.16 Elo per
1% NPS, even a good outcome here is single-digit Elo and a 10,000-game A/B
resolves ~+9 and up. The honest primary instrument is an NPS bench over a
PLAYED SEQUENCE, not cbench: search_bench wipes the table per position, so
growth never leaves its starting window and the bench is blind to this change
by construction. Treat the A/B below as the confirmation, not the screen.

    python3 match.py NNUE/shims/engine_grow_on.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"   # the net v60 ships
    LAZY_NNUE = True
    TT_GROW = True        # FI-116; TT_BITS stays 23, the window starts at 20
