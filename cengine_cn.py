#!/usr/bin/env python3
"""FI-103 A/B candidate: cut-node aware LMR, ALONE, measured on a CLOCK.

    python3 match.py cengine_cn.py cengine.py 2500 --offset 0 \
            --workers 0 --sprt --tag fi103_timed

Candidate in slot 1 -- match.py scores its pentanomial from Engine 1.

WHY REOPEN IT. FI-103 screened +2.43 +/- 15.2 on `--nodes` and was closed as
null. Two things since say that was the wrong call:

  * a 2k screen cannot resolve an effect near +3 -- it is triage, not a
    verdict. That is exactly how ProbCut was wrongly closed at +2.90.
  * `--nodes` UNDER-CREDITS node-saving changes. ProbCut read +4.11 on nodes
    and +11.44 on the clock (v56's timed cross-check, 5,940 games). The
    fixed-node instrument charges a change for its NPS cost up front while a
    clock lets the node saving become depth.

FI-103 cuts 10.5% of nodes (1,145,629 -> 1,025,572) -- the same class, on the
same under-reading instrument. TTPV_LMR stays off: it costs +31.8% nodes on
its own and turned FI-103's +2.43 screen into the pair's -0.69.
"""
from cengine import Engine as _Base


class Engine(_Base):
    CUTNODE_LMR = True      # FI-103: +1 reduction at non-PV cut nodes
