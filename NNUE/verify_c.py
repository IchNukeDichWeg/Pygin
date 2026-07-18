#!/usr/bin/env python3
"""NNUE/verify_c.py -- FI-15 Phase 4 verification gates.

    python3 NNUE/verify_c.py forward   [--positions 100000] [--net PATH]
    python3 NNUE/verify_c.py increment [--pushes 1000000]   [--net PATH]
    python3 NNUE/verify_c.py nps       [--net PATH] [--seconds 4]
    python3 NNUE/verify_c.py threatcost [--calls 200000]

  forward    gate (a): C forward pass (nnue_eval_oracle, full refresh) vs
             the numpy quantized reference (nnue_ref.QuantNet.forward) on
             random-game positions -- EXACT match required, 0 tolerance.
             Also cross-checks nnue_features_oracle vs extract_features.
             Without --net, a seeded random net is written first (the gate
             tests arithmetic, not learning).
  increment  gate (b): run real searches with set_nnue_verify(1) -- every
             nn_push re-derives the child slot by full refresh and
             compares (values + mirror/bucket metadata). Covers ordinary
             moves, captures, castling, promotions, ep; null moves are a
             slot copy (exact by construction). Requires 0 mismatches.
  nps        g_use_nnue on vs off throughput on the bench FENs (fresh
             subprocess per config, per the one-process-one-config rule).
  threatcost the measured cost of the T16 recompute (nnue_threats), the
             number the Phase-1 spec promises to document.
"""

import argparse
import ctypes
import os
import random
import subprocess
import sys

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess

from config import IN_DIM, HIDDEN, THREAT_DIM, D2, D3, NETS_DIR
from data_format import RECORD_DTYPE
from nnue_ref import QuantNet, extract_features, PAD_IDX

B64 = ctypes.c_uint64
BARGS = [B64] * 8 + [ctypes.c_int] * 2 + [B64]


def bargs(board):
    ep = board.ep_square if board.ep_square is not None else -1
    return (board.pawns, board.knights, board.bishops, board.rooks,
            board.queens, board.kings,
            board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK],
            1 if board.turn else 0, ep, board.clean_castling_rights())


def load_lib():
    lib = ctypes.CDLL(os.path.join(REPO_DIR, "csearch.so"))
    lib.nnue_load.argtypes = [ctypes.c_char_p]
    lib.nnue_eval_oracle.argtypes = BARGS
    lib.nnue_eval_oracle.restype = ctypes.c_int
    lib.nnue_threats.argtypes = BARGS + [ctypes.POINTER(ctypes.c_uint8)]
    lib.nnue_features_oracle.argtypes = BARGS + [ctypes.c_int,
                                                ctypes.POINTER(ctypes.c_int)]
    lib.nnue_features_oracle.restype = ctypes.c_int
    return lib


def random_net(path, seed=1234):
    rng = np.random.default_rng(seed)
    QuantNet.from_float(
        rng.normal(0, 0.05, (IN_DIM, HIDDEN)),
        rng.normal(0, 0.2, HIDDEN),
        rng.normal(0, 0.3, (D2, 2 * HIDDEN + THREAT_DIM)),
        rng.normal(0, 0.2, D2),
        rng.normal(0, 0.3, (D3, D2)), rng.normal(0, 0.2, D3),
        rng.normal(0, 0.3, D3), 0.05).save(path)
    return path


def default_net(args):
    if args.net:
        return args.net
    os.makedirs(NETS_DIR, exist_ok=True)
    p = os.path.join(NETS_DIR, "verify_random.nnue")
    random_net(p)
    print(f"(using seeded random net {p})")
    return p


def random_positions(n, seed=99):
    """Positions from random games (all move classes represented)."""
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        b = chess.Board()
        for _ in range(rng.randint(10, 160)):
            moves = list(b.legal_moves)
            if not moves:
                break
            b.push(rng.choice(moves))
            if b.is_game_over():
                break
            out.append(b.copy(stack=False))
            if len(out) >= n:
                break
    return out


def to_record(board):
    r = np.zeros((), dtype=RECORD_DTYPE)
    r["pawns"], r["knights"], r["bishops"] = \
        board.pawns, board.knights, board.bishops
    r["rooks"], r["queens"], r["kings"] = \
        board.rooks, board.queens, board.kings
    r["occ_w"] = board.occupied_co[chess.WHITE]
    r["castling"] = board.clean_castling_rights()
    r["stm"] = 1 if board.turn else 0
    r["ep"] = board.ep_square if board.ep_square is not None else -1
    return r


