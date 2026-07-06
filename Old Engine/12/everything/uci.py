"""
uci.py
======
A UCI-protocol wrapper around this project's ``Engine`` (engine.py), so the
engine can be driven by any UCI GUI (Cute Chess, Arena, Banksia) and -- the
point of this file -- played on Lichess through `lichess-bot
<https://github.com/lichess-bot-devs/lichess-bot>`_, which bridges a local UCI
engine to the Lichess Bot API.

Run it directly as the engine binary::

    python3 uci.py          # CPython
    pypy3   uci.py          # PyPy -> ~1.5x faster, recommended for play

In lichess-bot's ``config.yml`` point ``engine.dir`` / ``engine.name`` at the
``claudechess`` launcher (which runs this file under PyPy when available) and
set ``protocol: uci``.

Supported commands
------------------
``uci`` ``isready`` ``ucinewgame`` ``setoption`` ``position`` ``go`` ``stop``
``quit``. ``go`` understands ``wtime``/``btime``/``winc``/``binc``/``movetime``/
``depth``/``infinite`` (and ignores ``nodes``/``mate``/``ponder``). When a clock
is given the per-move budget comes from ``time_manager.calculate_move_time``;
``movetime`` and ``depth`` are honoured literally; ``infinite`` searches under a
large budget so ``stop`` stays responsive.

Design notes
------------
* The search runs on a worker thread so ``stop``/``isready``/``quit`` stay
  responsive while thinking. All stdout writes are serialised under a lock.
* ``stop`` aborts without touching engine.py: it pokes ``engine.time_limit`` /
  ``engine.start_time`` so the next ``_check_time`` poll (every 1024 nodes)
  trips the engine's own ``_TimeUp`` path, which returns the best move so far.
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


class UCIEngine:
    def __init__(self):
        self.engine = Engine()
        self.engine.pv_uci = True              # emit the PV in UCI (g1f3) form
        self.board = chess.Board()
        self._out_lock = threading.Lock()
        self._search_thread = None
        self._searching = False

        # Configurable UCI options (see _set_option / the `uci` advertisement).
        self.move_overhead_ms = DEFAULT_MOVE_OVERHEAD_MS
        self.hash_mb = self._default_hash_mb()
        self._apply_options()

    # ------------------------------------------------------------------ #
    # Output helpers (serialised so search-thread info lines never tear).
    # ------------------------------------------------------------------ #
    def _send(self, line):
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

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
            #   * SyzygyPath / UCI_ShowWDL -- declared and accepted but ignored
            #     (no tablebase or WDL support here).
            self._send(f"option name Hash type spin default {self.hash_mb} "
                       f"min 1 max 4096")
            self._send(f"option name Move Overhead type spin "
                       f"default {DEFAULT_MOVE_OVERHEAD_MS} min 0 max 5000")
            self._send("option name Threads type spin default 1 min 1 max 1024")
            self._send("option name SyzygyPath type string default <empty>")
            self._send("option name UCI_ShowWDL type check default false")
            self._send("uciok")
        elif cmd == "isready":
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
            return False
        # Unknown commands are ignored, per the UCI spec.
        return True

    # ------------------------------------------------------------------ #
    def _new_game(self):
        # Fresh engine => clears the transposition table and all search state
        # carried over from the previous game. Re-apply the configured options,
        # since a new Engine() reverts to its class defaults.
        self._stop()
        self.engine = Engine()
        self.engine.pv_uci = True
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
        # Threads / SyzygyPath / UCI_ShowWDL / anything else: accepted, no-op.

    def _apply_options(self):
        """Push option values onto the live engine instance."""
        entries = max(1, self.hash_mb * 1024 * 1024 // BYTES_PER_TT_ENTRY)
        self.engine.TT_MAX_ENTRIES = entries

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

        movetime = depth = None
        infinite = False
        wtime = btime = winc = binc = None
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
            else:
                i += 1                         # skip unknown / unsupported token

        white_to_move = self.board.turn == chess.WHITE

        # Decide the search mode: fixed depth, fixed movetime, clock budget, or
        # an "infinite" search under a large budget so `stop` can end it.
        if depth is not None and movetime is None and not infinite \
                and wtime is None and btime is None:
            mode, budget_ms, go_depth = "depth", None, depth
        elif movetime is not None:
            mode, budget_ms, go_depth = "time", movetime, MAX_DEPTH_CAP
        elif wtime is not None or btime is not None:
            my_ms = (wtime if white_to_move else btime) or 0
            opp_ms = (btime if white_to_move else wtime) or 0
            inc_ms = ((winc if white_to_move else binc) or 0)
            budget_ms = calculate_move_time(self.board, my_ms, opp_ms, inc_ms,
                                            overhead_ms=self.move_overhead_ms)
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

        line = (f"info depth {depth} score {score} nodes {nodes} "
                f"nps {nps} time {time_ms}")
        if pv:
            line += f" pv {pv}"
        self._send(line)

    def _stop(self):
        """Abort a running search (if any) and wait for its bestmove."""
        if not self._searching:
            return
        # Force the time budget to zero so the next _check_time poll trips the
        # engine's own _TimeUp path -> the search returns the best move so far.
        # NB: only time_limit is zeroed, NOT start_time -- the abort condition
        # `(now - start_time) >= 0` still fires immediately, while leaving
        # start_time intact keeps the time_ms of any in-flight info line correct.
        self.engine.time_limit = 0.0
        t = self._search_thread
        if t is not None:
            t.join(timeout=10.0)
        self._searching = False


def main():
    uci = UCIEngine()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if not uci.handle(line):
            break


if __name__ == "__main__":
    main()
