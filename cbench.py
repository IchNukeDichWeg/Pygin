#!/usr/bin/env python3
"""
cbench.py -- NPS benchmark for the C search core (csearch.so).

    python3 cbench.py [depth]      # default depth 8

Runs the standard bench positions at a fixed depth through search_bench
(fresh TT per position, full window -- the same instrument every phase-3
NPS number in DESIGN_c_search_core.md was measured with), with the eval
tables synced from engine.py exactly as cengine does in play. Prints
per-position and overall nodes/NPS plus the ratio to the Python engine's
~90k baseline.
"""

import ctypes
import sys
import time

import chess

DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 8
PY_BASELINE_NPS = 90_000            # v30 full search, for the ratio line

POSITIONS = [
    ("startpos",   chess.STARTING_FEN),
    ("kiwipete",   "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq -"),
    ("middlegame", "r2q1rk1/pp1bbppp/2n1pn2/2pp4/3P1B2/2NPPN2/PP3PPP/2RQKB1R w K -"),
    ("endgame",    "8/5pk1/6p1/8/6P1/5PK1/8/8 w - -"),
]

import cengine                                       # noqa: E402

ce = cengine.Engine()                # loads csearch.so + syncs all eval params
lib = ce._lib
lib.search_bench.restype = ctypes.c_uint32
lib.search_bench.argtypes = [ctypes.c_uint64] * 8 + [ctypes.c_int] * 2 + \
    [ctypes.c_uint64, ctypes.c_int,
     ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_int)]

print(f"== C core NPS bench (depth {DEPTH}) ==\n")
tot_nodes, tot_time = 0, 0.0
for name, fen in POSITIONS:
    b = chess.Board(fen)
    nodes = ctypes.c_uint64(0)
    score = ctypes.c_int(0)
    t0 = time.perf_counter()
    key = lib.search_bench(*ce._bargs(b), DEPTH,
                           ctypes.byref(nodes), ctypes.byref(score))
    dt = time.perf_counter() - t0
    tot_nodes += nodes.value
    tot_time += dt
    mv = ce._key_to_move(key)
    print(f"  {name:11s} d{DEPTH}: {mv.uci() if mv else '----':6s} "
          f"{score.value:+6d}  nodes={nodes.value:>10,}  "
          f"{dt*1000:7.1f}ms  {nodes.value/dt/1e6:6.2f}M nps")

nps = tot_nodes / tot_time
print(f"\n  overall: {tot_nodes:,} nodes in {tot_time:.2f}s = "
      f"{nps/1e6:.2f}M nps  (~{nps/PY_BASELINE_NPS:.0f}x the Python engine)")
