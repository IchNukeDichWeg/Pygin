#!/usr/bin/env python3
"""NNUE/tools/profile_nnue.py -- where the NNUE evaluation's time actually goes.

    python3 NNUE/tools/profile_nnue.py --net NNUE/nets/nnue_v1_<hash>.nnue

Splits one evaluation into accumulator, threat rebuild and tail, so the
speed work can be aimed instead of guessed. The engine's own screen says the
net concedes ~42.5% of NPS in real search; this says which part to attack.

TIMED INSIDE C (`nnue_profile` in nnue.c). A ctypes round trip costs ~1 us,
which is larger than the entire evaluation -- `verify_c.py threatcost`
reports 1689 ns/call and openly admits most of that is the call itself. Any
measurement taken from Python measures Python.

Each stage is timed separately rather than derived by subtracting one from
another, so a surprising split shows up as a surprising NUMBER rather than
as a negative remainder that has to be explained away.

Reported per stage:
  forward      full nn_forward: accumulator read + threats + tail
  threats      nn_threat_vec alone -- full attack generation for BOTH sides,
               recomputed every eval, never incremental
  tail         the three matmuls with threats supplied rather than built
  refresh      full accumulator rebuild (what a KING move costs)
  incremental  one nn_push (what a quiet move costs)
"""

import argparse
import ctypes
import os
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
NNUE_DIR = os.path.dirname(TOOLS)
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess                                                   # noqa: E402
import cengine                                                 # noqa: E402

POSITIONS = [
    ("start", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("kiwipete", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("midgame", "r2q1rk1/pp2ppbp/2np1np1/2p5/4P3/2NP1N1P/PPP1BPP1/R1BQ1RK1 w - - 0 1"),
    ("endgame", "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"),
    ("bare kings", "4k3/8/8/8/8/8/8/4K3 w - - 0 1"),
]


def bargs(b):
    occ_w = b.occupied_co[chess.WHITE]
    return (b.pawns, b.knights, b.bishops, b.rooks, b.queens, b.kings,
            occ_w, b.occupied & ~occ_w, 1 if b.turn else 0,
            b.ep_square if b.ep_square is not None else -1,
            b.clean_castling_rights())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--net", required=True)
    ap.add_argument("--iters", type=int, default=200_000,
                    help="timed repetitions per stage per position")
    args = ap.parse_args()

    eng = cengine.Engine()
    lib = eng._lib
    if not hasattr(lib, "nnue_profile"):
        sys.exit("profile_nnue: this csearch.so has no nnue_profile -- "
                 "run ./setup.sh to rebuild")
    B = ctypes.c_uint64
    lib.nnue_profile.argtypes = ([B] * 8 + [ctypes.c_int] * 2 + [B]
                                 + [ctypes.c_int, ctypes.POINTER(ctypes.c_double)])
    lib.nnue_profile.restype = ctypes.c_int
    lib.nnue_load.argtypes = [ctypes.c_char_p]
    if lib.nnue_load(os.path.abspath(args.net).encode()) != 0:
        sys.exit(f"profile_nnue: could not load {args.net}")

    buf = (ctypes.c_double * 5)()
    rows = []
    print(f"{os.path.basename(args.net)}   {args.iters:,} iterations per "
          f"stage per position\n")
    print(f"{'position':<12}{'forward':>10}{'threats':>10}{'tail':>10}"
          f"{'refresh':>10}{'increm':>10}   threats% of forward")
    for name, fen in POSITIONS:
        b = chess.Board(fen)
        if lib.nnue_profile(*bargs(b), args.iters, buf) != 0:
            sys.exit("profile_nnue: nnue_profile failed (net not loaded?)")
        fwd, thr, tail, refr, inc = [buf[i] for i in range(5)]
        rows.append((fwd, thr, tail, refr, inc))
        print(f"{name:<12}{fwd:9.0f}n{thr:9.0f}n{tail:9.0f}n"
              f"{refr:9.0f}n{inc:9.0f}n   {100 * thr / max(fwd, 1e-9):>8.1f}%")

    n = len(rows)
    avg = [sum(r[i] for r in rows) / n for i in range(5)]
    fwd, thr, tail, refr, inc = avg
    print(f"\n{'mean':<12}{fwd:9.0f}n{thr:9.0f}n{tail:9.0f}n"
          f"{refr:9.0f}n{inc:9.0f}n   {100 * thr / fwd:>8.1f}%")

    print("\nreading it:")
    print(f"  threats are {100 * thr / fwd:.0f}% of a forward pass. They are "
          f"rebuilt from scratch every\n  evaluation -- full sliding-attack "
          f"generation for both sides -- while the\n  accumulator beside them "
          f"is incremental and costs {inc:.0f}n for a quiet move.")
    if thr / fwd > 0.30:
        print("  => THREATS DOMINATE. Removing or incrementalising T16 is the "
              "lever;\n     a narrower tail would be attacking the smaller "
              "half.")
    elif tail / fwd > 0.60:
        print("  => THE TAIL DOMINATES. i8mm and a narrower first layer "
              "(528->16) are the\n     levers; incremental threats would buy "
              "little.")
    else:
        print("  => No single stage dominates; the win has to come from doing "
              "FEWER evals\n     (lazy NNUE eval) rather than cheaper ones.")
    print(f"  a king move costs {refr / max(inc, 1e-9):.0f}x a quiet move "
          f"({refr:.0f}n vs {inc:.0f}n).")


if __name__ == "__main__":
    main()
