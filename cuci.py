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
    Threads       (spin 1..64, default 1)  -- Lazy-SMP helper threads in C
    OwnBook       (check, default true)    -- engine's own Polyglot book
    UseTB         (check, default false)   -- root Lichess-Syzygy probe
                                              (difficulty-gated; needs network)
    Move Overhead (spin 0..5000, default 40) -- per-move clock slack, ms
    Hash          (spin 2..3072 MB, default 48) -- C TT size (FI-10;
                                              resize wipes the table)
    (+ the P-26 tuning spins; `bench` prints the OpenBench nodes signature;
    `go nodes N` is honored via a C-side node budget)

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
    # FI-13a: seldepth (deepest ply incl. extensions/qsearch) + hashfull
    # (TT permille) -- standard GUI fields, sampled from the C side.
    parts = [f"info depth {rec.get('depth', 0)}",
             f"seldepth {engine._lib.cs_seldepth()}",
             f"score {score_str}",
             f"nodes {nodes}", f"nps {int(nodes * 1000 / t)}",
             f"hashfull {engine._lib.cs_hashfull()}", f"time {t}"]
    pv = rec.get("pv", "")
    if pv:
        parts.append(f"pv {pv}")
    return " ".join(parts)


# FI-13c: OpenBench-style `bench` -- fixed suite, fixed depth, cold TT per
# position; the node total is the reproducible signature.
BENCH_FENS = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3",
    "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10",
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
    "8/2k5/3p4/p2P1p2/P2P1P2/8/8/4K3 w - - 0 1",
    "r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1",
]


def run_bench(engine, depth=11):
    import time as _time
    total, t0 = 0, _time.perf_counter()
    for fen in BENCH_FENS:
        engine._lib.cs_tt_reset()
        engine.get_best_move(chess.Board(fen), depth)
        total += engine.nodes_searched
    dt = max(1e-9, _time.perf_counter() - t0)
    out(f"{total} nodes {int(total / dt)} nps")


