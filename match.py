"""
match.py
========
Headless engine-vs-engine match runner -- everything ``engine_battle.py`` does
(subprocess engines with watchdogs, each position played both colours, the same
log-file format and Elo summary) but with NO pygame, so it runs anywhere and,
crucially, under PyPy:

Usage::

    python3 match.py [engine1.py] [engine2.py] [num_positions] [offset] [--workers N] [--engine-smp N]
                     [--book1 book.bin] [--book2 book.bin]   (per-engine opening books; book testing)
                     [--start-pos True]                       (all games from startpos, ignore the FEN file)
                     [--sprt]                                 (SPRT early-stop: quit as soon as the result is
                                                               provably good/bad instead of playing the whole
                                                               budget -- default [0, 4] normalized, a=b=0.05;
                                                               override --sprt-elo0/elo1/alpha/beta/model)

Arguments (all optional, fall back to CONFIG section below):
  engine1.py      path to engine 1 (default: ENGINE_1)
  engine2.py      path to engine 2 (default: ENGINE_2)
  num_positions   positions to test; each is played TWICE (both colours)
                  so total games = num_positions * 2  (default: NUM_GAMES)
  offset          skip this many positions into the pool (for non-overlapping
                  parallel runs on different machines)  (default: 0)
  --workers N     parallel game pairs; keep N * 2 <= CPU cores  (default: N_WORKERS)
                  0 or 'auto' => all cores but one
  --engine-smp N  SMP workers inside each engine; use 1 for match runs,
                  higher only when playing a single game  (default: 1)

Examples::

    python3 match.py engine.py engine_phalanx.py 2500 --workers 10 --engine-smp 1
    python3 match.py engine.py "Old Engine/21/engine21.py" 1000 --workers 5
    python3 match.py engine.py engine_phalanx.py 2500 1000 --workers 5  # offset=1000

Progress is streamed to the terminal; a full per-move/PGN log is written to a
file named like ``<e1>_vs_<e2>_<timestamp>_<pid>.txt``.

Run several copies in parallel for more games (with a fixed SUBSET_SEED they all
draw the SAME positions, so results stay directly comparable / poolable).

Press Ctrl-C to stop early -- the summary (with Elo so far) is still written.
"""

# ====================================================================== #
#  CONFIG  -- edit these
# ====================================================================== #
ENGINE_1 = "engine.py"                       # path to engine 1
ENGINE_2 = "Old Engine/34/engine34.py"       # path to engine 2
FEN_FILE = "UHO_4060_v4.epd"                 # positions (plain FEN or EPD, one per line). UHO_4060_v4.epd (16 MB, balanced Stockfish openings) is the default. fen.txt (447 KB) is also bundled as a small fallback; a bigger book (UHO_Lichess_4852_v1.epd, 174 MB) is at https://github.com/official-stockfish/books

other_elo = 2800
# PGN-header tag only (cosmetic). Reads the SAME env/default as
# stockfish_engine.py's SF_ELO so the tag can't drift from what the
# limiter is actually set to -- the 2026-07-16 run played at 2700 while
# a stale hardcoded 2600 here went into every PGN header.
import os                   # config-time env read; harmlessly re-imported below
stockfish_elo = int(os.environ.get("STOCKFISH_ELO", "2700"))

NUM_GAMES = 5000            # number of starting POSITIONS to play (default when
                            #   no arg passed). Each position is played twice --
                            #   once with each engine as White -- so the actual
                            #   TOTAL games played is NUM_GAMES * 2.
                            #   (Controls for colour bias: every engine plays the
                            #   same starting positions once with each colour.)

MODE = "clock"             # "time"  -> fixed milliseconds per move (TIME_PER_MOVE_MS)
                            # "depth" -> fixed search depth in plies (FIXED_DEPTH)
                            # "clock" -> real clock per side (TC_SECONDS + TC_INCREMENT),
                            #            per-move budget via time_manager.calculate_move_time
TIME_PER_MOVE_MS = 1000      # used when MODE == "time"
FIXED_DEPTH = 10             # used when MODE == "depth"
TC_SECONDS = 50             # used when MODE == "clock": starting clock per side, in seconds
TC_INCREMENT = 0.20         # used when MODE == "clock": seconds added per move
                            # ERA NOTE: 45+0.10 through v36 (the whole ledger
                            # v21..v36); 50+0.20 from v37-era A/Bs on (0.30
                            # was deemed increment-heavy on a 50s base;
                            # revisit 60+0.30 if the base grows). The engine
                            # got ~2x faster and outgrew the old TC.
                            # Cross-era Elo numbers are NOT the same currency.

# --- WDL-based adjudication (OFF until wdl_model.json is calibrated) ------- #
# Shortens decided games: a win is adjudicated when BOTH engines' own
# reported scores agree the game is over (leader >= +threshold, opponent
# <= -threshold, each for ADJ_WIN_COUNT consecutive own moves), where the
# threshold is the cp at which the fitted WDL model says P(win) >= ADJ_WIN_P
# at the current phase. A draw is adjudicated late in level games. Needs
# wdl_model.json (written by fit_wdl_model.py); silently stays off without it.
# MATCH_ADJUDICATE=0 disables per run without editing this file. Use it for
# CROSS-FAMILY matches (e.g. vs stockfish_engine.py): the WDL model is fitted
# on THIS engine's score scale, so the two-sided agreement rule loses its
# calibration against a foreign engine's cp reports. Same-family A/Bs only.
import os                   # config-time env read; harmlessly re-imported below
ADJUDICATE = (os.environ.get("MATCH_ADJUDICATE", "1") != "0")
ADJ_WIN_P = 0.99            # per-phase cp threshold = model's P(win) 99% point
ADJ_WIN_COUNT = 8           # consecutive own moves (each side) for a win call
ADJ_DRAW_CP = 10            # |cp| <= this from both sides...
ADJ_DRAW_COUNT = 16          # ...for this many consecutive plies...
ADJ_DRAW_MIN_PLY = 100       # ...never before this game ply

ENGINE_USE_BOOK = False     # opening books off -> a fair, search-only test
# Per-engine book override (BOOK TESTING): a Polyglot .bin path per side,
# e.g. "Perfect2023.bin". Setting one turns the book ON for that engine
# only, regardless of ENGINE_USE_BOOK -- so two books can be A/B'd against
# each other, or one side plays booked vs the other bookless. None = that
# engine follows ENGINE_USE_BOOK (and the default candidate scan). CLI:
# --book1 PATH / --book2 PATH.
BOOK_ENGINE1 = None
BOOK_ENGINE2 = None
# Start every game from the standard STARTING POSITION instead of FEN_FILE
# (CLI: --start-pos True). Meant for book testing (--book1/--book2): the
# UHO/EPD openings are deliberately ~8-12 plies deep, PAST book, so books
# never fire from them. Game variety then comes from the books' weighted-
# random move choice -- two bookless deterministic engines from startpos
# would repeat the same game, so leave this False for normal A/Bs.
START_POS = False
SUBSET_SEED = 42            # FIXED so parallel windows shuffle identically
MAX_PLIES = 200             # games longer than this are adjudicated a draw
VERBOSE_MOVES = False       # also print every move to the terminal
                            #   (per-move info is ALWAYS written to the log file)
N_WORKERS = 10              # parallel game workers (override via --workers N|auto)
                            #   1  -> sequential (one engine pair, plays all games)
                            #   >1 -> N worker processes, each with its own engine pair

ENGINE_SMP_OVERRIDE = 1     # override engine.SMP_WORKERS for THIS match run.
                            # None  -> respect each engine's own SMP_WORKERS
                            #          (the constant inside engine.py).
                            # int N -> force every engine subprocess this match
                            #          spawns to use N Lazy-SMP workers, by
                            #          exporting CLAUDECHESS_SMP=N before the
                            #          first subprocess is launched. Lets a
                            #          single match override the file default
                            #          without editing engine.py, and matches
                            #          the env-var semantics the engine already
                            #          honours (see SMP_WORKERS in engine.py).
                            # Keep ENGINE_SMP_OVERRIDE * N_WORKERS * 2  (two
                            # engines per game) <= CPU cores or you will
                            # oversubscribe and lose throughput.

