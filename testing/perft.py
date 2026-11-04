"""
perft.py -- movegen correctness check against the published Perft Results.

Runs the C perft() exported by movegen.so over the 6 standard positions from
https://www.chessprogramming.org/Perft_Results and compares every node count
against the published values. Any mismatch = move generation is broken.

Chess960 rides along: ten positions from the chessprogramming Chess960 perft
table (king-takes-rook castling, X-FEN/Shredder rights), counted by BOTH
movegen.so and csearch.so's own generator (cs_perft) -- the two are separate
copies, and the 960 castle is where they first had to be written twice.

    python3 perft.py            # quick suite (~16M nodes, a few seconds)
    python3 perft.py --deep     # full suite  (~600M nodes, ~1-2 minutes)

Exit code 0 = all pass, 1 = any mismatch (usable from other scripts/CI).
"""
# Path shim: this script moved into a subfolder on 2026-07-24 but
# still imports the engine modules that live at the repo root.
import os as _os, sys as _sys
_ROOT_ = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_ROOT_, _os.path.join(_ROOT_, "lib")]
import argparse
import ctypes
import os
import sys
import time

import chess

import interruptible

# The .so files stay at the repo ROOT (the Makefile and every Old Engine/<N>/
# snapshot expect them flat); this script lives in testing/ since 2026-07-24.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lib = ctypes.CDLL(os.path.join(HERE, "movegen.so"))
_lib.perft.restype = ctypes.c_uint64
_lib.perft.argtypes = [ctypes.c_uint64] * 8 + [ctypes.c_int, ctypes.c_int,
                                               ctypes.c_uint64, ctypes.c_int]
_lib.abi_version.restype = ctypes.c_int
_lib.set_chess960.argtypes = [ctypes.c_int]
# csearch.so is optional here: perft.py must still gate movegen.so on a
# checkout where only movegen has been built.
try:
    _cs = ctypes.CDLL(os.path.join(HERE, "csearch.so"))
    _cs.cs_perft.restype = ctypes.c_uint64
    _cs.cs_perft.argtypes = _lib.perft.argtypes
    _cs.cs_set_chess960.argtypes = [ctypes.c_int]
except (OSError, AttributeError):
    _cs = None

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


# Chess960 -- chessprogramming.org/Chess960_Perft_Results, the first ten rows
# (python-chess agrees on every count). Depth 6 is in the table but omitted:
# up to 1.25B nodes per position is past even --deep's budget.
POSITIONS_960 = [
    ("960 #1", "bqnb1rkr/pp3ppp/3ppn2/2p5/5P2/P2P4/NPP1P1PP/BQ1BNRKR w HFhf - 2 9",
     {1: 21, 2: 528, 3: 12_189, 4: 326_672, 5: 8_146_062}),
    ("960 #2", "2nnrbkr/p1qppppp/8/1ppb4/6PP/3PP3/PPP2P2/BQNNRBKR w HEhe - 1 9",
     {1: 21, 2: 807, 3: 18_002, 4: 667_366, 5: 16_253_601}),
    ("960 #3", "b1q1rrkb/pppppppp/3nn3/8/P7/1PPP4/4PPPP/BQNNRKRB w GE - 1 9",
     {1: 20, 2: 479, 3: 10_471, 4: 273_318, 5: 6_417_013}),
    ("960 #4", "qbbnnrkr/2pp2pp/p7/1p2pp2/8/P3PP2/1PPP1KPP/QBBNNR1R w hf - 0 9",
     {1: 22, 2: 593, 3: 13_440, 4: 382_958, 5: 9_183_776}),
    ("960 #5", "1nbbnrkr/p1p1ppp1/3p4/1p3P1p/3Pq2P/8/PPP1P1P1/QNBBNRKR w HFhf - 0 9",
     {1: 28, 2: 1_120, 3: 31_058, 4: 1_171_749, 5: 34_030_312}),
    ("960 #6", "qnbnr1kr/ppp1b1pp/4p3/3p1p2/8/2NPP3/PPP1BPPP/QNB1R1KR w HEhe - 1 9",
     {1: 29, 2: 899, 3: 26_578, 4: 824_055, 5: 24_851_983}),
    ("960 #7", "q1bnrkr1/ppppp2p/2n2p2/4b1p1/2NP4/8/PPP1PPPP/QNB1RRKB w ge - 1 9",
     {1: 30, 2: 860, 3: 24_566, 4: 732_757, 5: 21_093_346}),
    ("960 #8", "qbn1brkr/ppp1p1p1/2n4p/3p1p2/P7/6PP/QPPPPP2/1BNNBRKR w HFhf - 0 9",
     {1: 25, 2: 635, 3: 17_054, 4: 465_806, 5: 13_203_304}),
    ("960 #9", "qnnbbrkr/1p2ppp1/2pp3p/p7/1P5P/2NP4/P1P1PPP1/Q1NBBRKR w HFhf - 0 9",
     {1: 24, 2: 572, 3: 15_243, 4: 384_260, 5: 11_110_203}),
    ("960 #10", "qn1rbbkr/ppp2p1p/1n1pp1p1/8/3P4/P6P/1PP1PPPK/QNNRBB1R w hd - 2 9",
     {1: 28, 2: 811, 3: 23_175, 4: 679_699, 5: 19_836_606}),
]


def _args(board):
    return (board.pawns, board.knights, board.bishops, board.rooks,
            board.queens, board.kings,
            board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK],
            1 if board.turn == chess.WHITE else 0,
            board.ep_square if board.ep_square is not None else -1,
            board.clean_castling_rights())


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
    # Chess960: both generators flip to king-takes-rook castling for these
    # rows only, and back afterwards -- the flags are process-wide.
    gens = [("movegen.so", _lib.set_chess960, lambda b, d: _lib.perft(*_args(b), d))]
    if _cs is not None:
        gens.append(("csearch.so", _cs.cs_set_chess960,
                     lambda b, d: _cs.cs_perft(*_args(b), d)))
    else:
        print("\n  note csearch.so not built -- 960 rows counted on movegen.so only")
    print()
    for gname, set960, count in gens:
        set960(1)
        try:
            for name, fen, table in POSITIONS_960:
                board = chess.Board(fen, chess960=True)
                for depth in sorted(table):
                    expected = table[depth]
                    if cap is not None and expected > cap:
                        continue
                    t0 = time.perf_counter()
                    got = count(board, depth)
                    dt = time.perf_counter() - t0
                    total_nodes += got
                    total_t += dt
                    ok = got == expected
                    failed += not ok
                    mark = "ok  " if ok else "FAIL"
                    print(f"  {mark} {name:<9} {gname:<11} d{depth}  {got:>13,}"
                          + ("" if ok else f"  (expected {expected:,})")
                          + (f"  [{dt:.2f}s]" if dt >= 0.1 else ""))
        finally:
            set960(0)

    nps = total_nodes / total_t if total_t > 0 else 0
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILURE(S)'} -- "
          f"{total_nodes:,} nodes in {total_t:.2f}s ({nps/1e6:.2f}M nps)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    # Ctrl-C / SIGTERM: one line, no traceback, exit 130.
    with interruptible.salvage():
        main()
