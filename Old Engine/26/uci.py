"""
uci.py
======
A UCI-protocol wrapper around this project's ``Engine`` (engine.py), so the
engine can be driven by any UCI GUI (Cute Chess, Arena, Banksia) and -- the
point of this file -- played on Lichess through `lichess-bot
<https://github.com/lichess-bot-devs/lichess-bot>`_, which bridges a local UCI
engine to the Lichess Bot API.

Run it directly as the engine binary::

    python3 uci.py          # CPython -- the production configuration
    pypy3   uci.py          # works too, but post-C-ports warmed PyPy is only
                            # ~+25% (see engine.py's "NB on PyPy"); not used
                            # in production

In lichess-bot's ``config.yml`` point ``engine.dir`` / ``engine.name`` at the
``claudechess`` launcher (which runs this file under CPython -- ``exec
python3 uci.py``; run_engine.sh is the separate PyPy launcher) and set
``protocol: uci``.

Supported commands
------------------
``uci`` ``isready`` ``ucinewgame`` ``setoption`` ``position`` ``go`` ``stop``
``quit``. ``go`` understands ``wtime``/``btime``/``winc``/``binc``/``movestogo``/
``movetime``/``depth``/``infinite`` (and ignores ``nodes``/``mate``/``ponder``/
``searchmoves``). When a clock is given the per-move budget comes from
``time_manager.calculate_move_time``, which uses ``movestogo`` verbatim (as
ground truth from the GUI/arbiter) in place of its own phase-based guess when
supplied -- this matters most under classical (non-increment) controls, where
the guess has no way to know a time jump is a few moves away. ``movetime`` and
``depth`` are honoured literally; ``infinite`` searches under a large budget so
``stop`` stays responsive.

Design notes
------------
* The search runs on a worker thread so ``stop``/``isready``/``quit`` stay
  responsive while thinking. All stdout writes are serialised under a lock.
* ``stop`` aborts without touching engine.py internals: it sets the engine's
  ``_abort`` flag (and zeroes ``time_limit``) so the next ``_check_time`` poll
  (every 1024-4096 nodes, budget-dependent) trips the engine's own ``_TimeUp``
  path, which returns the best move so far.
* The engine reports scores from White's POV; UCI wants them from the side to
  move, so ``info`` lines flip the sign for Black (and convert mate distances),
  exactly like battle_worker.py does.
"""

import sys
import threading
import time

import chess

from engine import Engine
from time_manager import calculate_move_time

ENGINE_NAME = "ClaudeChess"
ENGINE_AUTHOR = "sam"

# Cap on iterative-deepening depth for timed/infinite searches (the clock, not
# this, is the real limit; it only bounds a search given an enormous budget).
MAX_DEPTH_CAP = 40
# Budget handed to a `go infinite` search so `stop` always has something to
# interrupt and we never hang forever (10 minutes is far past any real use).
INFINITE_BUDGET_MS = 600_000

# Rough bytes per transposition-table entry (a dict slot + the 6-tuple value),
# used to translate the UCI `Hash` option (megabytes) into an entry cap. This is
# a CPython estimate; PyPy entries are smaller, so the real memory use sits at or
# below the requested Hash. Approximate by design -- the TT is a Python dict, not
# a fixed-size table.
BYTES_PER_TT_ENTRY = 150
DEFAULT_MOVE_OVERHEAD_MS = 40       # matches time_manager.MOVE_OVERHEAD_MS

