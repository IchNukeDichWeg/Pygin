#!/usr/bin/env python3
"""
cuci.py -- UCI wrapper for the C search core (cengine.py).

    python3 cuci.py

Speaks standard UCI for external GUIs / match runners (cutechess-ob, Arena,
lichess-bot, ...). The engine is cengine.Engine (csearch.so under a Python
root driver); clock handling goes through the project's standard
time_manager.calculate_move_time, so `go wtime/btime/winc/binc` gets the
same budgets the internal harnesses use.

Options:
    Threads  (spin 1..64, default 1)  -- Lazy-SMP helper threads in C
    OwnBook  (check, default true)    -- engine's own Polyglot book
    UseTB    (check, default false)   -- root Lichess-Syzygy probe
                                         (difficulty-gated; needs network)

`stop` aborts the C search via cs_stop(); the search thread then prints the
bestmove found so far (UCI-required). `go infinite` relies on that path.
"""

import sys
import threading
import traceback

import chess

import cengine
from time_manager import calculate_move_time

NAME = "Pygin C31"
AUTHOR = "Sam"


def out(line):
    print(line, flush=True)


def info_line(rec, white_to_move, engine):
    """Map a cengine record dict (White-POV, v30 mate convention) to UCI."""
    score = rec.get("score", 0)
    stm = score if white_to_move else -score
    if abs(stm) >= engine.MATE_THRESHOLD:
        plies = engine.MATE_SCORE - abs(stm)
        moves = (plies + 1) // 2
        score_str = f"mate {moves if stm > 0 else -moves}"
    else:
        score_str = f"cp {stm}"
    t = max(1, rec.get("time_ms", 0))
    nodes = rec.get("nodes", 0)
    parts = [f"info depth {rec.get('depth', 0)}", f"score {score_str}",
             f"nodes {nodes}", f"nps {int(nodes * 1000 / t)}", f"time {t}"]
    pv = rec.get("pv", "")
    if pv:
        parts.append(f"pv {pv}")
    return " ".join(parts)


def main():
    engine = cengine.Engine()
    engine.pv_uci = True                     # UCI pv format
    board = chess.Board()
    search_thread = None

    def searching():
        return search_thread is not None and search_thread.is_alive()

    def go(tokens):
        params = {}
        it = iter(tokens)
        for tok in it:
            if tok in ("wtime", "btime", "winc", "binc", "movestogo",
                       "movetime", "depth", "nodes", "mate"):
                params[tok] = int(next(it, 0))
            elif tok == "infinite":
                params["infinite"] = True

        max_depth = int(params.get("depth", 60))
        if "movetime" in params:
            budget = params["movetime"] / 1000.0
        elif "wtime" in params or "btime" in params:
            my = params.get("wtime" if board.turn else "btime", 0)
            opp = params.get("btime" if board.turn else "wtime", 0)
            inc = params.get("winc" if board.turn else "binc", 0)
            budget = calculate_move_time(
                board, my, opp, inc,
                movestogo=params.get("movestogo")) / 1000.0
        elif "infinite" in params or "depth" in params:
            budget = None                    # until `stop` / depth cap
        else:
            budget = None                    # bare `go` == go infinite

        white_to_move = board.turn == chess.WHITE
        engine.on_depth = lambda rec: out(info_line(rec, white_to_move, engine))
        engine.on_final = None               # final info == last depth line

        def run():
            if budget is None:
                mv = engine.get_best_move(board.copy(), max_depth)
            else:
                mv = engine.get_best_move_timed(board.copy(), budget, max_depth)
            out(f"bestmove {mv.uci() if mv is not None else '0000'}")

        return threading.Thread(target=run, daemon=True)

    for raw in sys.stdin:
        # BUG-01: malformed input must never kill the process mid-game --
        # that's an instant forfeit (uci.py's Z-02 rule). Log + continue.
        try:
            line = raw.strip()
            if not line:
                continue
            tokens = line.split()
            cmd = tokens[0]

            if cmd == "uci":
                out(f"id name {NAME}")
                out(f"id author {AUTHOR}")
                out("option name Threads type spin default 1 min 1 max 64")
                out("option name OwnBook type check default true")
                out("option name UseTB type check default false")
                out("uciok")
            elif cmd == "isready":
                out("readyok")
            elif cmd == "setoption" and len(tokens) >= 5 and tokens[1] == "name":
                name = tokens[2].lower()
                value = tokens[4]
                if name == "threads":
                    engine.smp_workers = max(1, min(64, int(value)))
                elif name == "ownbook":
                    engine.use_book = value.lower() == "true"
                elif name == "usetb":
                    engine.use_tb = value.lower() == "true"
            elif cmd == "ucinewgame":
                if searching():
                    engine.stop()
                    search_thread.join()
                engine._lib.cs_tt_reset()
                engine.last_score = 0        # reset the TB difficulty gate
                board = chess.Board()
            elif cmd == "position":
                if "fen" in tokens:
                    i = tokens.index("fen")
                    j = tokens.index("moves") if "moves" in tokens else len(tokens)
                    board = chess.Board(" ".join(tokens[i + 1:j]))
                else:                        # startpos
                    board = chess.Board()
                if "moves" in tokens:
                    # BUG-02: guard every push -- an unparseable/illegal
                    # token must stop cleanly HERE, never leave a
                    # half-applied board that the next `go` silently
                    # searches (uci.py's rule).
                    for u in tokens[tokens.index("moves") + 1:]:
                        try:
                            mv = chess.Move.from_uci(u)
                        except ValueError:
                            break
                        if mv not in board.legal_moves:
                            break
                        board.push(mv)
            elif cmd == "go":
                if searching():
                    continue                 # already searching; ignore
                search_thread = go(tokens[1:])
                search_thread.start()
            elif cmd == "stop":
                if searching():
                    engine.stop()
                    search_thread.join()
            elif cmd == "quit":
                if searching():
                    engine.stop()
                    search_thread.join()
                break
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            out(f"info string error: {err}")


if __name__ == "__main__":
    main()
