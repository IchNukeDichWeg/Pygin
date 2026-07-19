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
SF_ELO = int(os.environ.get("STOCKFISH_ELO", "2900"))
SF_SKILL = os.environ.get("STOCKFISH_SKILL")          # if set, used instead of Elo
SF_THREADS = int(os.environ.get("STOCKFISH_THREADS", "1"))
SF_HASH = int(os.environ.get("STOCKFISH_HASH", "64"))


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
        self.last_depth = 0
        self.last_pv = ""

        self._proc = subprocess.Popen(
            [_find_sf()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._uci_handshake()

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
        self._read_until("uciok")
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

    def _go(self, board, limit):
        white_to_move = board.turn == chess.WHITE
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