# Eval parameters exposed as UCI options so chess-tuning-tools (or any GUI)
# can override them via setoption.  Format: name -> (min, max, default).
# Defaults match engine_tuned.py after the last tuning run.
TUNABLE_EVAL = {
    # scalars
    "ROOK_OPEN_FILE":      (10,  45,  17),
    "ROOK_SEMIOPEN_FILE":  ( 3,  25,  11),
    "TEMPO":               ( 2,  25,  20),
    "DOUBLED_PAWN":        ( 5,  35,  13),
    "ISOLATED_PAWN":       ( 3,  28,  10),
    "BACKWARD_PAWN":       ( 2,  22,  10),
    "BISHOP_PAIR_MG":      (10,  55,  32),
    "BISHOP_PAIR_EG":      (10,  55,  55),
    "KING_RING_ATTACK_MG": ( 2,  20,  13),
    "KING_RING_ATTACK_EG": ( 0,  12,   0),
    "KING_SHIELD_MG":      ( 2,  22,   5),
    "KING_SHIELD_EG":      ( 0,  10,   2),
    "KING_OPEN_FILE_MG":   ( 8,  40,  28),
    "KING_OPEN_FILE_EG":   ( 2,  22,   2),
    # material (MG then EG, named MG_Pawn etc. to match common convention)
    "MG_Pawn":             ( 60, 130,  89),
    "MG_Knight":           (290, 400, 353),
    "MG_Bishop":           (320, 430, 356),
    "MG_Rook":             (430, 570, 489),
    "MG_Queen":            (900,1150,1148),
    "EG_Pawn":             ( 70, 130, 108),
    "EG_Knight":           (245, 340, 335),
    "EG_Bishop":           (255, 350, 328),
    "EG_Rook":             (460, 570, 570),
    "EG_Queen":            (860,1020,1020),
}

_PIECE_FROM_NAME = {
    "pawn": chess.PAWN, "knight": chess.KNIGHT, "bishop": chess.BISHOP,
    "rook": chess.ROOK, "queen": chess.QUEEN,
}


def _phase24(board):
    """Tapered game phase 0..24, mirroring engine.py's PHASE_WEIGHTS/PHASE_MAX
    (knights+bishops 1, rooks 2, queens 4) -- input to the WDL model."""
    npm = (chess.popcount(board.knights | board.bishops)
           + 2 * chess.popcount(board.rooks)
           + 4 * chess.popcount(board.queens))
    return min(24, npm)


def _load_wdl():
    """(cp, phase) -> (win, draw, loss) permille, from the coefficients that
    fit_wdl_model.py wrote to wdl_model.json. Returns None while the model
    file doesn't exist (UCI_ShowWDL then stays a silent no-op). cp is from
    the side to move's point of view, matching the UCI wdl convention."""
    import json
    import math
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "wdl_model.json")
    try:
        with open(path, encoding="utf-8") as f:
            mod = json.load(f)
        AS, BS = mod["as"], mod["bs"]
        pmax, pmin = mod["phase_max"], mod["phase_clamp_min"]
    except (OSError, ValueError, KeyError):
        return None

    def win_rate(cp, phase):
        x = min(max(phase, pmin), pmax) / pmax
        a = ((AS[0] * x + AS[1]) * x + AS[2]) * x + AS[3]
        b = ((BS[0] * x + BS[1]) * x + BS[2]) * x + BS[3]
        return 1.0 / (1.0 + math.exp((a - cp) / b))

    def wdl(cp, phase):
        w = win_rate(cp, phase)
        l = win_rate(-cp, phase)
        d = max(0.0, 1.0 - w - l)
        win, draw, loss = round(w * 1000), round(d * 1000), round(l * 1000)
        drift = 1000 - (win + draw + loss)   # rounding can miss 1000 by +/-1
        if drift:
            biggest = max((win, 0), (draw, 1), (loss, 2))[1]
            if biggest == 0:
                win += drift
            elif biggest == 1:
                draw += drift
            else:
                loss += drift
        return win, draw, loss

    return wdl