# ====================================================================== #
#  Internals (rarely need changing)
# ====================================================================== #
PV_UCI = True                # PV format in the log: True = UCI (g1f3), False = SAN (Nf3)
MAX_DEPTH_CAP = 30           # max_depth handed to the timed search
DEPTH_SAFETY_CAP = 30.0      # seconds: watchdog for a runaway fixed-depth search
TIME_OVERSHOOT_FACTOR = 2.0  # time-mode watchdog = budget * factor + grace
TIME_GRACE = 4.0
LOAD_TIMEOUT = 30.0          # seconds to wait for an engine process to load

import datetime
import math
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from queue import Empty

import chess
import chess.pgn

from battle_worker import engine_worker
from time_manager import calculate_move_time

# Optional SPRT early-stop (--sprt). Imported defensively so a broken/missing
# sprt.py can never take a match down -- the feature just goes unavailable.
try:
    import sprt as _sprt
except Exception:
    _sprt = None

# Don't evaluate the SPRT on a tiny sample (the LLR is noisy early and could
# early-stop on a fluke); wait for this many PAIRS first. 500 pairs = 1000
# games -- a decision can't fire before then.
SPRT_MIN_PAIRS = 500


# ====================================================================== #
# Engine subprocess handle (parent side) -- ported from engine_battle.py
# ====================================================================== #
class EngineError(Exception):
    """The engine failed to load or raised while searching."""


class EngineTimeout(Exception):
    """The engine did not return a move within the watchdog window."""


class EngineProcess:
    """Owns one engine subprocess and talks to it over a pipe."""

    def __init__(self, ctx, path, book_path=None):
        self.ctx = ctx
        self.path = path
        self.book_path = book_path           # per-engine book (--book1/--book2)
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.proc = None
        self.conn = None

    def start(self):
        self._spawn()

    def _spawn(self):
        parent_conn, child_conn = self.ctx.Pipe()
        self.conn = parent_conn
        self.proc = self.ctx.Process(
            target=engine_worker,
            args=(child_conn, self.path, ENGINE_USE_BOOK, PV_UCI,
                  self.book_path),
            # NOT daemon: an engine using Lazy SMP (CLAUDECHESS_SMP/SMP_WORKERS)
            # spawns its own worker pool, and daemonic processes are forbidden
            # from having children. The engine process is shut down explicitly
            # via the shutdown protocol / terminate(), so non-daemon is safe.
            daemon=False,
        )
        self.proc.start()
        child_conn.close()
        if not self.conn.poll(LOAD_TIMEOUT):
            self.kill()
            raise EngineError(f"{self.name}: timed out while loading")
        msg = self.conn.recv()
        if msg[0] == "ready":
            return
        self.kill()
        if msg[0] == "fatal":
            raise EngineError(f"{self.name} failed to load:\n{msg[1]}")
        raise EngineError(f"{self.name}: unexpected reply {msg[0]!r} on load")

    def request_move(self, fen, mode, value, timeout):
        """Ask for a move; kill+respawn and raise on a timeout (hung search)."""
        self.conn.send(("move", fen, mode, value, MAX_DEPTH_CAP))
        if not self.conn.poll(timeout):
            self.kill()
            self._spawn()                # fresh process for the remaining games
            raise EngineTimeout(
                f"{self.name}: no move within {timeout:.2f}s (killed)")
        msg = self.conn.recv()
        if msg[0] == "ok":
            return msg[1]
        if msg[0] == "error":
            raise EngineError(f"{self.name} crashed during search:\n{msg[1]}")
        raise EngineError(f"{self.name}: unexpected reply {msg[0]!r}")

    def kill(self):
        try:
            if self.proc is not None and self.proc.is_alive():
                self.proc.terminate()
                self.proc.join(timeout=2)
        except Exception:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass


# ====================================================================== #
# Helpers
# ====================================================================== #
def load_fens(path):
    """Load and validate every position in ``path`` (plain FEN or EPD)."""
    fens = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                try:
                    chess.Board(s)                       # plain FEN
                    fens.append(s)
                    continue
                except Exception:
                    pass
                parts = s.split()                        # EPD: keep board + supply clocks
                if len(parts) >= 4:
                    cand = " ".join(parts[:4]) + " 0 1"
                    try:
                        chess.Board(cand)
                        fens.append(cand)
                    except Exception:
                        pass
    if not fens:
        fens = [chess.STARTING_FEN]
    return fens


def elo(score, n):
    """Elo difference for a match score in [0,1] over n games, with a rough 95%
    margin. Returns (elo, margin)."""
    score = min(max(score, 1e-9), 1 - 1e-9)
    e = -400.0 * math.log10(1.0 / score - 1.0)
    if n <= 0:
        return e, 999.0
    se = 0.5 / math.sqrt(n)
    lo = min(max(score - 1.96 * se, 1e-9), 1 - 1e-9)
    hi = min(max(score + 1.96 * se, 1e-9), 1 - 1e-9)
    margin = (-400.0 * math.log10(1.0 / hi - 1.0)
              - (-400.0 * math.log10(1.0 / lo - 1.0))) / 2.0
    return e, margin


# ====================================================================== #
# Pentanomial (paired-game) statistics
# ====================================================================== #
# The schedule already plays every FEN as a PAIR -- round 2k+1 with Engine 1
# White, round 2k+2 with Engine 2 White, same position (see schedule build in
# main()). Scoring the PAIR's combined result instead of each game in
# isolation is the standard paired-openings methodology (Fishtest/OpenBench):
# it cancels most of the opening-imbalance noise, which is what makes the
# Normalized Elo below a tighter, draw-rate-corrected effect size than the
# naive win/draw/loss Elo.
PENTA_LABELS = {0: "LL", 1: "LD", 2: "DD_WL", 3: "WD", 4: "WW"}


def game_score_e1(g, e1):
    """Engine 1's score for one finished game: 1.0 win / 0.5 draw / 0.0 loss.
    None for an errored/excluded game -- its pair can't be scored either."""
    if g["error"] is not None:
        return None
    if g["winner"] is None:
        return 0.5
    return 1.0 if g["winner"] is e1 else 0.0


def pentanomial_bucket(score_a, score_b):
    """Map two per-game E1 scores (each 0/0.5/1) to a pentanomial index 0..4:
    0=LL  1=LD  2=DD_WL (two draws OR a win+a loss -- both sum to 1)  3=WD  4=WW."""
    return round((score_a + score_b) * 2)


def pair_ratio(penta):
    """(WW + WD) / (LL + LD) -- a quick, distribution-free signal of which
    engine is ahead. Returns None (not a divide-by-zero crash) when the
    denominator is 0; the caller decides how to display that (e.g. "no
    losing pairs yet" vs "no pairs at all")."""
    denom = penta[0] + penta[1]
    if denom == 0:
        return None
    return (penta[4] + penta[3]) / denom


def elo_from_score(score):
    """Point-estimate Elo from a win/draw/loss score in (0, 1). Rounded to
    2 dp -- note round() on a float drops trailing zeros (5.1, not 5.10);
    use f'{elo_from_score(s):.2f}' wherever the fixed 2-decimal STRING
    ("5.10") matters for display."""
    score = min(max(score, 1e-9), 1 - 1e-9)
    return round(-400.0 * math.log10(1.0 / score - 1.0), 2)


