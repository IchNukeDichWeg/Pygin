"""
tactics.py
==========
Tactical solve-rate test: run the engine over an EPD suite (default ``wac.epd``,
a Stockfish-verified set of genuine tactics) at a fixed time per position and
report how many it solves. A quick, repeatable diagnostic -- and a regression
guard: re-run after any change to make sure tactical strength didn't drop.

    python3 tactics.py            # CPython
    pypy3   tactics.py            # PyPy (faster -> effectively more search)

Config (edit below): ENGINE_FILE, EPD_FILE, TIME_MS (per position).
A position counts as solved if the engine's move is one of the EPD ``bm`` moves.
"""

import importlib.util
import sys
import time

import chess

ENGINE_FILE = "engine.py"
EPD_FILE = "blindspots.epd"
TIME_MS = 2000               # think time per position (ms); set USE_DEPTH for fixed depth
USE_DEPTH = None             # e.g. 9 to test at fixed depth instead of time


def load_engine(path):
    spec = importlib.util.spec_from_file_location("tactics_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Engine()


def parse_epd(line):
    """Return (fen, set_of_bm_uci) or None. Accepts bm in UCI or SAN."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # FEN is the first 4 fields (+ optional clocks); operations follow.
    parts = line.split()
    fen = " ".join(parts[:6]) if len(parts) >= 6 and parts[4].lstrip("-").isdigit() \
        else " ".join(parts[:4]) + " 0 1"
    board = chess.Board(fen)
    bm = set()
    if " bm " in line:
        seg = line.split(" bm ", 1)[1].split(";")[0].strip()
        for tok in seg.split():
            try:
                bm.add(chess.Move.from_uci(tok).uci())
            except ValueError:
                try:
                    bm.add(board.parse_san(tok).uci())
                except ValueError:
                    pass
    if not bm:
        return None
    return fen, bm


def main():
    eng = load_engine(ENGINE_FILE)
    eng.use_book = False
    eng.get_best_move(chess.Board(), 4)        # warm-up (matters under PyPy)

    suite = []
    with open(EPD_FILE) as fh:
        for line in fh:
            p = parse_epd(line)
            if p:
                suite.append(p)

    impl = getattr(sys, "implementation", None)
    interp = f"{impl.name} {sys.version.split()[0]}" if impl else "python"
    budget = f"depth {USE_DEPTH}" if USE_DEPTH else f"{TIME_MS} ms"
    print(f"Tactics: {ENGINE_FILE}  |  {interp}  |  {len(suite)} positions @ {budget}\n")

    solved = 0
    t0 = time.time()
    for i, (fen, bm) in enumerate(suite, 1):
        board = chess.Board(fen)
        if USE_DEPTH:
            mv = eng.get_best_move(board, USE_DEPTH)
        else:
            mv = eng.get_best_move_timed(board, TIME_MS / 1000.0, 30)
        got = mv.uci() if mv else "----"
        ok = got in bm
        solved += ok
        want = " ".join(board.san(chess.Move.from_uci(u)) for u in bm)
        flag = "OK " if ok else "XX "
        print(f"  {flag}{i:2}/{len(suite)}  got {board.san(mv) if mv else '--':6} "
              f"want {want:8}  (d{eng.last_depth}, {eng.last_score:+d})")

    dt = time.time() - t0
    print(f"\nSolved {solved}/{len(suite)} ({100*solved/len(suite):.0f}%) in {dt:.1f}s")


if __name__ == "__main__":
    main()
