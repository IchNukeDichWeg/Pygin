#!/usr/bin/env python3
"""NNUE/selftest_nnue.py -- NNUE unit checks for selftest.py.

Run standalone or via selftest.py (which spawns this as a subprocess:
cengine's FB-04 one-process-one-config rule forbids a second differently-
configured Engine in the selftest process itself).

Exit codes: 0 = all NNUE checks pass, 42 = SKIPPED (no net file -- not a
failure: the build is dormant until a net is trained), 1 = a check failed.
Never touches the pinned ladder: g_use_nnue changes node counts by design,
so everything here asserts game-theoretic outcomes (mates, draws), oracle
agreement, and accumulator integrity -- not node counts.
"""

import ctypes
import os
import sys

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

NET = os.path.join(NNUE_DIR, "nets", "toy.nnue")

if not os.path.exists(NET):
    print(f"skip: no net file at {NET} (dormant build; train one via "
          "NNUE/README.md)")
    sys.exit(42)

import chess
import numpy as np

from nnue_ref import QuantNet, extract_features, PAD_IDX
from verify_c import bargs, random_positions, to_record

import cengine
cengine.Engine.USE_NNUE = True
cengine.Engine.NNUE_FILE = NET
eng = cengine.Engine()
eng.use_book = False
eng.use_tb = False
eng.smp_workers = 1
lib = eng._lib
lib.nnue_eval_oracle.argtypes = \
    [ctypes.c_uint64] * 8 + [ctypes.c_int] * 2 + [ctypes.c_uint64]
lib.nnue_eval_oracle.restype = ctypes.c_int
lib.nnue_threats.argtypes = lib.nnue_eval_oracle.argtypes + \
    [ctypes.POINTER(ctypes.c_uint8)]
lib.nnue_verify_stats.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 2

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  ({detail})" if detail else ""))
    if not ok:
        fails.append(label)


# 1. oracle agreement: C forward vs numpy reference on 200 positions
q = QuantNet.load(NET)
tbuf = (ctypes.c_uint8 * 16)()
mism = 0
for b in random_positions(200, seed=5):
    r = to_record(b)
    iw, ib = extract_features(r)
    lib.nnue_threats(*bargs(b), tbuf)
    ref = q.forward([x for x in iw[0] if x != PAD_IDX],
                    [x for x in ib[0] if x != PAD_IDX],
                    list(tbuf), 1 if b.turn else 0)
    if lib.nnue_eval_oracle(*bargs(b)) != ref:
        mism += 1
check("nn_eval oracle == numpy reference (200 pos)", mism == 0,
      f"{mism} mismatches")

# 2. accumulator integrity: verify-mode searches, 0 mismatches
lib.set_nnue_verify(1)
for fen in (chess.STARTING_FEN,
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "8/P5k1/8/8/8/8/1p4K1/8 w - - 0 1"):
    lib.cs_tt_reset()
    eng.get_best_move(chess.Board(fen), 6)
lib.set_nnue_verify(0)
p = ctypes.c_uint64(0)
bad = ctypes.c_uint64(0)
lib.nnue_verify_stats(ctypes.byref(p), ctypes.byref(bad))
check("incremental accumulator == full refresh", bad.value == 0,
      f"{p.value:,} pushes, {bad.value} mismatches")

# 3. mate minisuite with the net on (game-theoretic outcome, not nodes)
mates_ok = True
for fen, d in (("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", 4),
               ("r6k/5ppp/8/8/8/8/1R3PPP/1R4K1 w - - 0 1", 6)):
    lib.cs_tt_reset()
    eng.get_best_move(chess.Board(fen), d)
    if eng.last_score < eng.MATE_THRESHOLD:
        mates_ok = False
check("mate minisuite with g_use_nnue=1", mates_ok)

# 4. draw machinery with the net on (rule draws are search-side, pre-eval;
# d16 = the main selftest's fortress depth -- at d12 a noisy toy net's
# leaves haven't all collapsed into the cycle bound yet)
lib.cs_tt_reset()
eng.get_best_move(chess.Board("k7/8/8/p1p1p1p1/P1P1P1P1/8/8/K7 w - - 0 1"), 16)
check("blocked-wall fortress scores 0 with net on", eng.last_score == 0,
      f"score {eng.last_score}")
lib.cs_tt_reset()
eng.get_best_move(chess.Board("8/8/8/4k3/8/2N5/8/4K3 w - - 0 1"), 8)
check("KNvK insufficient-material draw with net on",
      abs(eng.last_score) <= 60, f"score {eng.last_score}")

if fails:
    print(f"NNUE selftest FAILED: {', '.join(fails)}")
    sys.exit(1)
print("NNUE selftest OK")