def normalized_elo(penta):
    """
    Fishtest-style Normalized Elo (nElo): an effect size in "Elo per standard
    deviation of game score" units, computed from the pentanomial pair
    distribution instead of the raw win rate.

    Why this corrects for draw-rate inflation: elo_from_score() only looks at
    the MEAN score. Two matches with the same mean score but different draw
    rates have very different score VARIANCE -- a higher draw rate compresses
    the score distribution toward 0.5, so the same mean edge is a stronger
    (less noisy) signal. nElo divides the score's distance from the 50%
    (draws-only) baseline by its standard deviation before converting to Elo
    units, so it rises with the draw rate for a fixed raw score -- correcting
    exactly the bias that makes raw Elo look smaller in high-draw-rate
    matches (e.g. near-equal engines at long time controls).

    `penta` is a dict/sequence of pair counts indexed 0..4 (LL, LD, DD_WL,
    WD, WW). Returns None when there's no variance to normalize by (zero
    pairs, or every pair landed in the same bucket).
    """
    n = sum(penta[i] for i in range(5))
    if n == 0:
        return None
    pair_scores = (0.0, 0.5, 1.0, 1.5, 2.0)        # score out of 2 per pair
    p = [penta[i] / n for i in range(5)]
    pair_mean = sum(p[i] * pair_scores[i] for i in range(5))
    pair_var = sum(p[i] * (pair_scores[i] - pair_mean) ** 2 for i in range(5))
    game_mean = pair_mean / 2.0                     # score out of 1 per game
    game_var = pair_var / 4.0                        # Var(pair) / 2**2
    sigma = math.sqrt(game_var)
    if sigma == 0.0:
        return None
    nelo = (game_mean - 0.5) / sigma * (800.0 / math.log(10))
    return round(nelo, 2)


def fmt_duration(seconds):
    ms = max(0, int(round(seconds * 1000)))
    d, ms = divmod(ms, 86_400_000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{d}d {h}h {m}m {s}s {ms}ms"


def fmt_clock(ms):
    if ms is None:
        return "-"
    s = max(0, int(ms)) / 1000.0
    return f"{int(s) // 60}:{int(s) % 60:02d}" if s >= 60 else f"{s:.2f}s"


def build_pgn(round_no, fen, white, black, board, result, now, tc_label, tpm):
    game = chess.pgn.Game()
    game.setup(chess.Board(fen))
    game.headers["Result"] = result   # ensures movetext ends with the correct terminator
    node = game
    for mv in board.move_stack:
        node = node.add_variation(mv)
    if white.name == 'stockfish_engine' or black.name == 'stockfish_engine':
        if white.name == 'stockfish_engine':
            white_elo = stockfish_elo
            black_elo = other_elo
        else:
            black_elo = stockfish_elo
            white_elo = other_elo
    else:
        white_elo = other_elo
        black_elo = other_elo
    exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    movetext = game.accept(exporter).strip() or result
    header = [
        '[Event "Engine Match"]', '[Site "Local"]',
        f'[Date "{now.strftime("%Y.%m.%d")}"]', f'[Round "{round_no}"]',
        f'[White "{white.name}"]', f'[Black "{black.name}"]',
        # f'[TimeControl "{tc_label}"]' if tc_label else '',
        # f'[Time Per Move "{tpm}ms"]' if tpm is not None else '',
        f'[BlackElo "{black_elo}"]',
        f'[WhiteElo "{white_elo}"]',
        f'[FEN "{fen}"]', f'[Result "{result}"]',
    ]
    return "\n".join(h for h in header if h) + "\n" + movetext


# ====================================================================== #
# One game
# ====================================================================== #
# --- WDL adjudication runtime (config block up top) ------------------------ #
_WDL_THR = ["unloaded"]     # per-process cache; None = model missing -> off


def _wdl_win_threshold(phase):
    """cp at which the fitted WDL model puts P(win) at ADJ_WIN_P for this
    phase, or None while wdl_model.json doesn't exist (adjudication then
    silently stays off). Loaded once per process -- each game worker reads
    the file on its first adjudication check."""
    if _WDL_THR[0] == "unloaded":
        try:
            import json
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "wdl_model.json")
            with open(path, encoding="utf-8") as f:
                mod = json.load(f)
            AS, BS = mod["as"], mod["bs"]
            pmax, pmin = mod["phase_max"], mod["phase_clamp_min"]
            # invert w = 1/(1+exp((a-cp)/b)) = p  ->  cp = a - b*ln(1/p - 1)
            gap = math.log(1.0 / ADJ_WIN_P - 1.0)

            def thr(ph):
                x = min(max(ph, pmin), pmax) / pmax
                a = ((AS[0] * x + AS[1]) * x + AS[2]) * x + AS[3]
                b = ((BS[0] * x + BS[1]) * x + BS[2]) * x + BS[3]
                return a - b * gap
            _WDL_THR[0] = thr
        except (OSError, ValueError, KeyError):
            _WDL_THR[0] = None
    return None if _WDL_THR[0] is None else _WDL_THR[0](phase)


def _phase24(board):
    """Tapered phase 0..24 (mirrors engine.py's PHASE_WEIGHTS/PHASE_MAX)."""
    npm = (chess.popcount(board.knights | board.bishops)
           + 2 * chess.popcount(board.rooks)
           + 4 * chess.popcount(board.queens))
    return min(24, npm)


def play_game(round_no, fen, white, black, e1, mode_cfg):
    """Play a single game. Returns a dict of results + the per-move log lines."""
    board = chess.Board(fen)
    engine_log = []
    error = None
    result = "*"
    reason = ""

    is_clock = (mode_cfg["mode"] == "clock")
    if is_clock:
        init_ms = int(mode_cfg["tc_seconds"] * 1000)
        clocks = {chess.WHITE: init_ms, chess.BLACK: init_ms}
        inc_ms = int(mode_cfg["tc_increment"] * 1000)
    else:
        clocks, inc_ms = None, 0
    clock_started = False

    # WDL adjudication state (see ADJUDICATE; all no-ops while it's off).
    # Keys are booleans (True = White), matching `mover_is_white`.
    adj_win = {True: 0, False: 0}    # consecutive own moves >= +threshold
    adj_lose = {True: 0, False: 0}   # consecutive own moves <= -threshold
    adj_draw = 0                     # consecutive near-zero plies (both sides)

    while True:
        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            result = outcome.result()
            reason = outcome.termination.name
            break
        if board.ply() >= MAX_PLIES:
            result, reason = "1/2-1/2", "MAX_PLIES (adjudicated draw)"
            break

        mover = white if board.turn == chess.WHITE else black
        mover_is_white = board.turn == chess.WHITE

        # Decide the per-move request (mode / value / watchdog).
        if is_clock:
            color = board.turn
            budget = calculate_move_time(board, clocks[color], clocks[not color], inc_ms)
            req_mode, req_value = "time", budget
            req_timeout = budget / 1000.0 * TIME_OVERSHOOT_FACTOR + TIME_GRACE
        elif mode_cfg["mode"] == "depth":
            req_mode, req_value = "depth", mode_cfg["depth"]
            req_timeout = DEPTH_SAFETY_CAP
        else:  # "time"
            req_mode, req_value = "time", mode_cfg["time_ms"]
            req_timeout = mode_cfg["time_ms"] / 1000.0 * TIME_OVERSHOOT_FACTOR + TIME_GRACE

        try:
            # Send start FEN + move history (not a bare FEN) so the engine's
            # repetition detection sees the whole game -- see battle_worker's
            # protocol note.
            res = mover.request_move(
                (fen, [m.uci() for m in board.move_stack]),
                req_mode, req_value, req_timeout)
        except (EngineError, EngineTimeout) as ex:
            error = str(ex)
            break

        # Clock bookkeeping + flag-fall (clock mode only). First move is untimed.
        if is_clock:
            if not clock_started:
                clock_started = True
            else:
                clocks[color] -= int(res.get("time_ms", 0))
                if clocks[color] < 0:
                    result = "0-1" if color == chess.WHITE else "1-0"
                    reason = "TIME_FORFEIT"
                    break
                clocks[color] += inc_ms

        uci = res.get("uci")
        if uci is None:
            error = f"{mover.name} returned no move"
            break
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            error = f"{mover.name} returned an unparseable move {uci!r}"
            break
        if move not in board.legal_moves:
            error = f"{mover.name} returned an illegal move {uci!r}"
            break

        san = board.san(move)
        engine_log.append(f"[{mover.name}] move {san}: {res['info']}")
        if res.get("pv"):
            engine_log.append(f"    PV: {res['pv']}")
        if VERBOSE_MOVES:
            clk = ""
            if is_clock:
                clk = f"  [{fmt_clock(clocks[chess.WHITE])} / {fmt_clock(clocks[chess.BLACK])}]"
            print(f"      {mover.name:>14} {san:7} {res['info']}{clk}")
        board.push(move)

        # --- WDL adjudication: end games both engines agree are decided --- #
        if ADJUDICATE:
            thr = _wdl_win_threshold(_phase24(board))
            if thr is not None:
                mate = res.get("mate")
                cp = res.get("score_cp")
                eff = (10_000 if (mate is not None and mate > 0) else
                       -10_000 if mate is not None else cp)
                if eff is None:
                    adj_win[mover_is_white] = adj_lose[mover_is_white] = 0
                    adj_draw = 0
                else:
                    adj_win[mover_is_white] = (adj_win[mover_is_white] + 1
                                               if eff >= thr else 0)
                    adj_lose[mover_is_white] = (adj_lose[mover_is_white] + 1
                                                if eff <= -thr else 0)
                    adj_draw = (adj_draw + 1
                                if mate is None and abs(eff) <= ADJ_DRAW_CP
                                else 0)
                # Win: mover has claimed a win for N own moves AND the
                # opponent's own last N scores concede it (two-sided
                # agreement, like cutechess resign adjudication).
                if (adj_win[mover_is_white] >= ADJ_WIN_COUNT
                        and adj_lose[not mover_is_white] >= ADJ_WIN_COUNT):
                    result = "1-0" if mover_is_white else "0-1"
                    reason = "ADJUDICATION_WIN"
                    break
                if (board.ply() >= ADJ_DRAW_MIN_PLY
                        and adj_draw >= ADJ_DRAW_COUNT):
                    result, reason = "1/2-1/2", "ADJUDICATION_DRAW"
                    break

    # Score (errored games are excluded; a clock forfeit is a real loss).
    winner = None
    if error is None and result in ("1-0", "0-1", "1/2-1/2"):
        if result != "1/2-1/2":
            winner = white if result == "1-0" else black
    return {
        "round": round_no, "fen": fen, "white": white, "black": black,
        "result": result, "reason": reason, "error": error, "winner": winner,
        "board": board, "log": engine_log, "clocks": clocks,
    }


