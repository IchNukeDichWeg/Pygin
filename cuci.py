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
    Threads       (spin 1..256, default 1) -- Lazy-SMP helper threads in C
    MultiPV       (spin 1..5, default 1)   -- k best lines per go (analysis;
                                              >1 bypasses the opening book,
                                              else book hits show no lines;
                                              =1 is byte-identical to before,
                                              match play never sets it)
    OwnBook       (check, default true)    -- engine's own Polyglot book
    BookFile      (string, default empty)  -- path to a custom Polyglot .bin
                                              (empty = bundled Perfect2023.bin)
    UseTB         (check, default false)   -- root Lichess-Syzygy probe
                                              (difficulty-gated; needs network)
    Move Overhead (spin 0..5000, default 40) -- per-move clock slack, ms
    Hash          (spin 2..6144 MB, default 192) -- C TT size (FI-10;
                                              resize wipes the table)
    (+ the P-26 tuning spins; `bench [depth]` prints the OpenBench nodes
    signature -- CONFIG-RELATIVE: it re-baselines after every tree-changing
    ship, so compare only within one confirmed version; `go nodes N` is
    honored via a C-side node budget)

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
import math

# WDL model -- fitted by fit_wdl_model.py (coefficients from wdl_model.json). Converts pygin's own
# cp score + game phase into Stockfish-style win/draw/loss permille, so the extension's WDL readout
# works on pygin too. Local-only (pygin is not in the public fork). Refit via fit_wdl_model.py; do
# not hand-edit the coefficients.
_WDL_AS = [-89.57259612411593, 374.8719716130802, -467.12182754889454, 279.5527333843635]
_WDL_BS = [97.47922944308569, -12.091574718162514, -98.21868968801675, 112.03272964593339]
_WDL_PHASE_MAX = 24
_WDL_PHASE_CLAMP_MIN = 6

def _win_rate_model(cp, phase):
    """P(win) for a score of `cp` centipawns (side-to-move POV) at game `phase` (0..24)."""
    cp = max(-1000, min(1000, cp))                       # match the fit's cp clamp
    m = min(max(phase, _WDL_PHASE_CLAMP_MIN), _WDL_PHASE_MAX) / _WDL_PHASE_MAX
    a = ((_WDL_AS[0] * m + _WDL_AS[1]) * m + _WDL_AS[2]) * m + _WDL_AS[3]
    b = ((_WDL_BS[0] * m + _WDL_BS[1]) * m + _WDL_BS[2]) * m + _WDL_BS[3]
    z = max(-60.0, min(60.0, (a - cp) / b))              # guard math.exp overflow
    return 1.0 / (1.0 + math.exp(z))

def _wdl_permille(cp, phase):
    """(win, draw, loss) permille ints summing to 1000 -- Stockfish's UCI `wdl` convention."""
    w = _win_rate_model(cp, phase)
    l = _win_rate_model(-cp, phase)
    d = max(0.0, 1.0 - w - l)
    vals = [round(w * 1000), round(d * 1000), round(l * 1000)]
    vals[vals.index(max(vals))] += 1000 - sum(vals)       # rounding can miss 1000 by +/-1
    return vals[0], vals[1], vals[2]

def _board_phase(board):
    """Mirror engine.py's tapered-eval phase: N/B weight 1, R weight 2, Q weight 4, capped at 24."""
    return min(24, bin(board.knights).count("1") + bin(board.bishops).count("1")
               + bin(board.rooks).count("1") * 2 + bin(board.queens).count("1") * 4)

NAME = "Pygin C-core"   # version-neutral: the old "Pygin C31" went stale
AUTHOR = "Nuke"         # version-neutral pseudonym; snapshots carry the number


def out(line):
    print(line, flush=True)


