"""NNUE/shims/engine_s11_falleval.py -- S11: falling-eval time extension.

v61 exactly, plus: when this iteration's root score fell more than 25cp below
the previous iteration's, the soft stop treats the iteration as unstable even
if the best MOVE is stable -- a position going wrong is when banked time pays,
and stock stops such lines at the 0.40 stable fraction (agent-verified
2026-08-29: prev_score is tracked but never consulted by the stop).

No method copying: a wrapper on _root_aspiration records per-iteration root
scores (STM-POV, same side across one search, so directly comparable), and a
property on SOFT_STOP_STABLE_FRAC answers the ID loop's stable-branch read
with the UNSTABLE fraction while the score is falling. The stab_changed
branch already extends on move changes; this covers the stable-move
collapsing-score case, which is exactly the S11 target.

TIMED instrument mandatory by nature (the change IS time policy; fixed-node
and fixed-depth runs are blind to it by construction).

    python3 match.py NNUE/shims/engine_s11_falleval.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Caveat: cuci.py writes engine.SOFT_STOP_STABLE_FRAC on two setoption paths;
under this shim that write would raise (property without setter) -- match.py
drives engines directly and never sends those options, so the A/B is clean,
but do not mount this shim behind a UCI host that tunes SoftStable.
"""

import cengine

_FALLING_DROP_CP = 25    # SF's fallingEval threshold neighborhood
_STABLE_BASE = cengine.Engine.SOFT_STOP_STABLE_FRAC   # 0.40 at cd046c6


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline

    _s11_prev = None
    _s11_falling = False

    def _search_impl(self, board, time_limit, max_depth):
        self._s11_prev = None
        self._s11_falling = False
        return super()._search_impl(board, time_limit, max_depth)

    def _root_aspiration(self, bargs, depth, prev_key, prev_score, hmc):
        res = super()._root_aspiration(bargs, depth, prev_key, prev_score, hmc)
        if not res[4]:                       # completed iteration only
            s = res[1]
            self._s11_falling = (self._s11_prev is not None
                                 and s < self._s11_prev - _FALLING_DROP_CP)
            self._s11_prev = s
        return res

    @property
    def SOFT_STOP_STABLE_FRAC(self):
        # consulted only on the stable-move branch of the soft stop; a
        # falling eval answers with the unstable fraction (0.80) instead
        return (self.SOFT_STOP_UNSTABLE_FRAC if self._s11_falling
                else _STABLE_BASE)