def cmd_forward(args):
    net_path = default_net(args)
    lib = load_lib()
    rc = lib.nnue_load(net_path.encode())
    assert rc == 0, f"nnue_load rc={rc}"
    q = QuantNet.load(net_path)
    boards = random_positions(args.positions)
    print(f"comparing C forward vs numpy reference on {len(boards)} "
          "random-game positions ...")
    tbuf = (ctypes.c_uint8 * 16)()
    fbuf = (ctypes.c_int * 40)()
    mism = feat_mism = 0
    for k, b in enumerate(boards):
        r = to_record(b)
        idx_w, idx_b = extract_features(r)
        iw = [x for x in idx_w[0] if x != PAD_IDX]
        ib = [x for x in idx_b[0] if x != PAD_IDX]
        if k < 2000:      # feature-index parity subset (diagnostic depth)
            for persp, py in ((1, iw), (0, ib)):
                n = lib.nnue_features_oracle(*bargs(b), persp, fbuf)
                if sorted(fbuf[:n]) != sorted(py):
                    feat_mism += 1
        lib.nnue_threats(*bargs(b), tbuf)
        ref = q.forward(iw, ib, list(tbuf), 1 if b.turn else 0)
        cv = lib.nnue_eval_oracle(*bargs(b))
        if cv != ref:
            mism += 1
            if mism <= 3:
                print(f"  MISMATCH {b.fen()}  C={cv} ref={ref}")
        if (k + 1) % 10000 == 0:
            print(f"  {k+1}/{len(boards)} ... ({mism} mismatches)")
    print(f"forward gate: {len(boards)} positions, {mism} eval mismatches, "
          f"{feat_mism} feature-set mismatches (subset of 2000)")
    sys.exit(0 if mism == 0 and feat_mism == 0 else 1)


def cmd_increment(args):
    net_path = default_net(args)
    import cengine
    cengine.Engine.USE_NNUE = True
    cengine.Engine.NNUE_FILE = os.path.abspath(net_path)
    eng = cengine.Engine()
    eng.use_book = False
    eng.use_tb = False
    eng.smp_workers = 1
    lib = eng._lib
    lib.nnue_verify_stats.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 2
    lib.set_nnue_verify(1)
    fens = [
        chess.STARTING_FEN,
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "8/P5k1/8/8/8/8/1p4K1/8 w - - 0 1",                  # promo race
        "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3",  # ep
        "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3",
        "8/2k5/3p4/p2P1p2/P2P1P2/8/8/4K3 w - - 0 1",
    ]
    p = ctypes.c_uint64(0)
    bad = ctypes.c_uint64(0)
    rng = random.Random(7)
    depth_first = True
    while True:
        for fen in fens:
            eng.get_best_move(chess.Board(fen), 7 if depth_first else 6)
        # random middlegames for breadth
        for _ in range(10):
            b = chess.Board()
            for _ in range(rng.randint(6, 60)):
                mv = list(b.legal_moves)
                if not mv:
                    break
                b.push(rng.choice(mv))
                if b.is_game_over():
                    break
            if not b.is_game_over():
                eng.get_best_move(b, 6)
        lib.nnue_verify_stats(ctypes.byref(p), ctypes.byref(bad))
        print(f"  pushes {p.value:,}  mismatches {bad.value}", flush=True)
        if bad.value or p.value >= args.pushes:
            break
        depth_first = False
    print(f"increment gate: {p.value:,} incremental pushes re-derived by "
          f"full refresh, {bad.value} mismatches")
    sys.exit(0 if bad.value == 0 and p.value >= args.pushes else 1)


def cmd_nps(args):
    net_path = os.path.abspath(default_net(args))
    code = r"""
import sys, time, chess
sys.path.insert(0, {repo!r})
import cengine
if {on}:
    cengine.Engine.USE_NNUE = True
    cengine.Engine.NNUE_FILE = {net!r}
e = cengine.Engine()
e.use_book = e.use_tb = False
e.smp_workers = 1
import cuci
tot = n = 0.0
for fen in cuci.BENCH_FENS:
    e._lib.cs_tt_reset()
    t0 = time.perf_counter()
    e.get_best_move_timed(chess.Board(fen), {secs}, max_depth=99)
    dt = time.perf_counter() - t0
    tot += e.nodes_searched; n += dt
print(f"{{'on' if {on} else 'off'}}: {{tot/n:,.0f}} nps")
"""
    for on in (0, 1):
        subprocess.run([sys.executable, "-c",
                        code.format(repo=REPO_DIR, on=on, net=net_path,
                                    secs=args.seconds / 1.0)],
                       cwd=REPO_DIR, check=True)


def cmd_threatcost(args):
    lib = load_lib()
    boards = random_positions(2000)
    tbuf = (ctypes.c_uint8 * 16)()
    argsets = [bargs(b) for b in boards]
    import time
    t0 = time.perf_counter()
    n = 0
    while n < args.calls:
        for a in argsets:
            lib.nnue_threats(*a, tbuf)
        n += len(argsets)
    dt = time.perf_counter() - t0
    print(f"threatcost: {n:,} nnue_threats calls in {dt:.2f}s = "
          f"{dt/n*1e9:.0f} ns/call (includes ctypes overhead; the in-search "
          "cost is lower)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gate", choices=["forward", "increment", "nps",
                                     "threatcost"])
    ap.add_argument("--positions", type=int, default=100_000)
    ap.add_argument("--pushes", type=int, default=1_000_000)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--calls", type=int, default=200_000)
    ap.add_argument("--net")
    args = ap.parse_args()
    {"forward": cmd_forward, "increment": cmd_increment,
     "nps": cmd_nps, "threatcost": cmd_threatcost}[args.gate](args)


if __name__ == "__main__":
    main()
