"""
stockfish_engine.py
===================
A Stockfish opponent that exposes the same ``Engine`` API the project's runners
(``match.py`` / ``battle_worker.py``) expect, by driving the Stockfish binary
directly over UCI (no third-party package, so it works under CPython and PyPy).

Use it to get an ABSOLUTE strength estimate: play your engine against Stockfish
capped at a target Elo and see where it crosses 50%.

    STOCKFISH_ELO=1500 python3 match.py engine.py stockfish_engine.py
    STOCKFISH_ELO=1800 python3 match.py engine.py stockfish_engine.py
    ...bracket until ~50%; that's roughly your engine's rating.

Config (env vars or edit the defaults):
    STOCKFISH_ELO    target Elo, clamped to Stockfish's 1320..3190  (default 2500)
                     0 (or negative) = FULL STRENGTH, no limit (for odds matches)
    STOCKFISH_SKILL  skill level 0..20 INSTEAD of Elo, if set (weaker than 1320)
    STOCKFISH_PATH   path to the binary (else auto-detected)
"""

import os
import subprocess

import chess

_SF_PATHS = [
    os.environ.get("STOCKFISH_PATH", ""),
    "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish",
    "/usr/games/stockfish", "/usr/bin/stockfish", "stockfish",
]
# Plain defaults -- no environment reads. Hosts that need a different value
# set the module global before constructing (match.py --sf-elo does exactly
# that, in the engine child, before _load_engine runs).
SF_ELO = 3000          # UCI_Elo cap; <= 0 means full strength.
                       # 3000, not the old 2900: v58 scored 65.47% over 333
                       # games at 50+0.50 against the 2900 cap, which puts the
                       # engine ~111 points above it. A cap that far below the
                       # engine spends most of its games already decided, so
                       # the score saturates and stops resolving changes.
                       # ~3000 is near parity at v58 and is where a fixed-cap
                       # yardstick has the most resolution. Raise it again once
                       # the score against this cap clears ~65%.
SF_SKILL = None        # if set, used instead of Elo
SF_THREADS = 1
SF_HASH = 64


def _find_sf():
    import shutil
    for p in _SF_PATHS:
        if p and (os.path.isfile(p) or shutil.which(p)):
            return p if os.path.isfile(p) else shutil.which(p)
    raise RuntimeError("stockfish binary not found (set STOCKFISH_PATH)")