# ====================================================================== #
# Logging
# ====================================================================== #
def write_game_block(fh, pgn_fh, g, e1, mode_cfg, tc_label, tpm):
    now = datetime.datetime.now()
    white, black, board = g["white"], g["black"], g["board"]
    pgn_str = build_pgn(g["round"], g["fen"], white, black, board,
                        g["result"], now, tc_label, tpm)
    if fh is not None:
        wlab = "Engine 1" if white is e1 else "Engine 2"
        blab = "Engine 1" if black is e1 else "Engine 2"
        out = [f"=== Game {g['round']} ===", f"FEN: {g['fen']}",
               f"{wlab} (White): {white.path}", f"{blab} (Black): {black.path}"]
        if mode_cfg["mode"] == "clock":
            out.append(f"Mode: Time control = {tc_label} (min + sec/move, dynamic budget)")
        elif mode_cfg["mode"] == "depth":
            out.append(f"Mode: Depth = {mode_cfg['depth']}")
        else:
            out.append(f"Mode: Time = {mode_cfg['time_ms']} ms/move")
        if g["error"]:
            out.append(f"Outcome: ERROR / excluded -- {g['error']}")
        elif g["result"] == "1/2-1/2":
            out.append(f"Outcome: draw ({g['reason']})")
        elif g["winner"] is not None:
            wl = "Engine 1" if g["winner"] is e1 else "Engine 2"
            wc = "White" if g["winner"] is white else "Black"
            out.append(f"Outcome: {g['winner'].name} ({wl}, {wc}) won -- {g['result']} ({g['reason']})")
        else:
            out.append(f"Outcome: {g['result']} ({g['reason']})")
        out.append("--- Engine Logs ---")
        out.extend(g["log"] if g["log"] else ["(no moves played)"])
        out.append("--- PGN ---")
        out.append(pgn_str)
        out.append("")
        try:
            fh.write("\n".join(out) + "\n")
            fh.flush()
        except Exception:
            pass
    if pgn_fh is not None:
        try:
            pgn_fh.write(pgn_str + "\n\n")
            pgn_fh.flush()
        except Exception:
            pass


def write_summary(fh, e1, e2, tally, total_games, start_t, stopped,
                  n_workers=None, sprt_info=None):
    lines = ["", "=== BATTLE SUMMARY ===",
             f"Engine 1: {e1.name}", f"Engine 2: {e2.name}",
             f"Games scored: {tally['completed']:,}  (of {total_games:,} scheduled)",
             f"Workers: {n_workers}",
             f"Engine 1 Wins: {tally['e1']:,}", f"Engine 2 Wins: {tally['e2']:,}",
             f"Draws: {tally['draws']:,}"]
    if int(tally['errors']) > 0:
        lines.append(f"Errors/Skipped (excluded): {tally['errors']:,}")
    if tally["completed"]:
        score = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
        el, margin = elo(score, tally["completed"])
        lines.append(
            f"Engine 1 score: {tally['e1'] + 0.5*tally['draws']:.2f}/{tally['completed']} "
            f"({100*score:.2f}%)  =>  {el:+.2f} +/- {margin:.1f} Elo")
        lines.append(f"Raw Elo (point estimate): {elo_from_score(score):.2f}")
    penta = tally.get("penta")
    if penta and sum(penta.values()) > 0:
        n_pairs = sum(penta.values())
        breakdown = "  ".join(f"{PENTA_LABELS[i]}={penta[i]}" for i in range(5))
        lines.append(f"Pairs scored: {n_pairs:,}  ({breakdown})")
        # Standard Fishtest-style compact array: index order 0..4 is always
        # LL, LD, DD_WL, WD, WW (see PENTA_LABELS) -- plain ints, no thousands
        # separators, so it pastes directly into other Fishtest-style tooling.
        ptnml = ", ".join(str(penta[i]) for i in range(5))
        lines.append(f"Ptnml: {ptnml}")
        if tally.get("penta_incomplete"):
            lines.append(f"Incomplete pairs (excluded): {tally['penta_incomplete']:,}")
        ratio = pair_ratio(penta)
        if ratio is not None:
            ratio_s = f"{ratio:.2f}"
        elif penta[4] + penta[3] > 0:
            ratio_s = "inf (no losing pairs yet)"
        else:
            ratio_s = "n/a"
        lines.append(f"Game pair ratio (WW+WD)/(LL+LD): {ratio_s}")
        nelo = normalized_elo(penta)
        lines.append(f"Normalized Elo: {f'{nelo:+.2f}' if nelo is not None else 'n/a'}")
    si = sprt_info
    sprt_decided = bool(si and si.get("decided"))
    if si and si.get("cfg") and si.get("llr") is not None:
        cfg = si["cfg"]
        lines.append(
            f"SPRT[{cfg['elo0']:g}, {cfg['elo1']:g}] {cfg['model']} "
            f"(alpha={cfg['alpha']:g} beta={cfg['beta']:g}): "
            f"LLR {si['llr']:+.3f} in [{si['lower']:+.3f}, {si['upper']:+.3f}]")
        dec = si.get("decided")
        if dec == "H1":
            lines.append("SPRT verdict: ACCEPT H1 -- change is good (ship); "
                         "stopped early.")
        elif dec == "H0":
            lines.append("SPRT verdict: ACCEPT H0 -- change rejected; "
                         "stopped early.")
        else:
            lines.append("SPRT verdict: no decision within the game budget "
                         "(inconclusive -- read the Elo / ptnml above).")
    # An SPRT stop is a CONCLUSION, not an interruption, so don't mislabel it.
    if stopped and not sprt_decided:
        lines.append("(match was stopped before completion)")
    if start_t is not None:
        elapsed = time.time() - start_t
        played = tally["completed"] + tally["errors"]
        per = fmt_duration(elapsed / played) if played else "-"
        lines += [
                  f"Duration: {fmt_duration(elapsed)}   (per game: {per})",
                  "",
                  f"Ended:    {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",]
    text = "\n".join(lines)
    print("\n" + text)
    if fh is not None:
        try:
            fh.write(text + "\n")
            fh.flush()
        except Exception:
            pass


