"""opponents/anchor_stockfish_full.py -- full-strength Stockfish, as a RIG TEST.

Not a rating anchor: Stockfish's own rating is far above this engine, so nearly
every game is decided the same way and the result carries almost no information.
It exists so `scripts/rating_ladder.py --check` can exercise the generic UCI
adapter end to end against a binary that is certainly present on this machine.
"""
import uci_engine

BINARY = "stockfish"
RATING = None
RATING_LIST = "n/a -- rig test, not an anchor"


class Engine(uci_engine.Engine):
    BINARY = BINARY
    OPTIONS = {}
    THREADS = 1
    HASH_MB = 64