class UCIEngine:
    def __init__(self):
        self.engine = Engine()
        self.engine.use_book = False
        self.engine.use_tb = True
        self.engine.pv_uci = True              # emit the PV in UCI (g1f3) form
        self.board = chess.Board()
        self._out_lock = threading.Lock()
        self._search_thread = None
        self._searching = False

        # Configurable UCI options (see _set_option / the `uci` advertisement).
        self.move_overhead_ms = DEFAULT_MOVE_OVERHEAD_MS
        self.hash_mb = self._default_hash_mb()
        self._eval_overrides = {}   # name -> int, applied each _apply_options
        # P-01: worker count for the SMP pool. Defaults to the engine's own
        # configured SMP width (SMP_WORKERS / CLAUDECHESS_SMP) so production
        # UCI play finally gets the multi-core search the benchmarks used.
        self.threads = max(1, self.engine.smp_workers)
        self.show_wdl = False       # UCI_ShowWDL; needs wdl_model.json to act
        self._wdl = None            # lazy: loaded when the option is enabled
        self._apply_options()

    # ------------------------------------------------------------------ #
    # Output helpers (serialised so search-thread info lines never tear).
    # ------------------------------------------------------------------ #
    def _send(self, line):
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            import os
            if os.environ.get("UCI_DEBUG"):
                sys.stderr.write(f">> {line}\n")
                sys.stderr.flush()

    # ------------------------------------------------------------------ #
    # Command dispatch (runs on the main / stdin thread).
    # ------------------------------------------------------------------ #
    def handle(self, line):
        parts = line.split()
        if not parts:
            return True
        cmd = parts[0]

        if cmd == "uci":
            self._send(f"id name {ENGINE_NAME}")
            self._send(f"id author {ENGINE_AUTHOR}")
            # Hash sizes the transposition table; Move Overhead is the clock
            # safety margin -- both are honored. The rest are advertised only so
            # that python-chess (which lichess-bot uses) accepts a config that
            # sets them: it REFUSES to send any option the engine didn't declare,
            # and it validates spin values against the advertised max. So:
            #   * Threads -- generous max so `Threads: N` validates; ignored
            #     (the engine is single-threaded).
            #   * UCI_ShowWDL -- honoured when wdl_model.json exists (fitted
            #     by fit_wdl_model.py); silently a no-op otherwise.
            #   * SyzygyPath -- declared and accepted but ignored (the engine
            #     probes the Lichess tablebase API instead, engine.use_tb).
            self._send(f"option name Hash type spin default {self.hash_mb} "
                       f"min 1 max 4096")
            self._send(f"option name Move Overhead type spin "
                       f"default {DEFAULT_MOVE_OVERHEAD_MS} min 0 max 5000")
            self._send(f"option name Threads type spin default {self.threads} "
                       f"min 1 max 64")
            self._send("option name SyzygyPath type string default <empty>")
            self._send("option name UCI_ShowWDL type check default false")
            for oname, (lo, hi, default) in TUNABLE_EVAL.items():
                self._send(f"option name {oname} type spin "
                           f"default {default} min {lo} max {hi}")
            self._send("uciok")
        elif cmd == "isready":
            # Spawn the SMP pool here (stdin = main thread), so the multi-
            # second worker startup lands on `isready` -- which the GUI waits
            # on -- instead of eating the first move's clock.
            self._ensure_pool()
            self._send("readyok")
        elif cmd == "ucinewgame":
            self._new_game()
        elif cmd == "setoption":
            self._set_option(parts[1:])
        elif cmd == "position":
            self._set_position(parts[1:])
        elif cmd == "go":
            self._go(parts[1:])
        elif cmd == "stop":
            self._stop()
        elif cmd == "quit":
            self._stop()
            pool = getattr(self.engine, "_smp_pool", None)
            if pool is not None:
                pool.close()                 # unlink the 64MB shared TT
            return False
        # Unknown commands are ignored, per the UCI spec.
        return True

    # ------------------------------------------------------------------ #
    def _ensure_pool(self):
        """P-01: create the SMP worker pool on the stdin (main) thread.

        Searches run on a worker thread, where the engine's fork-bomb guard
        refuses pool CREATION (pool USE is fine) -- so before this fix, uci.py
        play silently fell back to single-threaded despite SMP_WORKERS=4.
        Called from `isready` (spawn cost lands there, off the clock) and from
        `go` as a fallback for hosts that skip isready."""
        if self.threads > 1 and getattr(self.engine, "_smp_pool", None) is None:
            try:
                from smp import SMPPool
                self.engine._smp_pool = SMPPool(self.threads)
            except Exception as e:
                print(f"[uci] WARNING: SMP pool unavailable ({e}); "
                      "running single-threaded", file=sys.stderr)
                self.engine._smp_pool = None

    def _new_game(self):
        # Fresh engine => clears the transposition table and all search state
        # carried over from the previous game. Re-apply the configured options,
        # since a new Engine() reverts to its class defaults.
        self._stop()
        # P-07: carry the SMP pool (4 processes + 64MB shared TT) across games
        # instead of leaking one per ucinewgame; its shared TT is zeroed to
        # mirror the fresh dict TT.
        pool = getattr(self.engine, "_smp_pool", None)
        self.engine = Engine()
        self.engine.pv_uci = True
        if pool is not None:
            pool.clear_tt()
            self.engine._smp_pool = pool
        self._apply_options()
        self.board = chess.Board()

    # ------------------------------------------------------------------ #
    # UCI options.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_hash_mb():
        """Hash size (MB) implied by the engine's default TT entry cap, so the
        advertised default reflects what the engine actually ships with."""
        cap = int(getattr(Engine, "TT_MAX_ENTRIES", 1_500_000))
        return max(1, cap * BYTES_PER_TT_ENTRY // (1024 * 1024))

    def _set_option(self, args):
        """Handle `setoption name <Name with spaces> [value <Val with spaces>]`."""
        if "name" not in args:
            return
        ni = args.index("name")
        if "value" in args:
            vi = args.index("value")
            name = " ".join(args[ni + 1:vi])
            value = " ".join(args[vi + 1:])
        else:
            name = " ".join(args[ni + 1:])
            value = ""
        key = name.strip().lower()

        if key == "hash":
            try:
                self.hash_mb = max(1, int(value))
            except ValueError:
                return
            self._apply_options()
        elif key == "move overhead":
            try:
                self.move_overhead_ms = max(0, int(value))
            except ValueError:
                return
        elif key == "threads":
            # P-01: wire the advertised option to the pool size (it used to be
            # accepted and ignored). A size change closes the current pool;
            # the next isready/go recreates it on the main thread.
            try:
                n = max(1, min(64, int(value)))
            except ValueError:
                return
            if n != self.threads:
                self.threads = n
                pool = getattr(self.engine, "_smp_pool", None)
                if pool is not None:
                    pool.close()
                    self.engine._smp_pool = None
        elif key == "uci_showwdl":
            self.show_wdl = str(value).strip().lower() in ("true", "1", "on", "yes")
            if self.show_wdl and self._wdl is None:
                self._wdl = _load_wdl()   # None (silent no-op) until the model
                                          # file exists -- see fit_wdl_model.py
        elif name in TUNABLE_EVAL:
            try:
                lo, hi, _ = TUNABLE_EVAL[name]
                self._eval_overrides[name] = max(lo, min(hi, int(value)))
            except ValueError:
                return
            self._apply_options()
        # SyzygyPath / UCI_ShowWDL / anything else: accepted, no-op.

    def _apply_options(self):
        """Push option values onto the live engine instance."""
        entries = max(1, self.hash_mb * 1024 * 1024 // BYTES_PER_TT_ENTRY)
        self.engine.TT_MAX_ENTRIES = entries
        for oname, val in self._eval_overrides.items():
            if oname.startswith("MG_") or oname.startswith("EG_"):
                phase, piece_name = oname.split("_", 1)
                pt = _PIECE_FROM_NAME.get(piece_name.lower())
                if pt is None:
                    continue
                if phase == "MG":
                    d = dict(self.engine.MG_VALUES); d[pt] = val
                    self.engine.MG_VALUES = d
                else:
                    d = dict(self.engine.EG_VALUES); d[pt] = val
                    self.engine.EG_VALUES = d
            else:
                setattr(self.engine, oname, val)
        # The C eval reads process-global statics synced only at Engine
        # construction, and the pawn-structure memo caches scores computed
        # with the old weights -- re-push and invalidate, or the eval writes
        # above are silently inert.
        self.engine._sync_c_params()
        self.engine._pawn_cache.clear()

    def _set_position(self, args):
        if not args:
            return
        idx = 0
        if args[0] == "startpos":
            self.board = chess.Board()
            idx = 1
        elif args[0] == "fen":
            fen = " ".join(args[1:7])          # the 6 FEN fields
            try:
                self.board = chess.Board(fen)
            except Exception:
                self.board = chess.Board()
            idx = 7
        else:
            return
        # Optional trailing "moves e2e4 e7e5 ...".
        if idx < len(args) and args[idx] == "moves":
            for uci in args[idx + 1:]:
                try:
                    move = chess.Move.from_uci(uci)
                except Exception:
                    break
                if move in self.board.legal_moves:
                    self.board.push(move)
                else:
                    break

    # ------------------------------------------------------------------ #
    # Search.
    # ------------------------------------------------------------------ #
    def _go(self, args):
        # Don't start a second search on top of a running one.
        self._stop()
        # Clear a leftover stop request. Safe against _stop: both run on the
        # stdin thread; the search thread only ever reads the flag.
        self.engine._abort = False
        # P-01: make sure the pool exists (normally created at isready; this
        # is the fallback for hosts that skip isready). Must happen HERE on
        # the stdin/main thread -- the search thread may USE the pool but the
        # engine's fork-bomb guard refuses to CREATE one off-main-thread,
        # which is exactly the bug that made production play single-threaded.
        self._ensure_pool()

        movetime = depth = None
        infinite = False
        wtime = btime = winc = binc = movestogo = None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "movetime" and i + 1 < len(args):
                movetime = int(args[i + 1]); i += 2
            elif a == "depth" and i + 1 < len(args):
                depth = int(args[i + 1]); i += 2
            elif a == "infinite":
                infinite = True; i += 1
            elif a == "wtime" and i + 1 < len(args):
                wtime = int(args[i + 1]); i += 2
            elif a == "btime" and i + 1 < len(args):
                btime = int(args[i + 1]); i += 2
            elif a == "winc" and i + 1 < len(args):
                winc = int(args[i + 1]); i += 2
            elif a == "binc" and i + 1 < len(args):
                binc = int(args[i + 1]); i += 2
            elif a == "movestogo" and i + 1 < len(args):
                movestogo = int(args[i + 1]); i += 2
            else:
                i += 1                         # skip unknown / unsupported token

        white_to_move = self.board.turn == chess.WHITE

        # Decide the search mode: fixed depth, fixed movetime, clock budget, or
        # an "infinite" search under a large budget so `stop` can end it.
        if depth is not None and movetime is None and not infinite \
                and wtime is None and btime is None:
            mode, budget_ms, go_depth = "depth", None, depth
        elif movetime is not None:
            # Subtract overhead so the engine finishes before cutechess-cli's deadline.
            mode, budget_ms, go_depth = "time", max(1, movetime - self.move_overhead_ms), MAX_DEPTH_CAP
        elif wtime is not None or btime is not None:
            my_ms = (wtime if white_to_move else btime) or 0
            opp_ms = (btime if white_to_move else wtime) or 0
            inc_ms = ((winc if white_to_move else binc) or 0)
            budget_ms = calculate_move_time(self.board, my_ms, opp_ms, inc_ms,
                                            overhead_ms=self.move_overhead_ms,
                                            movestogo=movestogo)
            mode, go_depth = "time", MAX_DEPTH_CAP
        else:
            # bare "go" or "go infinite": search under the large fallback budget.
            mode, budget_ms, go_depth = "time", INFINITE_BUDGET_MS, MAX_DEPTH_CAP

        self._searching = True
        self._search_thread = threading.Thread(
            target=self._run_search,
            args=(self.board.copy(), white_to_move, mode, budget_ms, go_depth),
            daemon=True,
        )
        self._search_thread.start()

    def _run_search(self, board, white_to_move, mode, budget_ms, go_depth):
        # Stream one UCI `info` line per completed iterative-deepening depth.
        def on_depth(rec):
            self._emit_info(rec, white_to_move)
        self.engine.on_depth = on_depth
        self.engine.on_final = None
        t0 = time.perf_counter()

        try:
            if mode == "depth":
                move = self.engine.get_best_move(board, go_depth)
            else:
                move = self.engine.get_best_move_timed(
                    board, budget_ms / 1000.0, go_depth)
        except Exception:
            move = None
        finally:
            self.engine.on_depth = None

        self._searching = False
        # Under the SMP pool the per-depth on_depth stream never fires (the
        # searching happens in worker processes), so emit one summary info
        # line from the aggregated pool result before bestmove.
        if (move is not None and mode != "depth"
                and getattr(self.engine, "_smp_pool", None) is not None):
            self._emit_info({
                "depth": self.engine.last_depth,
                "score": self.engine.last_score,
                "nodes": self.engine.nodes,
                "time_ms": int((time.perf_counter() - t0) * 1000),
                "pv": move.uci(),
            }, white_to_move)
        self._send(f"bestmove {move.uci() if move is not None else '0000'}")

    def _emit_info(self, rec, white_to_move):
        depth = rec.get("depth", 0)
        nodes = rec.get("nodes", 0)
        time_ms = rec.get("time_ms", 0)
        nps = int(nodes / (time_ms / 1000.0)) if time_ms > 0 else 0
        pv = rec.get("pv", "")

        # rec["score"] is centipawns from White's POV; UCI wants side-to-move.
        white_score = rec.get("score", 0)
        stm = white_score if white_to_move else -white_score
        if abs(stm) >= self.engine.MATE_THRESHOLD:
            plies = self.engine.MATE_SCORE - abs(stm)
            full = (plies + 1) // 2
            score = f"mate {full if stm > 0 else -full}"
        else:
            score = f"cp {stm}"

        # UCI_ShowWDL: per-mille win/draw/loss from the fitted model (see
        # fit_wdl_model.py / wdl_model.json). Mate scores are certainties.
        wdl_str = ""
        if self.show_wdl and self._wdl is not None:
            if abs(stm) >= self.engine.MATE_THRESHOLD:
                w, d, l = (1000, 0, 0) if stm > 0 else (0, 0, 1000)
            else:
                w, d, l = self._wdl(stm, _phase24(self.board))
            wdl_str = f" wdl {w} {d} {l}"

        line = (f"info depth {depth} score {score}{wdl_str} nodes {nodes} "
                f"nps {nps} time {time_ms}")
        if pv:
            line += f" pv {pv}"
        self._send(line)

    def _stop(self):
        """Abort a running search (if any) and wait for its bestmove."""
        if not self._searching:
            return
        # Two abort signals, covering both sides of the race with `go`:
        #  * `_abort` is never reset by the engine, only by _go before the next
        #    search -- so a stop landing BEFORE the search thread arms its
        #    clock (which overwrites time_limit) still aborts at the first
        #    _check_time poll instead of running the full budget.
        #  * zeroing time_limit trips an already-armed timed search at that
        #    same poll (kept from before; harmless overlap). start_time stays
        #    intact so in-flight info lines report correct time_ms.
        self.engine._abort = True
        self.engine.time_limit = 0.0
        # P-01: _abort lives in THIS process; the pool's workers are separate
        # processes and need the shared stop event instead.
        pool = getattr(self.engine, "_smp_pool", None)
        if pool is not None:
            pool.request_stop()
        t = self._search_thread
        if t is not None:
            t.join(timeout=10.0)
        self._searching = False


def main():
    import os
    _dbg = os.environ.get("UCI_DEBUG")
    def dbg(msg):
        if _dbg:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
    uci = UCIEngine()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        dbg(f"<< {line}")
        if not uci.handle(line):
            break


if __name__ == "__main__":
    main()
