"""opponents/anchor_weiss14.py -- Weiss 1.4, CCRL Blitz 3254.

Built from source at tag v1.4 of github.com/TerjeKir/weiss (GPL), so what plays
is a compile of the version the rating names -- not a downloaded binary.
Hand-crafted eval, no network file, plain C: it builds unchanged on arm64 and
x86, which is what makes it a portable anchor.
"""
import uci_engine

BINARY = "~/engines/bin/weiss-v1.4"
RATING = 3254
RATING_LIST = ("CCRL Blitz, all engines, list computed 2026-09-12 "
               "(2'+1\" equivalent on an i7-4770K, ponder off, "
               "books to 12 moves, 3-4-5 EGTB)")


class Engine(uci_engine.Engine):
    BINARY = BINARY
    OPTIONS = {}
    THREADS = 1
    HASH_MB = 64
