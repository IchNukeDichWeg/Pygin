#!/usr/bin/env python3
"""NNUE/tools/compare_eval.py -- HCE vs NNUE on the same positions.

    python3 NNUE/tools/compare_eval.py --net NNUE/nets/nnue_v1_<hash>.nnue \
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    python3 NNUE/tools/compare_eval.py --net <net> --random 300

Both numbers come from the ENGINE -- csearch_eval_white for the hand-crafted
evaluation, nnue_eval_oracle for the net -- so the comparison is exact and
needs no reimplementation of either.

WHY THIS IS NOT IN THE BROWSER TOOL: the .nnue side is ~150 lines of integer
arithmetic and was portable; verifying it against the C took 40 exported
positions. The HCE is thousands of lines of tapered, phase-scaled, clamped
terms with mop-up and cant-win shaping on top. Porting that to JavaScript
would be a large surface with no independent truth to check against, and a
port that is subtly wrong is worse than no comparison at all -- it would
disagree with the engine while looking authoritative. So the HCE number is
produced where it is already correct, and the browser tool stays honest
about only knowing the net.

Both values are WHITE-POV centipawns, matching the inspector's headline
number, so a FEN read in one can be pasted into the other.
"""

import argparse
import ctypes
import os
import sys

NNUE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess                                                   # noqa: E402
import numpy as np                                             # noqa: E402
import cengine                                                 # noqa: E402


def bargs(b):
    occ_w = b.occupied_co[chess.WHITE]
    return (b.pawns, b.knights, b.bishops, b.rooks, b.queens, b.kings,
            occ_w, b.occupied & ~occ_w, 1 if b.turn else 0,
            b.ep_square if b.ep_square is not None else -1,
            b.clean_castling_rights())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fens", nargs="*", help="FEN(s) to compare")
    ap.add_argument("--net", required=True, help="the .nnue to load")
    ap.add_argument("--random", type=int, default=0, metavar="N",
                    help="instead of FENs, sample N random self-play "
                         "positions and report the distribution")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    eng = cengine.Engine()
    eng.USE_NNUE = True
    eng.NNUE_FILE = os.path.abspath(args.net)
    lib = eng._lib
    B = ctypes.c_uint64
    sig = [B] * 8 + [ctypes.c_int] * 2 + [B]
    lib.csearch_eval_white.argtypes = sig
    lib.csearch_eval_white.restype = ctypes.c_int
    lib.nnue_eval_oracle.argtypes = sig
    lib.nnue_eval_oracle.restype = ctypes.c_int
    if lib.nnue_load(os.path.abspath(args.net).encode()) != 0:
        sys.exit(f"compare_eval: could not load {args.net}")

    boards = [chess.Board(f) for f in args.fens]
    if args.random:
        from verify_c import random_positions
        boards += list(random_positions(args.random, seed=args.seed))
    if not boards:
        sys.exit("compare_eval: give at least one FEN, or --random N")

    quiet = args.random and not args.fens
    if not quiet:
        print(f"{'':38s} {'HCE':>8s} {'NNUE':>8s} {'diff':>8s}")
    diffs = []
    for b in boards:
        hce = lib.csearch_eval_white(*bargs(b))
        # the oracle returns stm-POV, like the net in search; flip to White
        stm = lib.nnue_eval_oracle(*bargs(b))
        nn = stm if b.turn else -stm
        diffs.append(nn - hce)
        if not quiet:
            label = b.fen().split(" ")[0]
            label = label if len(label) <= 36 else label[:33] + "..."
            print(f"{label:38s} {hce:>8d} {nn:>8d} {nn - hce:>+8d}")

    if len(diffs) > 1:
        a = np.asarray(diffs)
        print(f"\n{len(a)} positions   mean {a.mean():+.1f} cp   "
              f"median {np.median(a):+.1f} cp   "
              f"mean |diff| {np.abs(a).mean():.1f} cp   "
              f"p5/p95 {np.percentile(a, 5):+.0f}/{np.percentile(a, 95):+.0f}")
        print("A large mean is the two evaluations disagreeing on SCALE, which "
              "is expected and\nharmless; a large mean |diff| with a small mean "
              "is them disagreeing per POSITION,\nwhich is where the net is "
              "actually adding or destroying information.")


if __name__ == "__main__":
    main()