def main():
    engine = cengine.Engine()
    engine.pv_uci = True                     # UCI pv format
    engine.move_overhead_ms = 40             # FI-13b: UCI Move Overhead
    # P-26: shadow copies of the paired C-side tuning values (set_rfp and
    # set_null_move each set two values; UCI options arrive one at a time).
    # FB-06: PUSH them once so Python is authoritative -- if a C default ever
    # drifts, the first setoption would otherwise pair a stale shadow with it.
    engine._rfp_margin, engine._rfp_depth = 80, 6
    engine._null_base, engine._null_div = 2, 6
    engine._lib.set_rfp(engine._rfp_margin, engine._rfp_depth)
    engine._lib.set_null_move(engine._null_base, engine._null_div)
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
        # FB-09: honor `go nodes N` (deterministic testing / OpenBench);
        # None = unlimited. Applied per-go, cleared after.
        engine.node_limit = params.get("nodes") or None
        if "movetime" in params:
            # FB-09/B-22: movetime 0 (or negative) means "move now", not
            # "search until the depth cap" -- clamp to a near-instant budget.
            budget = max(1, params["movetime"]) / 1000.0
        elif "wtime" in params or "btime" in params:
            my = params.get("wtime" if board.turn else "btime", 0)
            opp = params.get("btime" if board.turn else "wtime", 0)
            inc = params.get("winc" if board.turn else "binc", 0)
            budget = calculate_move_time(
                board, my, opp, inc,
                overhead_ms=engine.move_overhead_ms,   # FI-13b
                movestogo=params.get("movestogo")) / 1000.0
        elif "nodes" in params:
            budget = None                    # node-limited: C aborts at N
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
        # depth cap). Depth/time/clock/node-limited gos report on completion.
        hold = ("infinite" in params) or not any(
            k in params for k in ("movetime", "wtime", "btime", "depth",
                                  "nodes"))
        stop_evt = threading.Event()
        holding = threading.Event()          # FB-14: search DONE, only holding

        white_to_move = board.turn == chess.WHITE
        def on_depth(rec):
            if rec.get("book"):
                out(f"info string book move {rec['move']}")
            elif rec.get("tb"):
                out(f"info string tablebase move {rec['move']} wdl {rec['wdl']}")
            out(info_line(rec, white_to_move, engine))
        engine.on_depth = on_depth
        engine.on_final = None               # final info == last depth line
        # FB-13d: snapshot the position NOW -- a `position` command racing
        # the thread's startup must not change what gets searched.
        search_board = board.copy()

        def run():
            # FB-02: an unhandled exception here used to kill the thread
            # silently -- no bestmove EVER = the host hangs the whole slot.
            # Always emit a bestmove; 0000 on error (arbiter-visible, not
            # a hang).
            mv = None
            try:
                if budget is None:
                    mv = engine.get_best_move(search_board, max_depth)
                else:
                    mv = engine.get_best_move_timed(search_board, budget,
                                                    max_depth)
            except Exception as ex:
                print(f"cuci: search error: {ex!r}", file=sys.stderr)
            finally:
                engine.node_limit = None     # FB-09: per-go, don't leak
                holding.set()                # FB-14: from here on, a release
                if hold:                     # is instant -- no search running
                    stop_evt.wait()          # B-03: hold until `stop`
                out(f"bestmove {mv.uci() if mv is not None else '0000'}")

        th = threading.Thread(target=run, daemon=True)
        th.stop_evt = stop_evt
        th.holding = holding
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
                out("option name Move Overhead type spin default 40 min 0 max 5000")
                out("option name Hash type spin default 48 min 2 max 3072")
                # FI-13d: self-identifying config line (A/B forensics: PGN
                # headers grep this to know exactly what was playing).
                out(f"info string abi={engine._lib.csearch_abi()}"
                    f" pv_exact={int(engine.PV_EXACT)}"
                    f" check_ext_budget={engine.CHECK_EXT_BUDGET}"
                    f" outpost={int(engine.USE_OUTPOST)}"
                    f" score_hygiene={int(engine.SCORE_HYGIENE)}"
                    f" simplify={int(engine.USE_SIMPLIFY)}"
                    f" threads={engine.smp_workers}")
                out("uciok")
            elif cmd == "isready":
                out("readyok")
            elif cmd == "setoption" and len(tokens) >= 3 and tokens[1] == "name":
                # FB-13a: UCI option names may be MULTI-WORD ("Move Overhead")
                # -- parse name as everything up to the `value` keyword and
                # normalize by dropping spaces, so single-word names keep
                # matching exactly as before.
                if "value" in tokens:
                    vi = tokens.index("value")
                    name = "".join(tokens[2:vi]).lower()
                    value = " ".join(tokens[vi + 1:])
                else:
                    name = "".join(tokens[2:]).lower()
                    value = ""
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
                elif name == "moveoverhead":            # FI-13b
                    engine.move_overhead_ms = max(0, int(value))
                elif name == "hash":                    # FI-10: MB -> bits
                    if not searching():                 # resize = realloc;
                        mb = max(2, min(3072, int(value)))   # never mid-search
                        entries = mb * 1024 * 1024 // 24
                        engine._lib.set_tt_bits(entries.bit_length() - 1)
            elif cmd == "bench":                        # FI-13c: OpenBench
                if not searching():
                    run_bench(engine)
            elif cmd == "ucinewgame":
                if searching():
                    engine.stop()
                    # FB-01: a HELD search (go infinite / bare go) blocks on
                    # stop_evt after unwinding -- joining without releasing
                    # it deadlocked the whole host on GUIs that send
                    # ucinewgame mid-analysis.
                    search_thread.stop_evt.set()
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
                    # FB-14: a self-terminated `go infinite` (mate break /
                    # depth cap) leaves its thread HOLDING the bestmove --
                    # dropping this go would mean no bestmove for it, ever
                    # (silent host hang on GUIs that skip the `stop`).
                    # Holding-only thread: implicit stop -- release the held
                    # bestmove, join, proceed. Genuinely live search: keep
                    # the old behavior (UCI says the GUI must stop first).
                    if search_thread.holding.is_set():
                        search_thread.stop_evt.set()
                        search_thread.join()
                    else:
                        continue             # actively searching; ignore
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
