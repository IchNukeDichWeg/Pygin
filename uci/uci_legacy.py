#!/usr/bin/env python3
"""uci/uci_legacy.py -- a minimal UCI front end for the PRE-C snapshots.

    python3 uci/uci_legacy.py 12          # speak UCI as v12

v1-v30 predate the C search core AND the project's UCI layer: they are plain
Engine classes with no protocol of their own, and the modern uci.py cannot
front-end them (it wants smp_workers, _sync_c_params and more they never had).
Without a protocol they emit no score lines, so a mate suite cannot read them
at all -- which is why matetrack stopped at v31.

They do, however, all expose the same three things, checked across v1/v8/v15/
v22/v30: get_best_move_timed(board, seconds), self.last_score, and
MATE_SCORE / MATE_THRESHOLD. That is exactly enough to synthesise the protocol
around them, so this speaks just the subset a mate suite uses:

    uci / isready / ucinewgame / position / go / stop / quit

and emits `info depth D score {mate N|cp X} pv <move>` before `bestmove`.

DELIBERATELY MINIMAL. No pondering, no MultiPV, no options -- an old engine
has nothing to configure. Anything unrecognised is ignored rather than
answered wrongly, because a wrong answer is worse than a silent one when the
consumer is deciding whether a mate was found.

Score convention: last_score is side-to-move POV, mate encoded as
MATE_SCORE - plies. UCI wants mate in MOVES, signed from the side to move,
hence the (plies + 1) // 2.
"""

import importlib.util
import os
import sys

import chess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    raise SystemExit("usage: python3 uci/uci_legacy.py <version-number>")
_N = sys.argv[1]

_DIR = os.path.join(_ROOT, "Old Engine", _N)
_CAND = [os.path.join(_DIR, f"engine{_N}.py"), os.path.join(_DIR, "engine.py")]
_SRC = next((p for p in _CAND if os.path.isfile(p)), None)
if _SRC is None:
    raise SystemExit(f"no engine module in {_DIR}")

sys.path.insert(0, _DIR)                 # the snapshot's own siblings
_spec = importlib.util.spec_from_file_location(f"engine{_N}", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _score_line(eng, board):
    """UCI score line. last_score is ALREADY side-to-move POV -- measured.

    The source is misleading: the value is assigned from `best_score_white` /
    `score_white` and v1 even comments it as "White's view". It is not. On a
    black-to-move mate-in-1 the raw value reads +999998, i.e. positive for the
    MOVER, which is the UCI convention already. Converting on the strength of
    the variable NAME inverts every black-to-move position, and a white-to-move
    test still passes, which is how that error hides. Measured, not read.
    """
    sc = int(getattr(eng, "last_score", 0) or 0)
    ms = int(getattr(eng, "MATE_SCORE", 1_000_000))
    mt = int(getattr(eng, "MATE_THRESHOLD", ms - 1000))
    if abs(sc) > mt:
        plies = ms - abs(sc)
        moves = (plies + 1) // 2
        return f"score mate {moves if sc > 0 else -moves}"
    return f"score cp {sc}"


def main():
    eng = _mod.Engine()
    # Force single-threaded. v21-v24 lazily `from smp import SMPPool` inside
    # the search, and their snapshots lost smp.py when the repo later gathered
    # shared modules into lib/ -- so a threaded search dies with
    # ModuleNotFoundError. Setting one worker never reaches that import, and a
    # mate suite runs one position per process anyway, so this is the
    # configuration the measurement wants regardless. Grafting a neighbouring
    # release's smp.py would be the alternative, and would silently mix two
    # versions' code.
    for attr in ("smp_workers", "SMP_WORKERS"):
        if hasattr(eng, attr):
            try:
                setattr(eng, attr, 1)
            except Exception:
                pass
    board = chess.Board()
    out = sys.stdout
    for raw in sys.stdin:
        cmd = raw.strip()
        if cmd == "uci":
            out.write(f"id name Pygin v{_N} (legacy)\nid author Pygin\nuciok\n")
        elif cmd == "isready":
            out.write("readyok\n")
        elif cmd == "ucinewgame":
            board = chess.Board()
        elif cmd.startswith("position"):
            parts = cmd.split()
            if "startpos" in parts:
                board = chess.Board()
                i = parts.index("startpos") + 1
            elif "fen" in parts:
                i = parts.index("fen") + 1
                fen = " ".join(parts[i:i + 6])
                board = chess.Board(fen)
                i += 6
            else:
                continue
            if i < len(parts) and parts[i] == "moves":
                for mv in parts[i + 1:]:
                    board.push(chess.Move.from_uci(mv))
        elif cmd.startswith("go"):
            parts = cmd.split()
            secs, depth = None, None
            for k, flag in (("movetime", 1000.0), ("wtime", 1000.0)):
                if k in parts:
                    secs = float(parts[parts.index(k) + 1]) / flag
                    break
            if "depth" in parts:
                depth = int(parts[parts.index("depth") + 1])
            try:
                if depth is not None:
                    mv = eng.get_best_move(board, depth)
                else:
                    mv = eng.get_best_move_timed(board, secs if secs else 1.0)
            except Exception as exc:                      # never hang the GUI
                out.write(f"info string error: {type(exc).__name__}: {exc}\n")
                mv = next(iter(board.legal_moves), None)
            if isinstance(mv, tuple):
                mv = mv[0]
            d = int(getattr(eng, "last_depth", 0) or 0)
            if mv is not None:
                out.write(f"info depth {d} {_score_line(eng, board)} pv {mv.uci()}\n")
                out.write(f"bestmove {mv.uci()}\n")
            else:
                out.write("bestmove 0000\n")
        elif cmd in ("stop", "quit"):
            if cmd == "quit":
                break
        out.flush()


if __name__ == "__main__":
    main()
