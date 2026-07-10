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

`stop` aborts the search via engine.stop() -- the host-owned `_abort` flag
plus cs_stop(); the search thread then prints the bestmove found so far
(UCI-required). `go infinite` relies on that path. The flag (cleared only
here, at each `go`) is what makes a stop that races the search thread's
startup stick: cs_stop() alone was erased by cs_search_begin, leaving
`go infinite` running to the depth cap with the host hung in join().
"""

import sys
import threading
import traceback

import chess

import cengine
from time_manager import calculate_move_time

NAME = "Pygin C-core"   # version-neutral: the old "Pygin C31" went stale
AUTHOR = "Sam"          # the moment v32 landed; snapshots carry the number


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
    # P-26: shadow copies of the paired C-side tuning values (set_rfp and
    # set_null_move each set two values; UCI options arrive one at a time).
    engine._rfp_margin, engine._rfp_depth = 80, 6
    engine._null_base, engine._null_div = 2, 6
    board = chess.Board()
    search_thread = None

    def searching():
        return search_thread is not None and search_thread.is_alive()

    def go(tokens):
        # Host-clears rule (engine.py P-05, now mirrored by cengine): _abort
        # is set by engine.stop() and only ever cleared HERE, before the next
        # search starts -- so a stop that raced the previous search thread's
        # startup can never leak into (or get erased by) this one.
        engine._abort = False
        params = {}
        it = iter(tokens)
        for tok in it:
            if tok in ("wtime", "btime", "winc", "binc", "movestogo",
                       "movetime", "depth", "nodes", "mate"):
                # B-06: a malformed number must not swallow the whole go
                # (no bestmove ever = host hang); skip the bad token.
                try:
                    params[tok] = int(next(it, 0))
                except (ValueError, TypeError):
                    pass
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

        # B-05: `go movetime X` means SPEND X -- the P-35 base soft-stop
        # (soft_stop_frac 0.55) and the U-06 stability scaling are clock-game
        # economies that would end an exact-time search at 40-80% of the
        # budget. Disable BOTH for movetime; restore for clock mode.
        if "movetime" in params:
            engine.use_stability_time = False
            engine.soft_stop_frac = None
        else:
            engine.use_stability_time = True
            engine.soft_stop_frac = 0.55     # cengine constructor default

        # B-03: UCI requires `go infinite` (and bare `go`) to hold bestmove
        # until `stop`, even if the search finishes early (mate break,
        # depth cap). Depth/time/clock-limited gos still report on completion.
        hold = ("infinite" in params) or not any(
            k in params for k in ("movetime", "wtime", "btime", "depth"))
        stop_evt = threading.Event()

        white_to_move = board.turn == chess.WHITE
        engine.on_depth = lambda rec: out(info_line(rec, white_to_move, engine))
        engine.on_final = None               # final info == last depth line

        def run():
            if budget is None:
                mv = engine.get_best_move(board.copy(), max_depth)
            else:
                mv = engine.get_best_move_timed(board.copy(), budget, max_depth)
            if hold:
                stop_evt.wait()              # B-03: hold until `stop`
            out(f"bestmove {mv.uci() if mv is not None else '0000'}")

        th = threading.Thread(target=run, daemon=True)
        th.stop_evt = stop_evt
        return th

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
                # P-26 tuning knobs (chess-tuning-tools): defaults = shipped
                # v34 values; percent-scaled where the native value is
                # fractional. Ranges are the tuner's search space.
                out("option name RFPMargin type spin default 80 min 20 max 300")
                out("option name RFPDepth type spin default 6 min 2 max 12")
                out("option name FutMargin type spin default 150 min 40 max 400")
                out("option name DeltaMargin type spin default 200 min 50 max 500")
                out("option name LMPScale type spin default 100 min 40 max 250")
                out("option name LMRDiv type spin default 200 min 120 max 350")
                out("option name NullBase type spin default 2 min 1 max 4")
                out("option name NullDiv type spin default 6 min 3 max 12")
                out("option name AspDelta type spin default 30 min 10 max 120")
                out("option name SoftStable type spin default 40 min 20 max 70")
                out("option name SoftUnstable type spin default 80 min 50 max 130")
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
                # P-26 tuning knobs. C-side setters take effect on the next
                # search; Python-side ones are plain instance attributes.
                elif name == "rfpmargin":
                    engine._lib.set_rfp(int(value), engine._rfp_depth)
                    engine._rfp_margin = int(value)
                elif name == "rfpdepth":
                    engine._lib.set_rfp(engine._rfp_margin, int(value))
                    engine._rfp_depth = int(value)
                elif name == "futmargin":
                    engine._lib.set_fut_margin(int(value))
                elif name == "deltamargin":
                    engine._lib.set_delta_margin(int(value))
                elif name == "lmpscale":
                    s = int(value)
                    engine._lib.set_lmp(round(6 * s / 100), round(10 * s / 100),
                                        round(14 * s / 100))
                elif name == "lmrdiv":
                    engine._lib.set_lmr_div(int(value))
                elif name == "nullbase":
                    engine._lib.set_null_move(int(value), engine._null_div)
                    engine._null_base = int(value)
                elif name == "nulldiv":
                    engine._lib.set_null_move(engine._null_base, int(value))
                    engine._null_div = int(value)
                elif name == "aspdelta":
                    engine.ASPIRATION_DELTA = int(value)
                elif name == "softstable":
                    engine.SOFT_STOP_STABLE_FRAC = int(value) / 100.0
                elif name == "softunstable":
                    engine.SOFT_STOP_UNSTABLE_FRAC = int(value) / 100.0
            elif cmd == "ucinewgame":
                if searching():
                    engine.stop()
                    search_thread.join()
                engine._lib.cs_tt_reset()
                engine.last_score = 0        # reset the TB difficulty gate
                board = chess.Board()
            elif cmd == "position":
                # BUG-02 + B-08: ALL-OR-NOTHING. Build on a scratch board;
                # a bad FEN or an unparseable/illegal move token rejects the
                # whole command (stderr note) and keeps the previous board --
                # never a half-applied prefix that the next `go` silently
                # searches, and never a stale board pretending to be the new
                # position without saying so.
                try:
                    if "fen" in tokens:
                        i = tokens.index("fen")
                        j = tokens.index("moves") if "moves" in tokens else len(tokens)
                        nb = chess.Board(" ".join(tokens[i + 1:j]))
                    else:                    # startpos
                        nb = chess.Board()
                    if "moves" in tokens:
                        for u in tokens[tokens.index("moves") + 1:]:
                            mv = chess.Move.from_uci(u)   # raises on garbage
                            if mv not in nb.legal_moves:
                                raise ValueError(f"illegal move {u!r}")
                            nb.push(mv)
                    board = nb
                except Exception as ex:
                    print(f"cuci: position command rejected ({ex})",
                          file=sys.stderr)
            elif cmd == "go":
                if searching():
                    continue                 # already searching; ignore
                search_thread = go(tokens[1:])
                search_thread.start()
            elif cmd == "stop":
                if searching():
                    engine.stop()
                    search_thread.stop_evt.set()   # B-03: release the hold
                    search_thread.join()
            elif cmd == "quit":
                if searching():
                    engine.stop()
                    search_thread.stop_evt.set()
                    search_thread.join()
                break
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            out(f"info string error: {err}")


if __name__ == "__main__":
    main()
