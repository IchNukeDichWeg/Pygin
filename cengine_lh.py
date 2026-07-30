#!/usr/bin/env python3
"""FI-105 candidate: history-fed LMR on top of cut-node reduction.

    python3 match.py cengine_lh.py cengine.py 5000 --offset 70000 \
            --workers 0 --nodes 1750000 --sprt --tag fi105

Candidate in slot 1 -- match.py scores the pentanomial from Engine 1.

TTPV_LMR (FI-104) is deliberately NOT armed. Measured against the shipped
baseline (bench 1,145,629): CUTNODE_LMR -10.5%, LMR_HIST -2.0%, both together
-10.3%, but TTPV_LMR +31.8% on its own and the trio +22.6%. A change that
costs a third of the tree at fixed depth reaches shallower on a fixed-node
budget -- the profile that sank FI-49 (+28% nodes, -3.65 Elo). It is also the
toggle that turned FI-103's +2.43 screen into the pair's -0.69.

So this drops the "safety valve" half of the R10 thesis and keeps the half
that saves nodes. FI-107 just showed a constant-factor node saving at flat
slope is worth +4.11 Elo; this has the same shape.
"""
from cengine import Engine as _Base


class Engine(_Base):
    CUTNODE_LMR = True      # FI-103: +1 reduction at non-PV cut nodes
    LMR_HIST = 2048         # FI-105: history nudge, live divisor (8192 = dead gate)
