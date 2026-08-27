#!/usr/bin/env python3
"""uci/uci_old.py -- a minimal UCI front end for the PRE-C snapshots (v1-v30).

    python3 uci/uci_old.py 12         # speak UCI as v12

The C era has uci/cuci_old.py; these predate csearch.so, so that path cannot
drive them. The modern uci.py cannot either -- it expects the C-era Engine
interface (smp_workers, _sync_c_params, ...) and the misses cascade. So this
is a SELF-CONTAINED loop against the one interface all thirty snapshots share
(verified across v1/v12/v25/v30, identical since v1):

    Engine()                                     zero-arg constructor
    get_best_move_timed(board, time_limit, max_depth=10)
    get_best_move(board, depth)
    last_score                                   White POV, cp
    MATE_SCORE = 1_000_000, mate at ply p scored +/-(MATE_SCORE - p)

The snapshot itself is NOT modified: it is importlib-loaded from its own
directory and used through those attributes only.

Score reporting is the whole point (matetrack reads `score mate N` off the
info line; the snapshots print nothing). last_score is White POV, so it is
flipped to the UCI side-to-move convention here; mate distance converts as
moves = (plies + 1) // 2 with the sign of the score.

The book is forced OFF when the snapshot has one: a book hit returns a move
with no search behind it, so last_score would be stale -- and a mate suite
must measure the SEARCH.

ONE VERSION PER PROCESS, as everywhere else in this repo: some snapshots
carry a compiled eval (engine_cy*.so) and the loader returns whichever image
it saw first.

KNOWN LIMIT: matetrack will report ~100% "Bad PVs" for these rows. The
snapshots expose only a best move and a score -- no principal variation --
so the wrapper emits a 1-move pv and matetrack cannot walk the mate line to
verify it. The found/best counts are still real; the Bad-PV column is not
meaningful for the pre-C era.

These engines are ~100-200x slower than the C core (v1 benches ~23k nps
against v60's 5.4M). At matetrack's usual 200ms they will find very little;
that is the honest historical measurement, not a fault of the wrapper.
"""

import importlib.util
import os
import sys

import chess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    raise SystemExit("usage: python3 uci/uci_old.py <version 1..30>")
_N = sys.argv[1]

_DIR = os.path.join(_ROOT, "Old Engine", _N)
_CAND = [os.path.join(_DIR, f"engine{_N}.py"), os.path.join(_DIR, "engine.py")]
_SRC = next((p for p in _CAND if os.path.isfile(p)), None)
if _SRC is None:
    raise SystemExit(f"no engine module in {_DIR}")

sys.path.insert(0, _DIR)               # snapshot finds its own siblings/.so
_spec = importlib.util.spec_from_file_location(f"engine{_N}", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _score_line(eng, board):
    """last_score (White POV) -> UCI 'score ...' fragment, side-to-move POV."""
    raw = getattr(eng, "last_score", 0) or 0
    stm = raw if board.turn == chess.WHITE else -raw
    mate_thresh = getattr(eng, "MATE_THRESHOLD",
                          getattr(eng, "MATE_SCORE", 1_000_000) - 1_000)
    mate_score = getattr(eng, "MATE_SCORE", 1_000_000)
    if abs(stm) >= mate_thresh:
        plies = mate_score - abs(stm)
        moves = (plies + 1) // 2
        return f"score mate {moves if stm > 0 else -moves}"
    return f"score cp {int(stm)}"


def main():
    eng = _mod.Engine()
    if hasattr(eng, "use_book"):
        eng.use_book = False           # a book hit bypasses the search
    board = chess.Board()
    out = sys.stdout
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        if cmd == "uci":
            out.write(f"id name Pygin v{_N} (pre-C, historical)\n")
            out.write("id author IchNukeDichWeg\n")
            out.write("uciok\n"); out.flush()
        elif cmd == "isready":
            out.write("readyok\n"); out.flush()
        elif cmd == "ucinewgame":
            board = chess.Board()
        elif cmd == "position":
            words = rest.split()
            if words and words[0] == "startpos":
                board = chess.Board(); moves = words[2:]
            elif words and words[0] == "fen":
                try:
                    mi = words.index("moves")
                    fen, moves = " ".join(words[1:mi]), words[mi + 1:]
                except ValueError:
                    fen, moves = " ".join(words[1:]), []
                board = chess.Board(fen)
            else:
                moves = []
            for u in moves:
                board.push(chess.Move.from_uci(u))
        elif cmd == "go":
            words = rest.split()
            def _arg(name, default=None):
                return (int(words[words.index(name) + 1])
                        if name in words else default)
            depth = _arg("depth")
            movetime = _arg("movetime")
            wtime, btime = _arg("wtime"), _arg("btime")
            winc, binc = _arg("winc", 0), _arg("binc", 0)
            if depth is not None:
                mv = eng.get_best_move(board, depth)
            else:
                if movetime is not None:
                    tl = movetime / 1000.0
                else:
                    # crude clock split; these wrappers exist for suites,
                    # which drive them with movetime anyway
                    t = wtime if board.turn == chess.WHITE else btime
                    inc = winc if board.turn == chess.WHITE else binc
                    tl = ((t or 1000) / 1000.0) / 30.0 + (inc or 0) / 1000.0
                mv = eng.get_best_move_timed(board, tl, max_depth=99)
            if isinstance(mv, tuple):
                mv = mv[0]
            if mv is None:                       # no legal move / terminal
                out.write("info depth 0 score cp 0\nbestmove 0000\n")
                out.flush(); continue
            out.write(f"info depth 1 {_score_line(eng, board)} "
                      f"pv {mv.uci()}\n")
            out.write(f"bestmove {mv.uci()}\n"); out.flush()
        elif cmd == "quit":
            break
        # setoption / stop / anything else: accepted silently -- the point is
        # to be drivable by python-chess and matetrack, not full compliance


if __name__ == "__main__":
    main()
