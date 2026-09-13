"""NNUE/shims/engine_pgo.py -- the same engine, PGO-built core.

Loads csearch_pgo.so instead of csearch.so. Node-identical by construction --
same sources, same flags apart from the profile -- so any difference the NPS
instrument reports is speed and nothing else. A DISTINCT filename is required:
dyld resolves by name, so two builds sharing one basename would silently return
whichever image loaded first.

    python3 bench/nps13.py cengine.py NNUE/shims/engine_pgo.py --rounds 16
"""
import cengine


class Engine(cengine.Engine):
    CSEARCH_SO = "csearch_pgo.so"