# ====================================================================== #
# Parallel workers: each worker owns one engine pair across many games.
# ====================================================================== #
class _EngineStub:
    """Naming stand-in used main-side when real engines live in workers."""
    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]


def _pack_result(g, e1):
    """Strip non-picklable references from a play_game result so it can be
    sent back over a queue. The worker only knows its own e1/e2 identity."""
    return {
        "round": g["round"], "fen": g["fen"],
        "result": g["result"], "reason": g["reason"], "error": g["error"],
        "white_is_e1": g["white"] is e1,
        "winner_is_e1": (g["winner"] is e1) if g["winner"] is not None else None,
        "moves_uci": [m.uci() for m in g["board"].move_stack],
        "log": g["log"],
        "clocks": g["clocks"],
    }


def _unpack_result(r, e1, e2):
    """Rebuild a g-style dict (with main-side EngineProcess/stub refs)."""
    board = chess.Board(r["fen"])
    for uci in r["moves_uci"]:
        board.push(chess.Move.from_uci(uci))
    white = e1 if r["white_is_e1"] else e2
    black = e2 if r["white_is_e1"] else e1
    if r["winner_is_e1"] is True:
        winner = e1
    elif r["winner_is_e1"] is False:
        winner = e2
    else:
        winner = None
    return {
        "round": r["round"], "fen": r["fen"],
        "white": white, "black": black,
        "result": r["result"], "reason": r["reason"], "error": r["error"],
        "winner": winner, "board": board, "log": r["log"], "clocks": r["clocks"],
    }


def _worker_loop(in_q, out_q, engine1_path, engine2_path, mode_cfg,
                 use_book, pv_uci, book1=None, book2=None):
    """Worker entry point: hold one engine pair, pull (rno, fen, white_is_e1)
    jobs off `in_q`, push packed results onto `out_q`. Engine startup is paid
    ONCE per worker, not per game -- crucial since loading an engine .py file
    + its weights can take seconds."""
    import multiprocessing as wmp           # nested mp inside the worker
    import signal

    # Ctrl-C hits the whole process group, and KeyboardInterrupt is a
    # BaseException -- it sails past the `except Exception` below and made every
    # worker dump a traceback. The parent owns interrupt handling; this worker
    # shuts down only via the in_q sentinel or terminate():
    #   SIGINT  -> ignored.
    #   SIGTERM -> SystemExit, so the `finally` still runs and this worker's TWO
    #              engine grandchildren get killed instead of being orphaned
    #              (terminate()'s default handler would skip the finally).
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass

    # Propagate the toggles play_game / write_game_block read off the module
    # globals (kept simple instead of threading every flag through call sites).
    global ENGINE_USE_BOOK, PV_UCI
    ENGINE_USE_BOOK = use_book
    PV_UCI = pv_uci

    ctx = wmp.get_context("spawn")
    e1 = EngineProcess(ctx, engine1_path, book1)
    e2 = EngineProcess(ctx, engine2_path, book2)
    try:
        e1.start()
        e2.start()
        while True:
            job = in_q.get()
            if job is None:
                return
            round_no, fen, white_is_e1 = job
            white = e1 if white_is_e1 else e2
            black = e2 if white_is_e1 else e1
            try:
                g = play_game(round_no, fen, white, black, e1, mode_cfg)
                out_q.put(_pack_result(g, e1))
            except Exception as ex:
                import traceback
                out_q.put({
                    "round": round_no, "fen": fen,
                    "result": "*", "reason": "WORKER_EXCEPTION",
                    "error": f"{ex}\n{traceback.format_exc()}",
                    "white_is_e1": white_is_e1, "winner_is_e1": None,
                    "moves_uci": [], "log": [], "clocks": None,
                })
    finally:
        e1.kill()
        e2.kill()


class _SPRTStop(Exception):
    """Raised from the result loop when the SPRT crosses a bound, so the match
    unwinds through the SAME finally-block shutdown that Ctrl-C uses (workers
    told to stop, joined, terminated). A conclusion, not an interruption."""


# How the run ended, for the interrupt message. Set by the SIGTERM handler;
# Ctrl-C leaves it None (Python raises KeyboardInterrupt on its own).
_signal_name = None


def _install_signal_handlers():
    """Make SIGTERM behave exactly like Ctrl-C in the MAIN process: raise
    KeyboardInterrupt so the result loop unwinds into the finally block that
    writes the summary + Ptnml. Without this, `kill`/`pkill` (and any job
    scheduler that stops a run politely) hit Python's default SIGTERM handler,
    which exits immediately and silently -- losing the summary of a run that
    may have taken hours."""
    def _on_sigterm(signum, _frame):
        global _signal_name
        _signal_name = signal.Signals(signum).name
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass                     # non-main thread / unsupported platform


def _shutdown_workers(workers, in_q, out_q, graceful):
    """Stop every worker within a bounded wall-clock budget.

    `graceful` (the schedule ran out) -> sentinel; each worker returns after its
    current game. Otherwise (Ctrl-C / SIGTERM / SPRT / engine error) a sentinel
    would sit behind the whole un-consumed backlog on in_q, so terminate()
    instead -- the worker's SIGTERM handler turns that into a clean unwind.

    The joins share ONE deadline: joining each worker with its own 3s timeout
    serialised into 3s x n_workers (24s on an 8-worker box) before the first
    SIGKILL ever landed."""
    if graceful and in_q is not None:
        for _ in workers:
            try:
                in_q.put(None)
            except Exception:
                pass
    else:
        # A Queue hands its items to a background feeder thread, and the
        # interpreter joins that thread at exit. With the workers about to die,
        # nothing drains in_q -- so once the un-consumed backlog exceeds the
        # ~64KB pipe buffer the feeder blocks forever and match.py never exits
        # (a Ctrl-C at 5,000 positions = 10,000 queued jobs is well past it).
        # The jobs are being abandoned anyway; drop them.
        for q in (in_q, out_q):
            try:
                if q is not None:
                    q.cancel_join_thread()
            except Exception:
                pass
        for w in workers:
            try:
                w.terminate()
            except Exception:
                pass
    deadline = time.time() + 6.0
    for w in workers:
        try:
            w.join(timeout=max(0.1, deadline - time.time()))
        except Exception:
            pass
    for w in workers:                # last resort: a worker wedged in C code
        try:
            if w.is_alive():
                w.kill()
                w.join(timeout=1.0)
        except Exception:
            pass


