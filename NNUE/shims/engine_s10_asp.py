"""NNUE/shims/engine_s10_asp.py -- P13/S10: aspiration repair (FI-21).

v61 exactly, with _root_aspiration overridden to carry the three FI-21 riders
(agent-verified 2026-08-29 that stock does none of them):
  1. score-scaled initial delta: 14 + prev_score^2/16384 instead of flat 30cp
  2. fail-low pulls beta to the midpoint (alpha+beta)/2 (SF order: beta from
     the OLD bounds, then alpha drops) -- kills the fail-low/fail-high
     oscillation that pays the full ladder to the 1920 fallback
  3. growth 1.5x instead of 2x
Driver-only; C untouched; the CB2/FB-23 provisional-move machinery is copied
verbatim from cengine.py @ cd046c6 -- if cengine's _root_aspiration changes,
RE-DIFF THIS SHIM before trusting it.

    python3 match.py NNUE/shims/engine_s10_asp.py NNUE/shims/engine_v61b_deadtag.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500
"""

import cengine
from cengine import CS_INF, CS_MATE_THRESH


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"
    LAZY_NNUE = True
    TT_DEADTAG = True     # v61 baseline

    def _root_aspiration(self, bargs, depth, prev_key, prev_score, hmc):
        """FI-21 window: scaled delta, fail-low beta midpoint, 1.5x growth."""
        if (depth < self.ASPIRATION_MIN_DEPTH or prev_score is None
                or abs(prev_score) >= CS_MATE_THRESH):
            return self._root(bargs, depth, -CS_INF, CS_INF, prev_key, hmc)
        delta = 14 + (prev_score * prev_score) // 16384      # S10 change 1
        alpha = max(-CS_INF, prev_score - delta)
        beta = min(CS_INF, prev_score + delta)
        provisional = 0                      # CB-02/FB-23 verbatim
        while True:
            res = self._root(bargs, depth, alpha, beta, prev_key, hmc)
            if res[4]:
                if self.CB2 and res[3] == 0 and provisional:
                    return (provisional, res[1], res[2], 1, True, res[5])
                return res
            score = res[1]
            if score <= alpha:               # fail low
                beta = (alpha + beta) // 2                   # S10 change 2
                alpha = max(-CS_INF, score - delta)
            elif score >= beta:              # fail high
                if self.CB2 and res[0]:
                    provisional = res[0]
                    prev_key = res[0]
                beta = min(CS_INF, score + delta)
            else:
                return res
            delta += delta // 2                              # S10 change 3
            if delta >= 2 * self.ASPIRATION_DELTA * 32:      # same 1920 cap
                return self._root(bargs, depth, -CS_INF, CS_INF, prev_key, hmc)
