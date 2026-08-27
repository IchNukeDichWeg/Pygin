"""NNUE/shims/engine_v61a_hash.py -- v61 candidate A: small transposition table.

A SHIP CANDIDATE, not an A/B control: this is v60 exactly, plus TT_BITS 20 (24 MiB) in place of the default 23 (192 MiB, what UCI Hash advertises).
Baseline is NNUE/shims/engine_nnue_v12.py (v60's net and settings), so a
confirmed gain here is a version, not just a finding.

    python3 match.py NNUE/shims/engine_v61a_hash.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Same instrument as every screen since s1 (seed 59, UHO_4060_v4, offset 0,
cores/2 workers). Budget note: this resolves ~+9 Elo and up; a null means
"not >= 9", not "no effect".

WHY. A 90-position sweep (3 per piece count, 32..3 men, fresh TT,
~1M-node searches) measured a 6 MiB table at +11.2% NPS over 96 MiB, faster in
84/87 -- probes stay cache-resident instead of TLB-missing over a cold table.
Held at 7-27x oversubscription (+12.3%) for only ~2% more nodes at fixed
depth, because always-replace keeps what matters. At ~1.16 Elo per 1% NPS
that is ~+12 Elo.

WHAT GAMES ADD that the sweep could not: real play CARRIES the table between
moves, and cross-move hits were 39-82% of all TT hits. A too-small table
should bleed exactly there. 20 rather than 18 is the hedge -- 24 MiB keeps
most of the locality win with far more room for carry-over.

PER TIME CONTROL. Sized for 10+0.1. At 50+0.5 (10-50M-node searches) the
trade may reverse, so that TC needs its own measurement before this ships
there."""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"   # the net v60 ships
    LAZY_NNUE = True
    TT_BITS = 20          # 24 MiB (default 23 = 192 MiB, the UCI Hash default)