# ====================================================================== #
# Main
# ====================================================================== #
def main():
    # Apply the engine SMP override FIRST -- the moment any worker (and hence
    # any engine subprocess) is spawned it inherits the current environment,
    # so this must happen before mp.get_context("spawn") or any .start() call
    # below. CLI flag wins over the CONFIG constant so you can do e.g.
    # ``python3 match.py --engine-smp 8`` without editing the file.
    smp_override = ENGINE_SMP_OVERRIDE
    if "--engine-smp" in sys.argv:
        i = sys.argv.index("--engine-smp")
        if i + 1 < len(sys.argv):
            smp_override = int(sys.argv[i + 1])
            del sys.argv[i:i + 2]
    if smp_override is not None:
        os.environ["CLAUDECHESS_SMP"] = str(int(smp_override))
        print(f"[match] engine SMP override: CLAUDECHESS_SMP={smp_override}")

    # Optional command-line overrides so parallel windows can run DIFFERENT
    # matchups or DISJOINT position shards without editing this file:
    #     pypy3 match.py [engine1] [engine2] [num_positions] [offset] [--workers N|auto]
    # The 3rd positional arg is the number of POSITIONS to play. Each position is
    # played twice (once with each engine as White), so TOTAL GAMES = arg * 2.
    # `offset` skips that many positions into the (seeded) shuffled pool, so
    # parallel windows with a FIXED SUBSET_SEED and offsets 0, N, 2N, ... each
    # play a non-overlapping slice. Any omitted argument falls back to CONFIG.
    # `--workers N` (or `--workers auto`) plays N games in parallel inside ONE
    # match run -- each worker owns its own engine pair, so N workers means
    # N pairs of engine subprocesses running concurrently.
    # Flag overrides for the CONFIG constants above (used by the AllIn1 web
    # dashboard, harmless from a terminal). Anything not passed keeps CONFIG.
    global MODE, TIME_PER_MOVE_MS, FIXED_DEPTH, TC_SECONDS, TC_INCREMENT, \
        ADJUDICATE, FEN_FILE, BOOK_ENGINE1, BOOK_ENGINE2, START_POS
    argv = sys.argv[1:]
    workers_str = None
    positional = []
    # SPRT early-stop config (opt-in via --sprt). Defaults = Option 2: a
    # [0, 4] normalized test, alpha=beta=0.05. Wider than Fishtest's standard
    # [0, 2] on purpose -- this repo's A/Bs are usually clearly-good or
    # clearly-null, and [0, 4] decides both far sooner within a 5000-pair
    # budget (a clear winner stops ~halfway; nothing ever runs longer than
    # the budget). Override any bound with --sprt-elo0/elo1/alpha/beta/model.
    sprt_enable = False
    sprt_elo0, sprt_elo1 = 0.0, 4.0
    sprt_alpha, sprt_beta = 0.05, 0.05
    sprt_model = "normalized"
    i = 0
    while i < len(argv):
        if argv[i] == "--workers" and i + 1 < len(argv):
            workers_str = argv[i + 1]
            i += 2
        elif argv[i].startswith("--workers="):
            workers_str = argv[i].split("=", 1)[1]
            i += 1
        elif argv[i] == "--mode" and i + 1 < len(argv):
            MODE = argv[i + 1]
            i += 2
        elif argv[i] == "--tc-seconds" and i + 1 < len(argv):
            # float, not int: a fractional base clock (e.g. 7.5s hyper-TC)
            # was silently impossible from the CLI while --tc-increment
            # already took floats.
            TC_SECONDS = float(argv[i + 1])
            i += 2
        elif argv[i] == "--tc-increment" and i + 1 < len(argv):
            TC_INCREMENT = float(argv[i + 1])
            i += 2
        elif argv[i] == "--time-per-move" and i + 1 < len(argv):
            TIME_PER_MOVE_MS = int(argv[i + 1])
            i += 2
        elif argv[i] == "--book1" and i + 1 < len(argv):
            BOOK_ENGINE1 = argv[i + 1]       # book testing: engine 1's .bin
            i += 2
        elif argv[i] == "--book2" and i + 1 < len(argv):
            BOOK_ENGINE2 = argv[i + 1]       # book testing: engine 2's .bin
            i += 2
        elif argv[i] == "--start-pos" and i + 1 < len(argv):
            START_POS = argv[i + 1].strip().lower() in ("true", "1", "yes")
            i += 2
        elif argv[i] == "--fixed-depth" and i + 1 < len(argv):
            FIXED_DEPTH = int(argv[i + 1])
            i += 2
        elif argv[i] == "--adjudicate" and i + 1 < len(argv):
            # Via env, not just the global: `spawn` workers re-import this
            # module and re-read MATCH_ADJUDICATE at line ~78, so the env is
            # the only channel that reaches play_game() in the children.
            os.environ["MATCH_ADJUDICATE"] = \
                "1" if argv[i + 1].lower() == "true" else "0"
            ADJUDICATE = argv[i + 1].lower() == "true"
            i += 2
        elif argv[i] == "--fen-file" and i + 1 < len(argv):
            FEN_FILE = argv[i + 1]
            i += 2
        elif argv[i] == "--sprt":
            sprt_enable = True
            i += 1
        elif argv[i] == "--sprt-elo0" and i + 1 < len(argv):
            sprt_elo0 = float(argv[i + 1])
            i += 2
        elif argv[i] == "--sprt-elo1" and i + 1 < len(argv):
            sprt_elo1 = float(argv[i + 1])
            i += 2
        elif argv[i] == "--sprt-alpha" and i + 1 < len(argv):
            sprt_alpha = float(argv[i + 1])
            i += 2
        elif argv[i] == "--sprt-beta" and i + 1 < len(argv):
            sprt_beta = float(argv[i + 1])
            i += 2
        elif argv[i] == "--sprt-model" and i + 1 < len(argv):
            sprt_model = argv[i + 1].strip().lower()
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    engine1 = positional[0] if len(positional) > 0 else ENGINE_1
    engine2 = positional[1] if len(positional) > 1 else ENGINE_2
    # Positional arg is the number of POSITIONS; each is played twice (both
    # colours), so total games = num_positions * 2.
    num_positions = max(1, int(positional[2]) if len(positional) > 2 else NUM_GAMES)
    offset = int(positional[3]) if len(positional) > 3 else 0

    if workers_str is None:
        n_workers = max(1, int(N_WORKERS))
    elif workers_str.lower() == "auto" or int(workers_str) == 0:
        n_workers = max(1, mp.cpu_count() - 1)  # 0 / auto => all cores but one
    else:
        n_workers = max(1, int(workers_str))

    for p in (engine1, engine2):
        if not os.path.isfile(p):
            print(f"ERROR: engine file not found: {p!r}")
            return

    mode_cfg = {"mode": MODE, "time_ms": TIME_PER_MOVE_MS, "depth": FIXED_DEPTH,
                "tc_seconds": TC_SECONDS, "tc_increment": TC_INCREMENT}
    tc_label = f"{TC_SECONDS:.2f}+{TC_INCREMENT:.2f}" if MODE == "clock" else None
    tpm = TIME_PER_MOVE_MS if MODE == "time" else None

    # Positions -> seeded shuffle, then take the slice [offset : offset+n].
    # With a FIXED SUBSET_SEED every window shuffles identically, so distinct
    # offsets give DISJOINT shards (no overlap across parallel windows).
    if START_POS:
        # --start-pos True: every game from the standard starting position
        # (book testing). One repeated "position" per scheduled pair; the
        # offset/shuffle machinery below degenerates harmlessly.
        pool = [chess.STARTING_FEN] * max(1, num_positions + offset)
        print("--start-pos: every game starts from the standard starting position")
    else:
        pool = list(load_fens(FEN_FILE))
        print(f'Loaded {FEN_FILE}\nTotal Positions Loaded: {len(pool):,}')
    import random as _r
    (_r.Random(SUBSET_SEED) if SUBSET_SEED is not None else _r).shuffle(pool)
    n = max(1, min(num_positions, len(pool)))
    fens = pool[offset:offset + n]
    if not fens:                       # offset past the end -> nothing to play
        print(f"ERROR: offset {offset} leaves no positions (pool size {len(pool)})")
        return
    total_games = len(fens) * 2

    ctx = mp.get_context("spawn")
    # Sequential mode keeps a single (e1, e2) pair on the main process. Parallel
    # mode uses stubs main-side (only for naming in logs / progress) and spawns
    # one real engine pair per worker.
    parallel = n_workers > 1
    if parallel:
        e1 = _EngineStub(engine1)
        e2 = _EngineStub(engine2)
    else:
        e1 = EngineProcess(ctx, engine1, BOOK_ENGINE1)
        e2 = EngineProcess(ctx, engine2, BOOK_ENGINE2)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = f"{e1.name}_vs_{e2.name}_{stamp}_{os.getpid()}.txt"
    pgn_path = log_path.replace(".txt", ".pgn")
    try:
        fh = open(log_path, "w", encoding="utf-8")
    except Exception as ex:
        print(f"Cannot open log file: {ex}")
        fh = None
    try:
        pgn_fh = open(pgn_path, "w", encoding="utf-8")
    except Exception as ex:
        print(f"Cannot open PGN file: {ex}")
        pgn_fh = None

    impl = getattr(sys, "implementation", None)
    interp = f"{impl.name} {sys.version.split()[0]}" if impl else "python"
    mode_desc = ({"time": f"{TIME_PER_MOVE_MS} ms/move",
                  "depth": f"depth {FIXED_DEPTH}",
                  "clock": f"clock {tc_label}"}[MODE])
    workers_desc = (f"{n_workers} parallel" if parallel else "1 sequential")
    banner = (f"Match: {e1.name}  vs  {e2.name}\n"
              f"Interpreter: {interp}\n"
              f"Mode: {mode_desc}   |   "
              f"{len(fens)} positions x 2 colours = {total_games} games\n"
              f"Workers: {workers_desc}\n"
              f"Log: {log_path}\n" + "-" * 72)
    print(banner)
    if fh is not None:
        fh.write(f"{e1.name} vs {e2.name}\n"
                 f"Interpreter: {interp}\nMode: {mode_desc}\n"
                 f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fh.flush()

    tally = {"e1": 0, "e2": 0, "draws": 0, "errors": 0, "completed": 0,
              "penta": {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}, "penta_incomplete": 0}
    start_t = time.time()
    stopped = False
    interrupted = False        # Ctrl-C / SIGTERM: skip the in_q sentinel dance
    _install_signal_handlers()

    # SPRT early-stop state. cfg is None unless --sprt was passed AND sprt.py
    # imported; then the result loop evaluates the LLR as pairs complete and
    # stops the match the moment a bound is crossed.
    if sprt_enable and _sprt is None:
        print("!! --sprt requested but sprt.py could not be imported -- "
              "running the full game budget without early-stop.")
    sprt_cfg = None
    if sprt_enable and _sprt is not None:
        sprt_cfg = {"elo0": sprt_elo0, "elo1": sprt_elo1,
                    "alpha": sprt_alpha, "beta": sprt_beta, "model": sprt_model}
        lo, hi = _sprt.bounds(sprt_alpha, sprt_beta)
        print(f"SPRT early-stop ON: [{sprt_elo0:g}, {sprt_elo1:g}] "
              f"{sprt_model}, alpha={sprt_alpha:g} beta={sprt_beta:g} "
              f"(bounds {lo:+.3f} .. {hi:+.3f}); stops as soon as a bound is "
              f"crossed, else runs the full {total_games:,}-game budget.")
    # llr/lower/upper/decision refreshed per completed pair; last_n throttles.
    sprt_state = {"cfg": sprt_cfg, "llr": None, "lower": None, "upper": None,
                  "decision": None, "decided": None, "last_n": 0}

    # Build schedule of (round_no, fen, white_is_e1) tuples once -- same in both
    # paths so each position is played once with E1 White and once with E2 White.
    schedule = []
    rno = 0
    for fen in fens:
        rno += 1
        schedule.append((rno, fen, True))       # E1 White
        rno += 1
        schedule.append((rno, fen, False))      # E2 White

    # --- live ETA status line, pinned to the bottom of the terminal --------
    # Completed-game lines scroll up normally; one status line ("how long
    # until all games finish") is redrawn in place below them on every game.
    _is_tty = sys.stdout.isatty()
    eta_state = {"first_t": None, "first_done": 0, "shown": False}

    def _fmt_dur(secs):
        secs = max(0, int(secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _status_text():
        played = tally["completed"] + tally["errors"]
        remaining = total_games - played
        elapsed = time.time() - start_t
        # Rate is measured from the FIRST completed game onward so the ~100 s
        # worker-init phase (during which nothing completes) doesn't inflate
        # the ETA. first_t/first_done are latched on the first call.
        if eta_state["first_t"] is None:
            eta_state["first_t"] = time.time()
            eta_state["first_done"] = played
        since = played - eta_state["first_done"]
        dt = time.time() - eta_state["first_t"]
        rate = None
        if since > 0 and dt > 0:
            rate = since / dt                       # games per second
            eta_s = _fmt_dur(remaining / rate)
            rate_s = f"{rate * 60:.2f} Games per min"
        else:
            eta_s, rate_s = "estimating...", "--"
        pct = 100 * played / total_games if total_games else 0
        base = (f">> {played}/{total_games} ({pct:.2f}%)  |  "
                f"elapsed {_fmt_dur(elapsed)}  |  ETA {eta_s}  |  {rate_s}")
        # --- SPRT segment: current LLR + a projected early-stop ETA ---------
        # The ETA above is the WORST case (full game budget); the SPRT will
        # usually stop sooner. LLR grows ~linearly in pairs while the score
        # holds, so pairs-to-bound ~= n_pairs * bound / LLR -- a rough live
        # projection of when (and which way) the test will decide.
        if sprt_state["cfg"] is not None and sprt_state["llr"] is not None:
            L = sprt_state["llr"]
            lo_b, hi_b = sprt_state["lower"], sprt_state["upper"]
            seg = f"  |  SPRT LLR {L:+.2f} [{lo_b:+.2f}, {hi_b:+.2f}]"
            if sprt_state["decided"]:
                seg += " -> DECIDED"
            else:
                n_pairs = sum(tally["penta"].values())
                if abs(L) > 1e-6 and n_pairs > 0:
                    bound = hi_b if L > 0 else lo_b
                    proj_pairs = n_pairs * bound / L      # same sign as L
                    side = "accept" if L > 0 else "reject"
                    if n_pairs < proj_pairs <= total_games / 2:
                        rem_games = max(0.0, proj_pairs * 2 - played)
                        proj = (_fmt_dur(rem_games / rate) if rate else "…")
                        seg += f" -> ~{proj} to {side}"
                    else:
                        seg += " -> runs to budget"
            base += seg
        return base

    def _draw_status():
        """Redraw the pinned status line (TTY only)."""
        if not _is_tty:
            return
        sys.stdout.write("\r\033[K" + _status_text())
        sys.stdout.flush()
        eta_state["shown"] = True

    def _clear_status():
        """Wipe the pinned status line so the next print lands on a clean row."""
        if _is_tty and eta_state["shown"]:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            eta_state["shown"] = False

    # --- log reorder buffer ------------------------------------------------
    # In parallel mode games finish out of schedule order (worker 7's game can
    # land before worker 2's), so writing each result to the log/PGN as it
    # arrives leaves "=== Game N ===" blocks jumbled. Buffer completed games by
    # round number and emit only the contiguous prefix, so the FILES stay in
    # strict round order (1, 2, 3, ...). Rounds are contiguous 1..total_games
    # (see schedule build), so next_round just steps by 1. The terminal line
    # below is unaffected -- it still prints immediately on completion.
    log_buf = {"pending": {}, "next": 1}

    def _flush_log_inorder():
        while log_buf["next"] in log_buf["pending"]:
            gg = log_buf["pending"].pop(log_buf["next"])
            write_game_block(fh, pgn_fh, gg, e1, mode_cfg, tc_label, tpm)
            log_buf["next"] += 1

    def _flush_log_remainder():
        # On a clean finish the buffer is empty; on Ctrl-C some early round may
        # never have arrived, so emit whatever is left in sorted order rather
        # than silently dropping completed games.
        for rnd in sorted(log_buf["pending"]):
            write_game_block(fh, pgn_fh, log_buf["pending"].pop(rnd),
                             e1, mode_cfg, tc_label, tpm)

    # --- pentanomial pair buffer --------------------------------------------
    # Rounds 2k+1/2k+2 share a FEN (see schedule build above) -- that's the
    # "pair". The two halves can finish in either order or on different
    # workers, so buffer by pair id until both arrive, then bucket the pair's
    # combined E1 score. _MISSING (not None) marks "not arrived yet", since a
    # real per-game score of None ("errored/excluded") is a valid, different
    # state that must still mark the pair incomplete once both halves are in.
    _MISSING = object()
    pair_buf = {}

    def _accumulate_pair(g):
        pair_id = (g["round"] - 1) // 2
        slot_idx = (g["round"] - 1) % 2
        slot = pair_buf.setdefault(pair_id, [_MISSING, _MISSING])
        slot[slot_idx] = game_score_e1(g, e1)
        if _MISSING in slot:
            return                       # still waiting on the other half
        del pair_buf[pair_id]
        s0, s1 = slot
        if s0 is None or s1 is None:     # one (or both) games errored/excluded
            tally["penta_incomplete"] += 1
            return
        tally["penta"][pentanomial_bucket(s0, s1)] += 1

    def _update_sprt():
        """Refresh the SPRT LLR from the live pentanomial counts and latch a
        decision when a bound is crossed. Cheap (a 5-bucket GSPRT), re-run
        only when a NEW pair has completed and we're past the minimum sample.
        Any stats hiccup is swallowed -- the match must never die on it."""
        cfg = sprt_state["cfg"]
        if cfg is None or sprt_state["decided"] is not None:
            return
        n_pairs = sum(tally["penta"].values())
        if n_pairs < SPRT_MIN_PAIRS or n_pairs == sprt_state["last_n"]:
            return
        sprt_state["last_n"] = n_pairs
        counts = [tally["penta"][k] for k in range(5)]
        try:
            r = _sprt.evaluate(counts, cfg["elo0"], cfg["elo1"],
                               cfg["model"], cfg["alpha"], cfg["beta"])
        except Exception:
            return
        sprt_state["llr"] = r["llr"]
        sprt_state["lower"] = r["lower"]
        sprt_state["upper"] = r["upper"]
        sprt_state["decision"] = r["decision"]
        if r["decision"] != "continue":
            sprt_state["decided"] = r["decision"]

    def handle_result(g, round_no):
        """Buffer game for in-order file write + update tally + print one line."""
        log_buf["pending"][g["round"]] = g
        _flush_log_inorder()
        _accumulate_pair(g)
        if g["error"] is not None:
            tally["errors"] += 1
            tag = f"ERR ({g['error'][:40]})"
        else:
            tally["completed"] += 1
            if g["winner"] is None:
                tally["draws"] += 1
                tag = f"draw  {g['reason']}"
            elif g["winner"] is e1:
                tally["e1"] += 1
                tag = f"{e1.name} wins  {g['reason']}"
            else:
                tally["e2"] += 1
                tag = f"{e2.name} wins  {g['reason']}"
        wn = g["white"].name
        bn = g["black"].name
        if tally["completed"]:
            sc = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
            el, mar = elo(sc, tally["completed"])
            run = (f"{e1.name} {tally['e1']:,}W | {tally['draws']:,} D | "
                   f"{e2.name} {tally['e2']:,}W "
                   f"({100*sc:.2f}%, {el:+.2f} +/-{mar:.1f} Elo)")
        else:
            run = "no scored games yet"
        # The counter increments monotonically with completion order, so it
        # never jumps even when games finish out of schedule order in parallel.
        _update_sprt()             # refresh LLR before the status line redraws
        played = tally["completed"] + tally["errors"]
        line = (f"[{played:>4}/{total_games}] "
                f"{wn}(W) vs {bn}(B)  ->  {g['result']:>7}  {tag:<34} | {run}")
        _clear_status()            # wipe pinned ETA, print game line above it,
        print(line)                #   then redraw the ETA as the new last line
        _draw_status()
        if not _is_tty and played % 500 == 0:
            # No pinned line when output is redirected -> drop an ETA marker
            # into the log every 500 games so progress is still visible.
            print(_status_text())

    workers = []
    in_q = out_q = None
    try:
        if parallel:
            in_q = ctx.Queue()
            out_q = ctx.Queue()
            for _ in range(n_workers):
                # NOT daemon: each worker spawns its own EngineProcess children,
                # and daemonic processes are forbidden from having children.
                # We rely on the explicit shutdown protocol below (None x N on
                # the in_q, then join with a timeout, then terminate) to ensure
                # workers exit cleanly when the match ends or is interrupted.
                w = ctx.Process(
                    target=_worker_loop,
                    args=(in_q, out_q, engine1, engine2, mode_cfg,
                          ENGINE_USE_BOOK, PV_UCI,
                          BOOK_ENGINE1, BOOK_ENGINE2),
                )
                w.start()
                workers.append(w)
            # Feed jobs from a BACKGROUND THREAD, not inline. A
            # multiprocessing.Queue's put() blocks once the queue holds
            # SEM_VALUE_MAX un-consumed items (32767 on macOS): a slot frees
            # only when a worker does in_q.get(). Pushing the whole schedule
            # up front therefore deadlocks the main thread on job #32768 for
            # any run with >32767 games (e.g. 20000 positions = 40000 games)
            # -- it never reaches the result loop, so nothing prints even
            # though the workers are busy. Producing on a side thread lets the
            # main thread drain out_q concurrently, which frees in_q slots and
            # keeps the feeder unblocked. (<=32767-game runs were unaffected,
            # which is why smaller matches "worked".)
            feeder = threading.Thread(
                target=lambda: [in_q.put(job) for job in schedule],
                daemon=True,
            )
            feeder.start()
            for _ in range(len(schedule)):
                # Bounded wait (smp.py's P-03 rule): a worker killed by the
                # OS (OOM / kill -9) puts nothing on the queue -- a bare
                # get() then hangs the match forever. Python-level failures
                # are unaffected (workers catch them and post an error row).
                # Ceiling: this only detects TOTAL worker loss; one dead
                # worker among live ones still stalls the tail of the run
                # (its in-flight game's result never arrives) -- per-job
                # acks if that ever bites in practice.
                while True:
                    try:
                        r = out_q.get(timeout=10.0)
                        break
                    except Empty:
                        if not any(w.is_alive() for w in workers):
                            raise EngineError(
                                "all match workers died -- aborting result "
                                "collection (summary so far still written)")
                g = _unpack_result(r, e1, e2)
                handle_result(g, g["round"])
                if sprt_state["decided"]:
                    # Leave via exception so the finally-block shutdown runs
                    # and feeder.join() (which would block on a still-filling
                    # in_q) is skipped -- the feeder is a daemon, it dies with
                    # the process. (At >32767 games the feeder can fill the
                    # queue before shutdown; not a concern at A/B budgets.)
                    raise _SPRTStop()
            feeder.join()
        else:
            e1.start()
            e2.start()
            for round_no, fen, white_is_e1 in schedule:
                white = e1 if white_is_e1 else e2
                black = e2 if white_is_e1 else e1
                g = play_game(round_no, fen, white, black, e1, mode_cfg)
                handle_result(g, round_no)
                if sprt_state["decided"]:
                    raise _SPRTStop()

    except _SPRTStop:
        stopped = True
        _clear_status()
        dec = sprt_state["decided"]
        verdict = ("ACCEPT H1 -- change is good (ship)" if dec == "H1"
                   else "ACCEPT H0 -- change rejected")
        print(f"\n[SPRT decided: {verdict} @ "
              f"{sum(tally['penta'].values()):,} pairs, "
              f"LLR {sprt_state['llr']:+.3f} -- stopping early]")
    except KeyboardInterrupt:
        stopped = True
        interrupted = True
        _clear_status()
        why = f"{_signal_name} received" if _signal_name else "interrupted"
        print(f"\n[{why} -- writing summary so far]")
    except EngineError as ex:
        stopped = True
        _clear_status()
        print(f"\nENGINE LOAD/RUN ERROR: {ex}")
    finally:
        # The summary IS the point of the run -- a second Ctrl-C while it is
        # being written (or while workers wind down) must not throw away hours
        # of games. Everything below is bounded (~7s worst case), so refusing
        # to be interrupted here cannot hang the process.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        _clear_status()        # drop the pinned ETA line before the summary
        _flush_log_remainder()  # emit any games still held by the reorder buffer
        write_summary(fh, e1, e2, tally, total_games, start_t, stopped,
                      n_workers=n_workers, sprt_info=sprt_state)
        if parallel:
            _shutdown_workers(workers, in_q, out_q, graceful=not interrupted)
        else:
            for eng in (e1, e2):
                if hasattr(eng, "kill"):
                    eng.kill()
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if pgn_fh is not None:
            try:
                pgn_fh.close()
            except Exception:
                pass
        print(f"\nLog written to: {log_path}")
        print(f"PGN written to: {pgn_path}")


if __name__ == "__main__":
    mp.freeze_support()
    main()
