#!/usr/bin/env python3
"""hce_reference.py -- (FEN, base, full) triples for checking the JS HCE port.

    python3 NNUE/tools/hce_reference.py [N]

Writes NNUE/tools/hce_vectors.json. engine.py is the oracle: the C core is a
verified port of it, and the browser eval must be a third implementation that
agrees with both. Positions come from the bundled opening book plus the
awkward cases a port actually fails on -- empty-ish endgames, lone kings,
promotion races -- rather than only quiet middlegames where every eval agrees.

`base` is _eval_base_white (tapered material + PST + tempo) and `full` is
_evaluate_static. Splitting them means a JS mismatch says WHICH half is wrong
instead of only that the totals differ.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "lib")]

import chess                                             # noqa: E402
import engine as eng_mod                                 # noqa: E402

# Cases chosen because they break naive ports, not because they are typical.
EDGE = [
    "8/8/8/4k3/8/4K3/8/8 w - - 0 1",                     # bare kings, phase 0
    "8/8/8/8/8/8/6k1/4K2R w K - 0 1",                    # KRvK, mop-up live
    "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1",                   # single pawn, EG end
    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",         # Kiwipete-ish, passers
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "8/k7/3p4/p2P1p2/P2P1P2/8/8/K7 w - - 0 1",           # locked, zero mobility
    "8/8/1P6/8/8/8/6p1/K6k w - - 0 1",                   # promotion race
]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    e = eng_mod.Engine()
    e.use_incremental_eval = False          # force the from-scratch scan
    rows = []

    def add(fen):
        b = chess.Board(fen)
        base, ctx = e._eval_base_white(b)
        # ctx = (occ_w, occ_b, pawns, knights, bishops, rooks, queens, kings,
        #        white pawns, black pawns, phase) -- see _eval_base_white.
        wp, bp, phase = ctx[8], ctx[9], ctx[10]
        rows.append({"fen": b.fen(), "base": int(base),
                     "pawns": int(e._pawn_structure_bb(wp, bp, phase)),
                     "full": int(e._evaluate_static(b))})

    for fen in EDGE:
        add(fen)

    book = os.path.join(_ROOT, "data", "UHO_4060_v4.epd")
    if os.path.isfile(book):
        with open(book, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if len(rows) >= n:
                    break
                if i % 977:                 # spread across the file, not a prefix
                    continue
                p = line.split()
                if len(p) < 4:
                    continue
                try:
                    add(" ".join(p[:4]) + " 0 1")
                except ValueError:
                    continue
    else:
        print("note: no opening book, edge cases only")

    path = os.path.join(_HERE, "hce_vectors.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"_source": "engine.py _eval_base_white / _evaluate_static",
                   "positions": rows}, fh, indent=1)
    print(f"wrote {os.path.relpath(path, _ROOT)}: {len(rows)} positions "
          f"({len(EDGE)} edge cases)")


if __name__ == "__main__":
    main()
