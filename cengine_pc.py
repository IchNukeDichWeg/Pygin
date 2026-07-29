#!/usr/bin/env python3
"""FI-107 A/B candidate: ProbCut ARMED at margin 200. Baseline is cengine.py.

    python3 match.py cengine_pc.py cengine.py 5000 --offset 60000 \
            --workers 0 --nodes 1750000 --sprt --tag fi107b

CANDIDATE IN SLOT 1. match.py scores its pentanomial from ENGINE 1, so the
SPRT asks "is Engine 1 >= elo1 better than Engine 2" -- with the candidate in
slot 2 the printed LLR answers the mirror question, and because the [0, 4]
bounds are not symmetric about the result the two magnitudes differ ~3x. That
mistake made a pooled 8,000 pairs read "ACCEPT H0 (change rejected)" when the
candidate was in fact at +1.128 and undecided.

WHY A SUBCLASS AND NOT A COPY. `sed`-ing a 2,500-line duplicate of cengine.py
is one command, and then it is a second copy that silently goes stale the next
time cengine.py changes -- and a B side that differs from A by anything other
than the toggle under test is not an A/B at all. Two lines that inherit cannot
drift: `diff` this file against nothing, there is nothing to diff.

Safe in one process because match.py runs each engine in its OWN subprocess:
the C globals live in csearch.so and are pushed by Engine.__init__, so two
differently-configured engines in a single process would clobber each other.
They never share one. Importing cengine does not push anything -- only
constructing does.
"""
from cengine import Engine as _Base


class Engine(_Base):
    # The one and only difference from the baseline. Everything else --
    # eval params, every other toggle, the net -- is inherited, by
    # construction rather than by remembering to re-copy.
    PROBCUT_MARGIN = 200
