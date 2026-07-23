"""
perft.py -- movegen correctness check against the published Perft Results.

Runs the C perft() exported by movegen.so over the 6 standard positions from
https://www.chessprogramming.org/Perft_Results and compares every node count
against the published values. Any mismatch = move generation is broken.

    python3 perft.py            # quick suite (~16M nodes, a few seconds)
    python3 perft.py --deep     # full suite  (~600M nodes, ~1-2 minutes)

Exit code 0 = all pass, 1 = any mismatch (usable from other scripts/CI).
"""
import argparse
import ctypes
import os
import sys
import time

import chess

import interruptible

HERE = os.path.dirname(os.path.abspath(__file__))
_lib = ctypes.CDLL(os.path.join(HERE, "movegen.so"))
_lib.perft.restype = ctypes.c_uint64
_lib.perft.argtypes = [ctypes.c_uint64] * 8 + [ctypes.c_int, ctypes.c_int,
                                               ctypes.c_uint64, ctypes.c_int]
_lib.abi_version.restype = ctypes.c_int

# (name, fen, {depth: published nodes})  -- chessprogramming.org/Perft_Results
POSITIONS = [
    ("Pos 1 startpos", chess.STARTING_FEN,
     {1: 20, 2: 400, 3: 8_902, 4: 197_281, 5: 4_865_609, 6: 119_060_324}),
    ("Pos 2 Kiwipete",
     "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
     {1: 48, 2: 2_039, 3: 97_862, 4: 4_085_603, 5: 193_690_690}),
    ("Pos 3 endgame",
     "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
     {1: 14, 2: 191, 3: 2_812, 4: 43_238, 5: 674_624, 6: 11_030_083,
      7: 178_633_661}),
    ("Pos 4 promo-mania",
     "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
     {1: 6, 2: 264, 3: 9_467, 4: 422_333, 5: 15_833_292, 6: 706_045_033}),
    ("Pos 5 buggy-ep/castle",
     "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
     {1: 44, 2: 1_486, 3: 62_379, 4: 2_103_487, 5: 89_941_194}),
    ("Pos 6 Steven Edwards",
     "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10",
     {1: 46, 2: 2_079, 3: 89_890, 4: 3_894_594, 5: 164_075_551}),
]


def c_perft(board, depth):
    return _lib.perft(
        board.pawns, board.knights, board.bishops, board.rooks,
        board.queens, board.kings,
        board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK],
        1 if board.turn == chess.WHITE else 0,
        board.ep_square if board.ep_square is not None else -1,
        board.clean_castling_rights(), depth)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--deep", action="store_true",
                    help="run every published depth (~600M nodes) "
                         "instead of the quick <=5M-node cap")
    args = ap.parse_args()
    cap = None if args.deep else 5_000_000

    print(f"movegen.so ABI {_lib.abi_version()} -- "
          f"{'FULL' if args.deep else 'quick'} suite\n")
    failed = 0
    total_nodes, total_t = 0, 0.0
    for name, fen, table in POSITIONS:
        board = chess.Board(fen)
        for depth in sorted(table):
            expected = table[depth]
            if cap is not None and expected > cap:
                continue
            t0 = time.perf_counter()
            got = c_perft(board, depth)
            dt = time.perf_counter() - t0
            total_nodes += got
            total_t += dt
            ok = got == expected
            failed += not ok
            mark = "ok  " if ok else "FAIL"
            print(f"  {mark} {name:<22} d{depth}  {got:>13,}"
                  + ("" if ok else f"  (expected {expected:,})")
                  + (f"  [{dt:.2f}s]" if dt >= 0.1 else ""))
    nps = total_nodes / total_t if total_t > 0 else 0
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILURE(S)'} -- "
          f"{total_nodes:,} nodes in {total_t:.2f}s ({nps/1e6:.2f}M nps)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    # Ctrl-C / SIGTERM: one line, no traceback, exit 130.
    with interruptible.salvage():
        main()