class Engine:
    """UCI-driven Stockfish, wearing the project's Engine interface."""

    MATE_SCORE = 1_000_000
    MATE_THRESHOLD = MATE_SCORE - 1_000

    def __init__(self):
        # Attributes the runners read.
        self.use_book = False
        self.pv_uci = True
        self.nodes_searched = 0
        self.last_score = 0          # WHITE's-perspective centipawns
        self.last_wdl = None         # (w, d, l) permille, SIDE-TO-MOVE POV as
                                     # SF prints it -- battle_worker forwards
                                     # it raw, matching score_cp's stm POV
        self.last_depth = 0
        self.last_pv = ""

        self._spawn()

    def _spawn(self):
        self._proc = subprocess.Popen(
            [_find_sf()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._uci_handshake()

    def _ensure_alive(self):
        """Respawn a dead Stockfish rather than poisoning the whole run.

        SF-18 has segfaulted inside its own NNUE eval on this Mac (identical
        crash signature on 2026-08-06 and 2026-08-12, macOS DiagnosticReports,
        long before any harness change) -- rare, upstream, not ours to fix.
        What IS ours: odds.py reuses one SF process per worker for the whole
        campaign, so without this a single segfault turned every remaining
        game in that worker into an instant error -- 950 of 1,000 games died
        that way on 2026-08-12. A respawn costs the crashed game only. The
        wrapper is stateless per move (full FEN each `go`), so a fresh
        process with a fresh handshake is a correct continuation; only SF's
        hash is lost."""
        if self._proc.poll() is not None:
            self._spawn()

    # -- UCI plumbing ------------------------------------------------- #
    def _send(self, cmd):
        self._proc.stdin.write(cmd + "\n")
        self._proc.stdin.flush()

    def _read_until(self, token):
        """Return all lines up to and incl. the one starting with ``token``."""
        lines = []
        for line in self._proc.stdout:        # blocking readline loop (no select)
            line = line.rstrip("\n")
            lines.append(line)
            if line.split(" ", 1)[0] == token or line.startswith(token):
                break
        return lines

    def _uci_handshake(self):
        self._send("uci")
        opts = self._read_until("uciok")
        # SF >= 12 publishes its own WDL model over UCI. When advertised,
        # every info line carries "wdl W D L" (permille, side-to-move POV)
        # -- SF's own calibration, which match.py's adjudication prefers to
        # any curve fitted here. Guarded on the option list: sending an
        # unknown setoption makes some engines print noise.
        self._has_wdl = any("UCI_ShowWDL" in ln for ln in opts)
        if self._has_wdl:
            self._send("setoption name UCI_ShowWDL value true")
        self._send(f"setoption name Threads value {SF_THREADS}")
        self._send(f"setoption name Hash value {SF_HASH}")
        if SF_SKILL is not None:
            self._send(f"setoption name Skill Level value {int(SF_SKILL)}")
        elif SF_ELO <= 0:
            pass                          # STOCKFISH_ELO=0 -> FULL strength
        else:
            elo = max(1320, min(3190, SF_ELO))
            self._send("setoption name UCI_LimitStrength value true")
            self._send(f"setoption name UCI_Elo value {elo}")
        self._send("isready")
        self._read_until("readyok")
        self._send("ucinewgame")

    # -- Engine API --------------------------------------------------- #
    def get_best_move_timed(self, board, time_limit, max_depth=None):
        return self._go(board, f"movetime {int(time_limit * 1000)}")

    def get_best_move(self, board, depth):
        return self._go(board, f"depth {int(depth)}")

    def get_best_move_clock(self, board, wtime_ms, btime_ms,
                            winc_ms=0, binc_ms=0):
        """FI-88: hand Stockfish the CLOCK and let it budget the move itself.

        Every match this project ran before 2026-07-24 drove SF with
        `go movetime <ms>`, where the ms came from Pygin's OWN
        time_manager -- so SF's time management, one of its better-tuned
        components, never ran. Under `go wtime/btime/winc/binc` it decides
        for itself: more on a critical move, less on a forced one, and it
        can bank time for the endgame.

        Hosts detect this method with hasattr, so an engine WITHOUT it
        (ours -- Pygin has no internal clock manager) keeps the movetime
        path. Callers pass WHITE's and BLACK's clocks, not mover/opponent:
        SF reads the side to move from the position."""
        return self._go(board, f"wtime {max(1, int(wtime_ms))} "
                               f"btime {max(1, int(btime_ms))} "
                               f"winc {max(0, int(winc_ms))} "
                               f"binc {max(0, int(binc_ms))}")

    def _go(self, board, limit):
        white_to_move = board.turn == chess.WHITE
        self._ensure_alive()
        try:
            self._send(f"position fen {board.fen()}")
        except BrokenPipeError:
            # Died between poll() and the write: respawn once and retry.
            self._spawn()
            self._send(f"position fen {board.fen()}")
        self._send(f"go {limit}")
        last_info = None
        bestmove = None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("info ") and " pv " in line:
                last_info = line
            elif line.startswith("bestmove"):
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else None
                break
        self._parse_info(last_info, white_to_move)
        if not bestmove or bestmove == "(none)":
            return None
        try:
            return chess.Move.from_uci(bestmove)
        except ValueError:
            return None

    def _parse_info(self, info, white_to_move):
        self.nodes_searched = 0
        self.last_depth = 0
        self.last_pv = ""
        self.last_score = 0
        self.last_wdl = None         # reset BEFORE the early return: a move
                                     # with no info line must not inherit the
                                     # previous move's wdl
        if not info:
            return
        toks = info.split()
        for i, t in enumerate(toks):
            if t == "depth":
                self.last_depth = int(toks[i + 1])
            elif t == "nodes":
                self.nodes_searched = int(toks[i + 1])
            elif t == "score":
                kind, val = toks[i + 1], int(toks[i + 2])
                if kind == "cp":
                    stm = val
                else:                                   # mate in `val`
                    stm = (self.MATE_SCORE - abs(val)) * (1 if val > 0 else -1)
                self.last_score = stm if white_to_move else -stm   # -> WHITE POV
            elif t == "wdl":
                try:
                    self.last_wdl = (int(toks[i + 1]), int(toks[i + 2]),
                                     int(toks[i + 3]))
                except (IndexError, ValueError):
                    self.last_wdl = None
            elif t == "pv":
                self.last_pv = " ".join(toks[i + 1:])
                break

    def __del__(self):
        try:
            self._send("quit")
            self._proc.wait(timeout=1)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
