#!/usr/bin/env python3
"""Add the missing startpos -> first-move entries to a book built BEFORE the
move-1 fix (commit 87b3187). Re-evaluates the first moves with Stockfish for
weights, appends them, and rewrites the .bin. Idempotent: existing startpos
entries are dropped first, so re-running is safe.

  python3 patch_book_startpos.py book.bin                 # uses make_book's seeds
  python3 patch_book_startpos.py book.bin --moves e4 d4 c4 # custom first moves
  python3 patch_book_startpos.py book.bin --out fixed.bin  # don't overwrite
"""
import argparse
import struct

import chess
import chess.engine
import chess.polyglot

from make_book import _encode_move, _white_pov_cp, SEED_FIRST_MOVES


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("book")
    p.add_argument("--moves", nargs="+", default=SEED_FIRST_MOVES,
                   help="White first moves (SAN); defaults to make_book's seeds")
    p.add_argument("--stockfish", default="stockfish")
    p.add_argument("--depth", type=int, default=20)
    p.add_argument("--out", help="output path (default: overwrite in place)")
    a = p.parse_args()

    with open(a.book, "rb") as f:
        entries = list(struct.iter_unpack(">QHHI", f.read()))
    root = chess.Board()
    root_key = chess.polyglot.zobrist_hash(root)
    before = len(entries)
    entries = [e for e in entries if e[0] != root_key]     # idempotent re-patch

    eng = chess.engine.SimpleEngine.popen_uci(a.stockfish)
    seed_evals = []
    for san in a.moves:
        try:
            mv = root.parse_san(san)
        except chess.IllegalMoveError:
            print(f"skipping illegal first move: {san!r}")
            continue
        b = root.copy()
        b.push(mv)
        info = eng.analyse(b, chess.engine.Limit(depth=a.depth))
        cp_w = _white_pov_cp(b, info["score"].relative.score(mate_score=100000))
        seed_evals.append((mv, cp_w))
    eng.quit()

    best_w = max(cp for _, cp in seed_evals)
    for mv, cp in seed_evals:
        weight = max(1, min(65535, 1000 - (best_w - cp)))
        entries.append((root_key, _encode_move(root, mv), weight, 0))

    entries.sort(key=lambda e: (e[0], e[1]))
    out = a.out or a.book
    with open(out, "wb") as f:
        for key, move, weight, _ in entries:
            f.write(struct.pack(">QHHI", key, move, weight, 0))
    print(f"patched {len(seed_evals)} startpos moves into {out} "
          f"({before} -> {len(entries)} entries)")
    b = chess.Board()
    with chess.polyglot.open_reader(out) as r:
        top = sorted(r.find_all(b), key=lambda e: e.weight, reverse=True)[:3]
        print("  move 1:", ", ".join(f"{b.san(e.move)} ({e.weight})" for e in top))


if __name__ == "__main__":
    main()
