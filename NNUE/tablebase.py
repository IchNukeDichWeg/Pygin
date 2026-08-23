#!/usr/bin/env python3
"""NNUE/tablebase.py -- Syzygy probing for TRAINING LABELS.

The labeller's teacher is a 5,000-node search with the hand-crafted eval, and
in sparse endgames that teacher is provably wrong. Measured on gen_only,
2026-08-23: of 4,000 sampled positions with <=5 pieces, **31% carry a label
the tablebase contradicts** -- rook+bishop vs rook scored +451, rook+knight vs
rook +420, knight+pawn vs king +766, all of them dead draws. 5.1% of that
corpus is <=5 pieces, so roughly 915,000 records hold a provably wrong label,
concentrated exactly where eval_bench found the net's worst errors.

No amount of training fixes a label that is wrong at the source. A tablebase
probe replaces it with the truth.

WDL ONLY. `.rtbw` files are all a label needs (win/draw/loss); `.rtbz` (DTZ,
distance-to-zero) exists to PLAY the ending out and is 561 MB against WDL's
379 MB. Training never needs it.

    from tablebase import Tablebase
    tb = Tablebase("syzygy")          # or None/"" -> disabled, and says so
    v = tb.probe(board)               # -1/0/+1 WHITE POV, or None if unknown

This is deliberately NOT the engine's UseTB, which is an online Lichess probe
for root moves during play. Different job, different failure modes.
"""

import os
import sys

MAX_PIECES = 5          # what the shipped .rtbw set covers


class Tablebase:
    """A Syzygy handle that is honest about being unavailable.

    Passing a path that cannot be opened RAISES rather than silently probing
    nothing: a relabel run that quietly found no tables would look like "the
    labels were already right" and is exactly the kind of null result this
    project has been burned by. Passing None disables probing explicitly.
    """

    def __init__(self, path):
        self.path = path or None
        self._tb = None
        self.hits = self.misses = 0
        if not self.path:
            return
        if not os.path.isdir(self.path):
            raise FileNotFoundError(
                f"syzygy path {self.path!r} is not a directory -- pass the "
                f"folder holding the .rtbw files, or omit it to disable")
        wdl = [f for f in os.listdir(self.path) if f.endswith(".rtbw")]
        if not wdl:
            raise FileNotFoundError(
                f"no .rtbw files under {self.path!r} -- DTZ (.rtbz) alone "
                f"cannot answer win/draw/loss")
        import chess.syzygy
        self._tb = chess.syzygy.open_tablebase(self.path)
        self.n_tables = len(wdl)

    @property
    def enabled(self):
        return self._tb is not None

    def probe(self, board):
        """-1 / 0 / +1 from WHITE's point of view, or None when unknown.

        python-chess returns WDL from the SIDE TO MOVE's perspective; every
        label in a .pygdata record is White-POV, so this converts. Getting
        that backwards would invert exactly the black-to-move half of the
        corpus, silently.
        """
        if self._tb is None:
            return None
        if _piece_count(board) > MAX_PIECES:
            self.misses += 1
            return None
        try:
            wdl = self._tb.probe_wdl(board)
        except Exception:          # position not in the set, or castling rights
            self.misses += 1
            return None
        self.hits += 1
        if board.turn:             # chess.WHITE is True
            return (wdl > 0) - (wdl < 0)
        return (wdl < 0) - (wdl > 0)

    def close(self):
        if self._tb is not None:
            self._tb.close()
            self._tb = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _piece_count(board):
    return bin(board.occupied).count("1")


def add_arg(ap, required=False):
    """Register --syzygy on an argparse parser, one wording everywhere."""
    ap.add_argument("--syzygy", metavar="DIR", default=None, required=required,
                    help="folder of Syzygy .rtbw files (<=5 pieces). Labels "
                         "for positions inside the set come from the tablebase "
                         "instead of the search, which is measurably wrong "
                         "there (31%% of <=5-piece labels in gen_only)")


if __name__ == "__main__":                      # smoke check
    import chess
    path = sys.argv[1] if len(sys.argv) > 1 else "syzygy"
    tb = Tablebase(path)
    print(f"opened {tb.n_tables} WDL tables from {path!r}")
    cases = [
        ("6R1/8/8/3KB3/8/4r2k/8/8 w - - 0 1", 0, "R+B vs R, drawn"),
        ("6R1/3K4/8/3N4/8/r7/5k2/8 w - - 0 1", 0, "R+N vs R, drawn"),
        ("8/8/8/8/8/2k5/8/KQ6 w - - 0 1", 1, "KQ vs K, white wins"),
        ("8/8/8/8/8/2K5/8/kq6 b - - 0 1", -1, "KQ vs K, black wins"),
    ]
    bad = 0
    for fen, want, why in cases:
        got = tb.probe(chess.Board(fen))
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {got:>2} (want {want:>2})  {why}")
    tb.close()
    sys.exit(1 if bad else 0)
