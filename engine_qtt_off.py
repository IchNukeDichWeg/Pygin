"""
engine_qtt_off.py -- live cengine with P-44 (qsearch TT) switched OFF.

A/B baseline for isolating P-44's solo Elo on top of the P-22 NPS gain:
    python3 match.py cengine.py engine_qtt_off.py 5000 --workers auto
measures exactly "P-44 on vs off" (both sides carry P-22), because the
first bundle A/B vs Old Engine/34 (2026-07-10, ~+72 Elo) conflated
P-22's timed-play speed gain with P-44's unknown contribution.

Safe only because match.py hosts each engine in its OWN process: g_qs_tt
is a csearch.so process-global, so flipping it here inside one shared
process would flip it for the opponent too (the .so cross-contamination
rule). Every EngineProcess respawn reconstructs Engine -> the toggle is
reapplied.
"""

from cengine import Engine as _CEngine


class Engine(_CEngine):
    def __init__(self):
        super().__init__()
        self._lib.set_qs_tt(0)      # P-44 off: v34 tree + P-22 speed