def info_line(rec, white_to_move, engine, multipv=None, board=None):
    """Map a cengine record dict (White-POV, v30 mate convention) to UCI.
    `multipv` (int) tags the line for MultiPV consumers; None = untagged
    (identical to the pre-MultiPV output). `board` (when given) adds a `wdl`
    token from the fitted model so the extension can show win/draw/loss."""
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
    booky = bool(rec.get("book") or rec.get("tb"))   # FB-25: no search ran;
    parts = [f"info depth {rec.get('depth', 0)}",     # the C counters still
             f"seldepth {0 if booky else engine._lib.cs_seldepth()}",  # hold
             *([f"multipv {multipv}"] if multipv is not None else []),
             f"score {score_str}",                    # the PREVIOUS search's
             f"nodes {nodes}", f"nps {int(nodes * 1000 / t)}",  # values
             f"hashfull {0 if booky else engine._lib.cs_hashfull()}",
             f"time {t}"]
    # WDL (permille, side-to-move POV) for real cp scores only -- not mate/book/tb positions.
    if (board is not None and not booky and getattr(engine, "show_wdl", True)
            and abs(stm) < engine.MATE_THRESHOLD):
        win, draw, loss = _wdl_permille(stm, _board_phase(board))
        parts.append(f"wdl {win} {draw} {loss}")
    pv = rec.get("pv", "")
    if pv:
        parts.append(f"pv {pv}")
    return " ".join(parts)


# FI-13c: OpenBench-style `bench` -- fixed suite, fixed depth, cold TT per
# position; the node total is the reproducible signature.
# FB-39: 6 FENs. Adding/removing a FEN re-baselines the signature and
# invalidates every stored comparison; only do that at a tree-changing ship.
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
    saved = (engine.use_book, engine.use_tb, engine.smp_workers,
             engine.on_depth, engine.on_final)
    engine.use_book = engine.use_tb = False   # FB-20: the signature must not
    engine.smp_workers = 1                    # depend on .bin files -- nor on
    engine.on_depth = engine.on_final = None  # Threads (FB-32): 1-thread only.
                                              # FB-37: a prior go()'s closure
                                              # must not spray info lines into
                                              # the nodes/nps output (same
                                              # leak class as FB-20/FB-32)
    try:
        total, t0 = 0, _time.perf_counter()
        for fen in BENCH_FENS:
            engine._lib.cs_tt_reset()
            engine.get_best_move(chess.Board(fen), depth)
            total += engine.nodes_searched
        dt = max(1e-9, _time.perf_counter() - t0)
        out(f"{total} nodes {int(total / dt)} nps")
    finally:
        (engine.use_book, engine.use_tb, engine.smp_workers,
         engine.on_depth, engine.on_final) = saved


def _emit_multipv(engine, board, best_mv, k, budget, white_to_move, stop_evt):
    """MultiPV k>1 (analysis feature, never active in match play): line 1 is
    the just-finished main search; lines 2..k re-search with the better
    lines' first moves EXCLUDED at the C root (root_exclude_*, abi 10) --
    the warm TT makes those re-searches cheap. Emits one
    `info ... multipv i ...` line per line, best first, BEFORE bestmove.
    The engine's last_* snapshot is restored afterwards so PM-01's premove
    certification (and the GUI-facing state) still see line 1."""
    import time as _t
    lib = engine._lib
    depth = engine.last_depth or 1
    # FB-38: line 1's dt was hardcoded 0.0 -> time_ms clamped to 1 ->
    # info_line printed nps = nodes*1000 (billions). The main search's
    # real elapsed is the final search_log record (int ms -> seconds).
    main_ms = (engine.search_log[-1].get("time_ms", 0)
               if engine.search_log else 0)
    lines = [(engine.last_score, engine.last_pv, depth,
              engine.nodes_searched, max(main_ms, 1) / 1000.0)]
    excl = [best_mv]
    sv = (engine.last_score, engine.last_pv, engine.last_depth,
          engine.nodes_searched, engine.use_book, engine.on_depth,
          engine.on_final)
    engine.use_book = False              # book replies would shadow line 2+
    engine.on_depth = engine.on_final = None   # no per-depth spam for extras
    per = max(0.05, min(1.0, (budget or 1.0) * 0.25))  # per extra line
    try:
        for _ in range(k - 1):
            if stop_evt.is_set() or engine._abort:
                break
            lib.root_exclude_clear()
            for m in excl:               # 15-bit key: from|to<<6|promo<<12
                lib.root_exclude_add(m.from_square | (m.to_square << 6)
                                     | ((m.promotion or 0) << 12))
            t0 = _t.perf_counter()
            mv = engine.get_best_move_timed(board, per, depth)
            dt = _t.perf_counter() - t0
            if mv is None:               # fewer legal moves than k
                break
            lines.append((engine.last_score, engine.last_pv,
                          engine.last_depth, engine.nodes_searched, dt))
            excl.append(mv)
    finally:
        lib.root_exclude_clear()         # NEVER leak exclusions into play
        (engine.last_score, engine.last_pv, engine.last_depth,
         engine.nodes_searched, engine.use_book, engine.on_depth,
         engine.on_final) = sv
    for i, (score, pv, d, nodes, dt) in enumerate(lines, 1):
        rec = {"depth": d, "score": score, "pv": pv, "nodes": nodes,
               "time_ms": max(1, int(dt * 1000))}
        out(info_line(rec, white_to_move, engine, multipv=i, board=board))


