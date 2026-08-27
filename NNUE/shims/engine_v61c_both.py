"""NNUE/shims/engine_v61c_both.py -- v61 candidate C: small table AND the dead-entry tag.

A SHIP CANDIDATE, not an A/B control: this is v60 exactly, plus TT_BITS 20 and TT_DEADTAG on.
Baseline is NNUE/shims/engine_nnue_v12.py (v60's net and settings), so a
confirmed gain here is a version, not just a finding.

    python3 match.py NNUE/shims/engine_v61c_both.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Same instrument as every screen since s1 (seed 59, UHO_4060_v4, offset 0,
cores/2 workers). Budget note: this resolves ~+9 Elo and up; a null means
"not >= 9", not "no effect".

WHY BOTH. They compound rather than overlap. A smaller table creates
more replacement pressure -- which is precisely the regime where choosing the
right victim pays most, and where the dead-entry tag has the most garbage to
spend. Candidate A buys NPS; candidate B spends the resulting pressure well.

Test A and B separately FIRST. If either is null this pairing tells you
nothing about which half carried it, and a combined-only result is the sort
of two-changes-at-once measurement this project has been burned by."""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"   # the net v60 ships
    LAZY_NNUE = True
    TT_BITS = 20          # 24 MB
    TT_DEADTAG = True     # FI-115
