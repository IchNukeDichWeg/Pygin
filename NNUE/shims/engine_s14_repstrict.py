"""NNUE/shims/engine_s14_repstrict.py -- S14: strict repetition. LOWEST PRIORITY.

v61 exactly, plus REP_STRICT on. Built and verified-off in C (is_repetition
seen>=2 for pre-root positions + the repeats_further_back rider).

PREMISE-BROKEN, run last if at all (agent-verified 2026-08-29): the audit's
reset argument claimed the rider was not in the killed screen's build; it WAS
(same commit 31a36d4, 2026-07-27). FI-89 screen-killed at -20.17 +/-15.3 --
the worst screen on the books -- measuring the exact rule this shim arms.
The only remaining argument is the NNUE-regime one, standing against an
explicit 'do-not-retry without a different rule'. If run: screen only, kill
fast if the -20 reproduces.

    python3 match.py NNUE/shims/engine_s14_repstrict.py NNUE/shims/engine_v61b_deadtag.py \
        1000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59
"""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline
    REP_STRICT = True     # S14: the one variable