# --------------------------------------------------------------------------- #
# PM-01: certified instant reply (opt-in via `setoption name Premove value
# true`; inert by default, zero effect on match play).
#
# After `bestmove m1` the engine keeps working ON THE OPPONENT'S CLOCK for up
# to PREMOVE_CAP_S and FOLLOWS ITS OWN LINE, emitting an ordered CHAIN of
# certified pairs via spec-ignored info-string lines the bridge parses:
#   info string pygin-reply <r> <m>     -- "if the opponent plays r, answer m
#                                          instantly" (the client walks the
#                                          chain on exact matches only: zero
#                                          misfire risk)
#   info string pygin-end               -- collection terminator (always)
# Chain caps (don't trade depth for speed): at most 2 pairs where the
# opponent had a CHOICE, and only while the searched line REMAINING after
# each pair is >= PREMOVE_MIN_LINE plies (a d13 search affords 1 pair, d14+
# the full 2, below d12 none) -- but UNCAPPED while the opponent's reply is
# FORCED (single legal move: mate funnels, forced recaptures -- nothing to
# search, no depth lost).
# Quality gate: the answer must be depth-stable (d6 and d9 agree) -- a missing
# reply costs one normal round-trip; a wrong one would cost a game. The
# certification searches warm the TT with exactly the position the next move
# will face (a free poor-man's ponder), and the loop bails on stop_evt (a new
# go/stop/ucinewgame joins within one ms-scale step).
# --------------------------------------------------------------------------- #
PREMOVE_CHECK_DEPTH = 6
PREMOVE_TABLE_DEPTH = 9
PREMOVE_FORCED_DEPTH = 10
PREMOVE_MIN_LINE = 10    # a choice-pair may only be played while the line
                         # REMAINING after it is >= this deep: each instant
                         # reply consumes 2 plies of the searched line, so a
                         # d13 search affords 1 pair (13-2=11 ok, 13-4=9 no),
                         # d14+ affords the max 2, below d12 none at all
PREMOVE_CAP_S = 0.1      # hard wall-clock cap (user: bullet-safe)


