"""NNUE/shims/engine_hash_small.py -- TT sizing A/B, 24 MB side.

Identical to the shipped engine in EVERY respect except TT_BITS. Both sides
run the v12 net (what v60 ships) and the same search, so the only variable is
transposition-table size.

WHY. A 90-position sweep (3 per piece count, 32..3 men, fresh TT, ~1M-node
searches) measured a 6 MB table at +11.2% NPS over 100 MB, faster in 84/87 --
probes stay cache-resident instead of TLB-missing across a big cold table.
Pushed to 7-27x oversubscription the gain HELD (+12.3%) and cost only ~2% in
extra nodes at fixed depth, because always-replace keeps the entries that
matter. At ~1.16 Elo per 1% NPS that is ~+12 Elo, above our ~9 Elo resolution.

WHAT THIS TEST ADDS that the sweep could not: real games CARRY the table
between moves. Cross-move reuse was measured at 39-82% of all TT hits, and a
too-small table is exactly where that should bleed. The sweep used a fresh
table per search and is blind to it. This A/B is the honest instrument.

Sizing is PER TIME CONTROL: at 50+0.5 (10-50M-node searches) the trade may
reverse. This pair is for 10+0.1 only.

    python3 match.py NNUE/shims/engine_hash_small.py NNUE/shims/engine_hash_default.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_BITS = 20          # 24 MB at 24 B/entry