def certify_premoves(engine, board, my_move, stop_evt):
    """Return an ordered CHAIN of certified (predicted_reply, answer) pairs,
    following the engine's own PV. BOARD is the position MY_MOVE was played
    from. Two caps, per the design rule "don't trade depth for speed":
      * at most 2 pairs where the opponent had a CHOICE (each instant reply
        skips a full search, so an uncapped chain would play shallow) --
      * UNCAPPED while the opponent's reply is FORCED (single legal move --
        mate funnels, forced recapture ladders: no depth is lost, there was
        nothing to search)."""
    import time as _t
    t_end = _t.perf_counter() + PREMOVE_CAP_S
    pv = (engine.last_pv or "").split()      # read BEFORE any cert search
    d0 = engine.last_depth or 0              # the searched line's depth
    b = board.copy()
    b.push(my_move)
    chain = []
    normal = 0                               # pairs where opponent had choice
    pvi = 1                                  # next PV token = opponent reply
    pv_ok = True                             # PV still aligned with the chain
    while not stop_evt.is_set() and _t.perf_counter() < t_end:
        if b.is_game_over():
            break
        replies = list(b.legal_moves)
        if len(replies) == 1:                # FORCED: safe, uncapped
            r = replies[0]
            bb = b.copy(); bb.push(r)
            if bb.is_game_over():
                break
            m = engine.get_best_move(bb, PREMOVE_FORCED_DEPTH)
            if m is None:
                break
            chain.append((r, m))
            # keep PV alignment only if the line predicted this exchange
            if pv_ok and pvi + 1 < len(pv) and pv[pvi] == r.uci() \
                    and pv[pvi + 1] == m.uci():
                pvi += 2
            else:
                pv_ok = False
            b = bb; b.push(m)
            continue
        # CHOICE: follow the PV prediction -- capped at 2 such pairs AND
        # only while the line remaining after this pair is >= d10 deep
        # (PREMOVE_MIN_LINE): never trade real depth for instant speed.
        if (normal >= 2 or d0 - 2 * (normal + 1) < PREMOVE_MIN_LINE
                or not pv_ok or pvi >= len(pv)):
            break
        try:
            r = chess.Move.from_uci(pv[pvi])
        except ValueError:
            break
        if r not in b.legal_moves:
            break
        bb = b.copy(); bb.push(r)
        if bb.is_game_over():
            break
        m6 = engine.get_best_move(bb, PREMOVE_CHECK_DEPTH)
        s6 = engine.last_score
        if stop_evt.is_set() or _t.perf_counter() > t_end:
            break
        m9 = engine.get_best_move(bb, PREMOVE_TABLE_DEPTH)
        s9 = engine.last_score
        if m9 is None or m6 != m9 or abs(s9 - s6) > 60:
            break                            # not depth-stable: stop here
        if pvi + 1 < len(pv) and m9.uci() != pv[pvi + 1]:
            break                            # fresh checks must agree with
        chain.append((r, m9))                # the line's own answer
        normal += 1
        pvi += 2
        b = bb; b.push(m9)
    return chain


def main():
    engine = cengine.Engine()
    # FB-42: _board_phase (wdl display) and time_manager._phase_24 hand-type
    # the 1/1/2/4/24 taper weights. If PHASE_WEIGHTS is ever retuned, fail
    # loudly here instead of letting the wdl field and the moves-to-go guess
    # drift silently. Explicit raise, not assert: python -O must not strip it.
    _pw = engine._py.PHASE_WEIGHTS
    if ((_pw[chess.KNIGHT], _pw[chess.BISHOP], _pw[chess.ROOK],
         _pw[chess.QUEEN], engine._py.PHASE_MAX) != (1, 1, 2, 4, 24)):
        raise SystemExit(
            "PHASE_WEIGHTS retuned: update cuci._board_phase, "
            "cuci._WDL_PHASE_MAX and time_manager._phase_24 to match")
    # OpenBench CLI mode: `pygin bench [depth]` prints the node signature
    # and exits -- the OpenBench worker runs `./engine bench` (argv, not
    # UCI) to verify every build. Same run_bench as the UCI `bench` command
    # (FB-20/FB-32/FB-37 hygiene: book/tb/threads/closures all forced off).
    if len(sys.argv) > 1 and sys.argv[1] == "bench":
        run_bench(engine,
                  depth=int(sys.argv[2]) if len(sys.argv) > 2 else 11)
        return
    engine.pv_uci = True                     # UCI pv format
    engine.move_overhead_ms = 40             # FI-13b: UCI Move Overhead
    # P-26: shadow copies of the paired C-side tuning values (set_rfp and
    # set_null_move each set two values; UCI options arrive one at a time).
    # FB-06: PUSH them once so Python is authoritative -- if a C default ever
    # drifts, the first setoption would otherwise pair a stale shadow with it.
    engine.premove_on = False                # PM-01 (opt-in)
    engine._rfp_margin, engine._rfp_depth = 80, 6
    engine._null_base, engine._null_div = 2, 6
    engine._lib.set_rfp(engine._rfp_margin, engine._rfp_depth)
    engine._lib.set_null_move(engine._null_base, engine._null_div)
    board = chess.Board()
    search_thread = None
    pending_hash_mb = None                   # FB-25: Hash sent mid-search
    engine.show_wdl = True                   # FI-45: UCI_ShowWDL default
    dbg = {"on": False}                      # FI-45: `debug on` channels
    hf_ring = []                             # FI-45: hashfull trajectory

    def searching():
        return search_thread is not None and search_thread.is_alive()

    def go(tokens):
        # Host-clears rule (engine.py P-05, now mirrored by cengine): _abort
        # is set by engine.stop() and only ever cleared HERE, before the next
        # search starts -- so a stop that raced the previous search thread's
        # startup can never leak into (or get erased by) this one.
        engine._abort = False
        engine._go_pending = True            # FB-21: a stop arriving before
                                             # the search thread starts is
                                             # LIVE, not stale
        params = {}
        # FI-45: `searchmoves m1 m2 ...` -- collect the whitelist (tokens up
        # to the next keyword), strip it, invert to the C exclusion list at
        # search time (the MultiPV root_exclude_* infra, g_rx now 256-wide).
        if "searchmoves" in tokens:
            kw = {"wtime", "btime", "winc", "binc", "movestogo", "movetime",
                  "depth", "nodes", "mate", "infinite", "ponder"}
            i = tokens.index("searchmoves")
            j = i + 1
            while j < len(tokens) and tokens[j] not in kw:
                j += 1
            params["searchmoves"] = tokens[i + 1:j]
            tokens = tokens[:i] + tokens[j:]
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
        if "mate" in params and "depth" not in params:   # FB-25: `go mate N`
            max_depth = min(60, 2 * max(1, params["mate"]))
        # FB-09: honor `go nodes N` (deterministic testing / OpenBench);
        # None = unlimited. Applied per-go, cleared after.
        engine.node_limit = (max(1, params["nodes"])
                             if "nodes" in params else None)   # FB-25: 0 -> 1
        if engine.node_limit and engine.smp_workers > 1:
            # FB-25: the C budget counts MAIN-thread nodes only -- helpers
            # would make the reported total blow past the limit, and
            # node-limited runs exist for determinism anyway.
            engine._lib.set_threads(1)
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
                                  "nodes", "mate"))   # FB-25: mate reports
        stop_evt = threading.Event()
        holding = threading.Event()          # FB-14: search DONE, only holding

        white_to_move = board.turn == chess.WHITE
        # FB-13d: snapshot the position NOW -- a `position` command racing
        # the thread's startup must not change what gets searched. on_depth
        # reads the SNAPSHOT too: it derives the WDL phase from the board, and
        # main's live `board` can be reassigned by a `position` command while
        # the search is still streaming info lines (same race FB-13d closed
        # for the search itself).
        search_board = board.copy()
        prev_nodes = [0]                     # FI-45: per-go EBF tracking
        def on_depth(rec):
            if rec.get("book"):
                out(f"info string book move {rec['move']}")
            elif rec.get("tb"):
                out(f"info string tablebase move {rec['move']} wdl {rec['wdl']}")
            out(info_line(rec, white_to_move, engine, board=search_board))
            if dbg["on"]:                    # FI-45: `debug on` observability
                n = rec.get("nodes", 0)
                if prev_nodes[0] > 0 and n > prev_nodes[0]:
                    out(f"info string ebf={n / prev_nodes[0]:.2f}"
                        f" depth={rec.get('depth', 0)}")
                prev_nodes[0] = n
        engine.on_depth = on_depth
        engine.on_final = None               # final info == last depth line

        def run():
            # FB-02: an unhandled exception here used to kill the thread
            # silently -- no bestmove EVER = the host hangs the whole slot.
            # Always emit a bestmove; 0000 on error (arbiter-visible, not
            # a hang).
            mv = None
            sm_active = False
            mpv_book = None
            try:
                # FI-45: searchmoves -> exclude every legal move NOT listed
                # (root TT store + FI-06 recorder auto-suppressed while the
                # exclusion list is non-empty, per the MultiPV design).
                if params.get("searchmoves"):
                    want = set()
                    for u in params["searchmoves"]:
                        try:
                            m = chess.Move.from_uci(u)
                            if m in search_board.legal_moves:
                                want.add(m)
                        except ValueError:
                            pass
                    if want:
                        engine._lib.root_exclude_clear()
                        for m in search_board.legal_moves:
                            if m not in want:
                                engine._lib.root_exclude_add(
                                    m.from_square | (m.to_square << 6)
                                    | ((m.promotion or 0) << 12))
                        sm_active = True
                # MultiPV > 1 = analysis: bypass the opening book for the
                # MAIN search too -- a book hit returns a bare bestmove with
                # no PV, so every book position would show ZERO lines in the
                # GUI (the gate below needs a real search). =1 keeps the
                # book path byte-identical (match play never sets MultiPV).
                if getattr(engine, "multipv", 1) > 1 and engine.use_book:
                    mpv_book, engine.use_book = engine.use_book, False
                if budget is None:
                    mv = engine.get_best_move(search_board, max_depth)
                else:
                    mv = engine.get_best_move_timed(search_board, budget,
                                                    max_depth)
                # MultiPV: extra lines only when the option is >1 AND a real
                # search ran (book/tb hits and stopped searches are skipped);
                # =1 is byte-identical to the pre-MultiPV path.
                if (getattr(engine, "multipv", 1) > 1 and mv is not None
                        and not sm_active
                        and engine.last_pv and not stop_evt.is_set()
                        and not engine._abort):
                    _emit_multipv(engine, search_board, mv, engine.multipv,
                                  budget, white_to_move, stop_evt)
            except Exception as ex:
                print(f"cuci: search error: {ex!r}", file=sys.stderr)
            finally:
                if mpv_book is not None:     # restore the book setting the
                    engine.use_book = mpv_book   # MultiPV bypass overrode
                if sm_active:                # FI-45: NEVER leak exclusions
                    engine._lib.root_exclude_clear()
                engine.node_limit = None     # FB-09: per-go, don't leak
                holding.set()                # FB-14/FI-27: set BEFORE the
                                             # hold-wait AND as early as the
                                             # search result exists -- a go
                                             # arriving in the gap is handed
                                             # off, not dropped. Release
                if hold:                     # is instant -- no search running
                    stop_evt.wait()          # B-03: hold until `stop`
                bm_str = mv.uci() if mv is not None else "0000"
                pv = (engine.last_pv or "").split()
                if mv is not None and len(pv) >= 2 and pv[0] == bm_str:
                    out(f"bestmove {bm_str} ponder {pv[1]}")   # FI-45: GUIs
                else:                        # display it; real go-ponder
                    out(f"bestmove {bm_str}")  # semantics stay FI-13e
                if ("mate" in params and mv is not None
                        and abs(engine.last_score) < engine.MATE_THRESHOLD):
                    out(f"info string no mate found in <={params['mate']}")
                hf_ring.append(engine._lib.cs_hashfull())   # FI-45: per-move
                del hf_ring[:-64]            # trajectory, dumped on quit
                # PM-01: certified premoves, computed on the OPPONENT'S clock
                # (we are idle after bestmove). Not for held searches (a new
                # position is coming) or after a stop.
                if engine.premove_on and not hold:
                  try:                       # pygin-end ALWAYS follows (the
                    if mv is None or stop_evt.is_set():   # bridge's collector
                        raise StopIteration  # needs a terminator either way)
                    _sv = (engine.last_score, engine.last_pv,
                           engine.last_depth, engine.nodes_searched,
                           engine.use_book, engine.smp_workers,
                           engine.on_depth, engine.on_final)
                    try:
                        engine.on_depth = engine.on_final = None
                        engine.use_book = False   # cert needs real scores
                        engine.smp_workers = 1    # ms-scale probes: no SMP
                        pairs = certify_premoves(
                            engine, search_board, mv, stop_evt)
                        for r, m in pairs:
                            out(f"info string pygin-reply {r.uci()} {m.uci()}")
                    except Exception as ex:
                        print(f"cuci: premove cert error: {ex!r}",
                              file=sys.stderr)
                    finally:
                        (engine.last_score, engine.last_pv,
                         engine.last_depth, engine.nodes_searched,
                         engine.use_book, engine.smp_workers,
                         engine.on_depth, engine.on_final) = _sv
                  except StopIteration:
                    pass
                  finally:
                    out("info string pygin-end")

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
                out("option name Threads type spin default 1 min 1 max 256")
                out("option name MultiPV type spin default 1 min 1 max 5")
                out("option name OwnBook type check default true")
                out("option name BookFile type string default <empty>")
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
                out("option name Premove type check default false")
                out("option name UCI_ShowWDL type check default true")
                out("option name Clear Hash type button")
                out("option name Contempt type spin default 50 min -100 max 100")
                out("option name Move Overhead type spin default 40 min 0 max 5000")
                out("option name Hash type spin default 192 min 2 max 6144")
                # FI-13d: self-identifying config line (A/B forensics: PGN
                # headers grep this to know exactly what was playing).
                out(f"info string abi={engine._lib.csearch_abi()}"
                    f" pv_exact={int(engine.PV_EXACT)}"
                    f" check_ext_budget={engine.CHECK_EXT_BUDGET}"
                    f" outpost={int(engine.USE_OUTPOST)}"
                    f" score_hygiene={int(engine.SCORE_HYGIENE)}"
                    f" simplify={int(engine.USE_SIMPLIFY)}"
                    f" ep_filter={int(engine.EP_FILTER)}"
                    f" cb2={int(engine.CB2)}"
                    f" cantwin={int(engine.CANTWIN)}"
                    f" null_verify={int(engine.NULL_VERIFY)}"
                    f" lmr_hist={engine.LMR_HIST}"
                    f" tt_eval_sharpen={int(engine.TT_EVAL_SHARPEN)}"
                    f" see_prune={int(engine.SEE_PRUNE)}"
                    f" root_order={int(engine.ROOT_ORDER)}"
                    f" qs_evict_max={engine.QS_EVICT_MAX}"
                    f" hist_prune={engine.HIST_PRUNE}"
                    f" qs_tt_sharpen={int(engine.QS_TT_SHARPEN)}"
                    f" qs_keep_move={int(engine.QS_KEEP_MOVE)}"
                    f" cycle={int(engine.CYCLE_DETECT)}"
                    f" qs_beta_narrow={int(engine.QS_BETA_NARROW)}"
                    f" qs_ttm_exempt={int(engine.QS_TTM_EXEMPT)}"
                    f" qs_chk_d1={int(engine.QS_CHK_D1)}"
                    f" tt_keep_exact={engine.TT_KEEP_EXACT}"
                    f" tt_fh_tight={int(engine.TT_FH_TIGHT)}"
                    f" tt_r50={int(engine.TT_R50)}"
                    f" term_store={int(engine.TERM_STORE)}"
                    f" tt_mate_cut={int(engine.TT_MATE_CUT)}"
                    f" root_lmr={int(engine.ROOT_LMR)}"
                    f" use_nnue={int(engine.USE_NNUE)}"
                    f" king_shelter={int(engine.USE_KING_SHELTER)}"
                    f" tt_keep_warm={int(engine.TT_KEEP_WARM)}"
                    f" simplify_threshold={engine.SIMPLIFY_THRESHOLD}"
                    f" contempt={engine.contempt}"
                    f" hash_bits={engine.TT_BITS}"
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
                    engine.smp_workers = max(1, min(256, int(value)))
                elif name == "multipv":
                    engine.multipv = max(1, min(5, int(value)))
                elif name == "ownbook":
                    engine.use_book = value.lower() == "true"
                elif name == "bookfile":
                    # Point at a custom Polyglot .bin; empty/<empty> restores
                    # the auto-discovered bundled book (Perfect2023.bin ...).
                    engine.book_path = None if value in ("", "<empty>") else value
                elif name == "usetb":
                    engine.use_tb = value.lower() == "true"   # online Lichess
                                                              # Syzygy; no path
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
                elif name == "premove":                 # PM-01
                    engine.premove_on = value.lower() == "true"
                elif name == "uci_showwdl":             # FI-45: some arenas
                    engine.show_wdl = value.lower() == "true"   # reject
                                                        # unknown info fields
                elif name == "clearhash":               # FI-45: standard GUI
                    if not searching():                 # button; ignored
                        engine._lib.cs_tt_reset()       # mid-search
                elif name == "contempt":                # FI-45: C support
                    engine.contempt = max(-100, min(100, int(value)))
                    engine._lib.csearch_set_draw(       # FB-34: margin stays
                        engine.contempt,                # authoritative in
                        engine._py.DRAW_AVOID_MARGIN)   # engine.py -- never
                                                        # hardcode 200 here
                elif name == "moveoverhead":            # FI-13b
                    engine.move_overhead_ms = max(0, int(value))
                elif name == "hash":                    # FI-10: MB -> bits
                    if not searching():                 # resize = realloc;
                        mb = max(2, min(6144, int(value)))   # never mid-search
                        entries = mb * 1024 * 1024 // 24
                        bits = entries.bit_length() - 1
                        engine._lib.set_tt_bits(bits)
                        engine.TT_BITS = bits   # FB-30: fingerprint honesty
                        pending_hash_mb = None
                    else:                               # FB-25: defer, don't
                        pending_hash_mb = int(value)    # silently drop
            elif cmd == "bench":                        # FI-13c: OpenBench
                if not searching():                     # FB-32: depth arg
                    d = (int(tokens[1]) if len(tokens) > 1
                         and tokens[1].isdigit() else 11)
                    run_bench(engine, depth=d)
            elif cmd == "ucinewgame":
                if searching():
                    engine.stop()
                    # FB-01: a HELD search (go infinite / bare go) blocks on
                    # stop_evt after unwinding -- joining without releasing
                    # it deadlocked the whole host on GUIs that send
                    # ucinewgame mid-analysis.
                    search_thread.stop_evt.set()
                    search_thread.join()
                if pending_hash_mb is not None:      # FB-32: resize BEFORE the
                    mb = max(2, min(6144, pending_hash_mb))   # reset, so the
                    entries = mb * 1024 * 1024 // 24  # new game starts at the
                    bits = entries.bit_length() - 1   # requested size
                    engine._lib.set_tt_bits(bits)
                    engine.TT_BITS = bits
                    pending_hash_mb = None
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
                    # FB-36: trust-boundary gate -- python-chess parses
                    # structurally-legal-but-illegal FENs (opponent in check,
                    # missing king); csearch.c ctzll's the king bitboard, so
                    # a kingless board is UB. Reject via the existing
                    # all-or-nothing path; nb.status() names the reason.
                    if not nb.is_valid():
                        raise ValueError(f"invalid position: {nb.status()!r}")
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
                        engine.stop()    # FB-32: a PM-01 certification
                                         # sub-search polls stop_evt only
                                         # BETWEEN searches -- abort the
                                         # in-flight one or this join blocks
                                         # up to a full d9/d10 search. The
                                         # stray _abort is cleared by go()
                                         # before the next search (FB-21).
                        search_thread.join()
                    else:
                        continue             # actively searching; ignore
                if pending_hash_mb is not None:      # FB-25/FB-35: apply the
                    mb = max(2, min(6144, pending_hash_mb))   # deferred Hash
                    entries = mb * 1024 * 1024 // 24  # AFTER the holding
                    bits = entries.bit_length() - 1   # release above --
                    engine._lib.set_tt_bits(bits)     # idle is guaranteed
                    engine.TT_BITS = bits    # FB-30   # here (joined or
                    pending_hash_mb = None            # bailed), so the FB-32
                                                      # next-go promise holds
                                                      # on the FB-14 path too
                search_thread = go(tokens[1:])
                search_thread.start()
            elif cmd == "stop":
                if searching():
                    engine.stop()
                    search_thread.stop_evt.set()   # B-03: release the hold
                    search_thread.join(timeout=30)   # FI-27: a wedged C
                    if search_thread.is_alive():     # search must not brick
                        print("cuci: search thread failed to stop in 30s",
                              file=sys.stderr)       # the whole host slot
            elif cmd == "debug":                 # FI-45: spec conformance;
                dbg["on"] = (len(tokens) > 1     # gates the ebf channel
                             and tokens[1] == "on")
            elif cmd == "ponderhit":
                pass                     # FB-32: no ponder search yet
                                         # (FI-13e); accepting the standard
                                         # command kills the GUI error spam
            elif cmd == "quit":
                if searching():
                    engine.stop()
                    search_thread.stop_evt.set()
                    search_thread.join()
                if hf_ring:                  # FI-45: saturation evidence for
                    out("info string hashfull trajectory (last "
                        f"{len(hf_ring)} moves): "
                        + " ".join(str(v) for v in hf_ring))   # the FI-20 gate
                break
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            out(f"info string error: {err}")


if __name__ == "__main__":
    main()
