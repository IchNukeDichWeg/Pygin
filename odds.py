"""
odds.py
=======
Headless engine-vs-engine **odds match** runner. Plays N games with both
**material odds** (one side starts down pieces) and/or **time odds** (each
engine has its own time/depth/clock policy). Colors alternate game-by-game --
the same engine keeps giving the odds, and when its color flips the removed
pieces mirror to the other side automatically (d1 -> d8, etc.) so the handicap
stays consistent across the match.

Streams progress + every move's eval to the terminal; writes a full per-game
log (``<e1>_vs_<e2>_<stamp>_<pid>.txt``) and a PGN file (``.pgn``) of every
game played, same convention as ``match.py``. Final summary includes the Elo
estimate from Engine 1's perspective.

Run::

    python3 odds.py            # that's it -- everything is CONFIG below

Parallelism, opponent strength and engine threading are plain config
variables (``N_WORKERS``, ``STOCKFISH_ELO``, ``ENGINE_SMP``) -- no shell
loops or env vars needed. Each worker process loads its own engine pair
once and plays its share of the games; results stream back to one log/PGN.
"""

# ====================================================================== #
#  CONFIG  -- edit these
# ====================================================================== #

# --- Engines ---------------------------------------------------------- #
ENGINE_1_PATH = "cengine.py"            # the live C core; "engine.py" = v30
ENGINE_2_PATH = "stockfish_engine.py"

# Engine 1 plays this color in GAME 1; colors alternate every game after.
ENGINE_1_PLAYS_FIRST = "white"          # "white" | "black"

# --- Match length & parallelism --------------------------------------- #
NUM_GAMES = 400       # TOTAL games (400 -> ~±35 Elo CI: enough to SEE a
                      # step on the rook-odds line, per the v30-era runs)
N_WORKERS = 10        # parallel games (worker processes); 1 = sequential.
                      # Keep N_WORKERS * ENGINE_SMP <= CPU cores.

# --- Opponent strength / engine threading (was env vars) --------------- #
STOCKFISH_ELO = 0     # stockfish_engine.py strength: 0 = FULL strength,
                      # otherwise a UCI_Elo cap (clamped to 1320..3190)
ENGINE_SMP = 1        # engine.py SMP workers per game (1 = single-thread)

# --- Material odds ---------------------------------------------------- #
# Which engine GIVES the odds -- i.e. starts the game down the material listed
# in ODDS_SQUARES below. Reads as plain English: "Engine 2 gives queen odds."
#   "engine_1" -> Engine 1 starts down material every game
#   "engine_2" -> Engine 2 starts down material every game
#   "none"     -> even match (ODDS_SQUARES is ignored)
# When colors alternate game-to-game, the same engine keeps giving the odds;
# the squares below mirror to the other side automatically so the handicap
# stays on the right pieces.
ODDS_GIVEN_BY = "engine_2"

# Squares whose pieces are removed from the engine that GIVES odds. ALWAYS
# written from White's side (rank 1/2); if that engine is actually playing
# Black they are vertically mirrored automatically (d1 -> d8, f2 -> f7, ...).
# So ``["d1"]`` means "queen odds" regardless of which colour gives them.
#
# Presets (uncomment ONE):
# ODDS_SQUARES = ["d1"]                 # Queen odds       (Q on d1, saturated)
# ODDS_SQUARES = ["a1"]                 # Rook odds        (Ra1, saturated @v31)
ODDS_SQUARES = ["b1"]                   # Knight odds      (Nb1) -- the standing
                                        # external yardstick; v31 baseline
                                        # 76.75% (400g @45+0.15, +207 +/-48)
# ODDS_SQUARES = ["f1"]                 # Bishop odds      (Bf1)
# ODDS_SQUARES = ["f2"]                 # Pawn odds        (f-pawn)
# ODDS_SQUARES = ["d1", "a1"]           # Queen + Rook odds
# ODDS_SQUARES = ["b1", "g1"]           # Two-knight odds
# ODDS_SQUARES = []                     # no material odds

# --- Time policy per engine (TIME ODDS via different settings) -------- #
# Each engine independently picks a mode:
#   "time"  -> fixed milliseconds per move  (uses ENGINE_<N>_TIME_MS)
#   "depth" -> fixed search depth in plies   (uses ENGINE_<N>_DEPTH)
#   "clock" -> real clock per side + increment, dynamic per-move budget from
#              time_manager.calculate_move_time (uses CLOCK_SECONDS / INCREMENT)
# To give TIME ODDS, set the two engines to different settings -- e.g.
# Engine 1 at 2000 ms/move vs Engine 2 at 500 ms/move, or one on a 5-minute
# clock vs the other on a 1-minute clock. Modes can differ across engines.
# Clock state is reset to CLOCK_SECONDS at the start of every game.

ENGINE_1_MODE             = "clock"
ENGINE_1_TIME_MS          = 1000
ENGINE_1_DEPTH            = 6
ENGINE_1_CLOCK_SECONDS    = 45        # 45s + 0.15s, same TC as the SF-2450 match
ENGINE_1_CLOCK_INCREMENT  = 0.15

ENGINE_2_MODE             = "clock"
ENGINE_2_TIME_MS          = 500
ENGINE_2_DEPTH            = 6
ENGINE_2_CLOCK_SECONDS    = 45
ENGINE_2_CLOCK_INCREMENT  = 0.15

# --- Output ----------------------------------------------------------- #
SHOW_BOARD            = False           # print the board after each move in terminal
SHOW_ENGINE_INFO      = True            # print depth/score/nodes after each move
USE_UNICODE_PIECES    = True            # False -> ASCII letters (KQRBNP / kqrbnp)
PERSPECTIVE           = "white"         # board orientation: "white" | "black"
VERBOSE_MOVES         = False           # mirror every move to the terminal
                                        # (per-move info is ALWAYS written to log)

# --- Misc ------------------------------------------------------------- #
ENGINE_USE_BOOK       = False           # disable opening books for an odds match
MAX_PLIES             = 300             # hard cap -> adjudicated draw (odds games can grind)
MAX_DEPTH_CAP         = 50              # safety cap on timed-search depth

# ====================================================================== #
import datetime
import importlib.util
import io
import math
import multiprocessing
import os
import sys
import time

import chess
import chess.pgn

from time_manager import calculate_move_time


# ---------------------------------------------------------------------- #
# Config overrides via environment. `spawn` workers re-import this module,
# so the ONLY config a child sees is what's in its inherited environment
# (same trick odds.py already uses for STOCKFISH_ELO / CLAUDECHESS_SMP).
# The web dashboard passes CLI flags -> main() writes them here -> this runs
# at import in every child. Unset vars keep the literal defaults above.
# ---------------------------------------------------------------------- #
def _apply_env_config():
    global NUM_GAMES, N_WORKERS, STOCKFISH_ELO, ENGINE_SMP
    global ENGINE_1_PATH, ENGINE_2_PATH, ODDS_GIVEN_BY, ODDS_SQUARES
    global ENGINE_1_CLOCK_SECONDS, ENGINE_1_CLOCK_INCREMENT
    global ENGINE_2_CLOCK_SECONDS, ENGINE_2_CLOCK_INCREMENT
    g = os.environ.get
    NUM_GAMES = int(g("ODDS_NUM_GAMES", NUM_GAMES))
    N_WORKERS = int(g("ODDS_WORKERS", N_WORKERS))
    STOCKFISH_ELO = int(g("STOCKFISH_ELO", STOCKFISH_ELO))
    ENGINE_SMP = int(g("CLAUDECHESS_SMP", ENGINE_SMP))
    ENGINE_1_PATH = g("ODDS_ENGINE1", ENGINE_1_PATH)
    ENGINE_2_PATH = g("ODDS_ENGINE2", ENGINE_2_PATH)
    ODDS_GIVEN_BY = g("ODDS_GIVEN_BY", ODDS_GIVEN_BY)
    sq = g("ODDS_SQUARES")
    if sq is not None:
        sq = "" if sq.strip().lower() == "none" else sq
        ODDS_SQUARES = [s.strip() for s in sq.split(",") if s.strip()]
    secs = g("ODDS_TC_SECONDS")
    if secs is not None:
        ENGINE_1_CLOCK_SECONDS = ENGINE_2_CLOCK_SECONDS = float(secs)
    inc = g("ODDS_TC_INC")
    if inc is not None:
        ENGINE_1_CLOCK_INCREMENT = ENGINE_2_CLOCK_INCREMENT = float(inc)


_apply_env_config()


# ---------------------------------------------------------------------- #
# Engine loading
# ---------------------------------------------------------------------- #
def load_engine(path, tag):
    # Unique spec name per (path, tag) so both engines stay distinct even when
    # ENGINE_1_PATH == ENGINE_2_PATH (self-play).
    spec = importlib.util.spec_from_file_location(
        f"odds_engine_{tag}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "Engine"):
        raise AttributeError(f"{path!r} does not define an `Engine` class")
    eng = module.Engine()
    try:
        eng.use_book = ENGINE_USE_BOOK
    except Exception:
        pass
    try:
        eng.pv_uci = False              # SAN PV is friendlier in the log
    except Exception:
        pass
    return eng


# ---------------------------------------------------------------------- #
# Material odds setup
# ---------------------------------------------------------------------- #
def apply_odds(board, color, squares):
    """Remove the listed squares' pieces from ``color`` on ``board``.

    Squares are written from White's side; they're vertically mirrored when
    Black is the giver, so the same preset works no matter which color is
    giving odds (and on alternate games when colors swap). Drops castling
    rights covering any removed rook/king. Mutates board. Returns a list of
    (original_square_label, piece) for the header.
    """
    removed = []
    for s in squares:
        try:
            sq = chess.parse_square(s)
        except Exception:
            print(f"  (ignoring bad square {s!r})", file=sys.stderr)
            continue
        if color == chess.BLACK:
            sq ^= 56                    # vertical flip onto Black's side
        piece = board.piece_at(sq)
        if piece is None or piece.color != color:
            continue                    # the square is empty for this colour
        board.remove_piece_at(sq)
        removed.append((s, piece))
    # Drop any castling right whose rook (or king) is now gone.
    fen = board.fen().split()
    valid = ""
    for ch in fen[2]:
        rook_sq, king_sq = {
            "K": (chess.H1, chess.E1), "Q": (chess.A1, chess.E1),
            "k": (chess.H8, chess.E8), "q": (chess.A8, chess.E8),
        }.get(ch, (None, None))
        if rook_sq is None:
            continue
        rook = board.piece_at(rook_sq)
        king = board.piece_at(king_sq)
        if (rook and rook.piece_type == chess.ROOK
                and king and king.piece_type == chess.KING
                and rook.color == king.color):
            valid += ch
    fen[2] = valid or "-"
    board.set_fen(" ".join(fen))
    return removed


def odds_label(removed):
    if not removed:
        return "no material odds"
    names = {chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
             chess.ROOK: "R", chess.QUEEN: "Q", chess.KING: "K"}
    parts = [f"{names[p.piece_type]}{sq}" for sq, p in removed]
    kinds = {p.piece_type for _, p in removed}
    if len(removed) == 1:
        kind = next(iter(kinds))
        kind_name = {chess.QUEEN: "Queen", chess.ROOK: "Rook",
                     chess.BISHOP: "Bishop", chess.KNIGHT: "Knight",
                     chess.PAWN: "Pawn", chess.KING: "King"}[kind]
        return f"{kind_name} odds ({parts[0]})"
    return f"Odds ({', '.join(parts)})"


# ---------------------------------------------------------------------- #
# Board rendering (terminal)
# ---------------------------------------------------------------------- #
UNICODE = {
    "K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
    "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟",
}


def render_board(board, perspective_white=True, last_move=None):
    files = "abcdefgh"
    last_sqs = {last_move.from_square, last_move.to_square} if last_move else set()
    rows = range(7, -1, -1) if perspective_white else range(0, 8)
    cols = range(0, 8) if perspective_white else range(7, -1, -1)
    out = ["   +" + "---+" * 8]
    for r in rows:
        cells = []
        for f in cols:
            sq = chess.square(f, r)
            p = board.piece_at(sq)
            if p is None:
                cells.append("   ")
            else:
                sym = UNICODE[p.symbol()] if USE_UNICODE_PIECES else p.symbol()
                marker = "*" if sq in last_sqs else " "
                cells.append(f"{marker}{sym} ")
        out.append(f" {r + 1} |" + "|".join(cells) + "|")
        out.append("   +" + "---+" * 8)
    out.append("     " + "   ".join(files[f] for f in cols))
    return "\n".join(out)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def fmt_score_stm(white_cp, stm_white, mate_score, mate_threshold):
    stm = white_cp if stm_white else -white_cp
    if abs(stm) >= mate_threshold:
        plies = mate_score - abs(stm)
        n = (plies + 1) // 2
        return f"#{n if stm > 0 else -n}"
    return f"{stm / 100.0:+.2f}"


def fmt_clock(ms):
    if ms is None:
        return "-"
    s = max(0, ms) / 1000.0
    if s >= 60:
        return f"{int(s) // 60}:{int(s) % 60:02d}"
    return f"{s:.1f}s"


def elo(score, n):
    """Elo difference for a match score in [0,1] over n games, with a rough
    95% margin. Returns (elo, margin). Mirrors match.py exactly."""
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


def fmt_duration(seconds):
    ms = max(0, int(round(seconds * 1000)))
    d, ms = divmod(ms, 86_400_000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{d}d {h}h {m}m {s}s {ms}ms"


def describe_policy(mode, time_ms, depth, clock_sec, clock_inc_s):
    """Human-readable policy string; usable without loading an engine."""
    if mode == "time":
        return f"{int(time_ms)} ms/move"
    if mode == "depth":
        return f"fixed depth {int(depth)}"
    s = int(float(clock_sec))
    return f"clock {s // 60}:{s % 60:02d} + {clock_inc_s:g}s"


# ---------------------------------------------------------------------- #
# Per-engine policy: holds an engine, its mode settings and (optional) clock.
# ---------------------------------------------------------------------- #
class EnginePolicy:
    def __init__(self, path, mode, time_ms, depth, clock_sec, clock_inc, tag):
        if mode not in ("time", "depth", "clock"):
            raise ValueError(f"{tag}: bad mode {mode!r}")
        self.path = path
        self.mode = mode
        self.time_ms = int(time_ms)
        self.depth = int(depth)
        self.clock_seconds = float(clock_sec)
        self.clock_inc_ms = int(clock_inc * 1000)
        self.clock_ms = None
        self.clock_started = False
        self.engine = load_engine(path, tag)
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.tag = tag                  # "engine_1" or "engine_2"

    def describe(self):
        return describe_policy(self.mode, self.time_ms, self.depth,
                               self.clock_seconds, self.clock_inc_ms / 1000.0)

    def reset_clock(self):
        """Initialise the clock for a fresh game (clock mode only)."""
        if self.mode == "clock":
            self.clock_ms = int(self.clock_seconds * 1000)
        else:
            self.clock_ms = None
        self.clock_started = False

    def next_request(self, board, opp_clock_ms):
        if self.mode == "depth":
            return "depth", self.depth
        if self.mode == "time":
            return "time", self.time_ms
        budget_ms = calculate_move_time(
            board, self.clock_ms, opp_clock_ms or 0, self.clock_inc_ms)
        return "time", budget_ms

    def commit_clock(self, used_ms):
        if self.mode != "clock":
            return False
        if not self.clock_started:
            self.clock_started = True   # first move of the game is "free"
            return False
        self.clock_ms -= int(used_ms)
        if self.clock_ms < 0:
            return True
        self.clock_ms += self.clock_inc_ms
        return False


# ---------------------------------------------------------------------- #
# One game
# ---------------------------------------------------------------------- #
def play_game(round_no, p_white, p_black, odds_giver, mate_score,
              mate_threshold, perspective_white):
    """Play a single game between two engine policies.

    Returns a dict: round, white, black, result, reason, error, winner (the
    EnginePolicy that won or None), board, start_fen, move_log (list of
    per-move "[name] move ...: info" strings for the log file).
    """
    board = chess.Board()
    # Apply material odds for THIS game -- since which engine is White can
    # change game-to-game, the giver's *current* colour is used here, and
    # apply_odds mirrors the squares onto the right side automatically.
    odds_color = None
    if odds_giver is not None:
        odds_color = chess.WHITE if odds_giver is p_white else chess.BLACK
    removed = apply_odds(board, odds_color, ODDS_SQUARES) if odds_color is not None else []
    start_fen = board.fen()

    p_white.reset_clock()
    p_black.reset_clock()

    move_log = []
    last_move = None
    result, reason, error, winner = "*", "", None, None

    if VERBOSE_MOVES:
        print(f"  [Game {round_no}] {odds_label(removed)} -- "
              f"given by {odds_giver.name if odds_giver else 'nobody'}")
        print(f"  WHITE: {p_white.name} ({p_white.describe()})   "
              f"BLACK: {p_black.name} ({p_black.describe()})")
        print(f"  FEN: {start_fen}")

    while True:
        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            result, reason = outcome.result(), outcome.termination.name
            break
        if board.ply() >= MAX_PLIES:
            result, reason = "1/2-1/2", "MAX_PLIES (adjudicated draw)"
            break

        mover = p_white if board.turn == chess.WHITE else p_black
        opp = p_black if mover is p_white else p_white
        side = "WHITE" if board.turn == chess.WHITE else "BLACK"

        req_mode, req_value = mover.next_request(
            board, opp.clock_ms if opp.mode == "clock" else None)

        t0 = time.time()
        try:
            if req_mode == "time":
                move = mover.engine.get_best_move_timed(
                    board, req_value / 1000.0, MAX_DEPTH_CAP)
            else:
                move = mover.engine.get_best_move(board, int(req_value))
        except Exception as ex:
            error = f"{mover.name} crashed: {ex}"
            result = "0-1" if mover is p_white else "1-0"
            reason = "ENGINE_ERROR"
            break
        elapsed = time.time() - t0
        used_ms = int(elapsed * 1000)

        if move is None or move not in board.legal_moves:
            error = f"{mover.name} returned no legal move ({move!r})"
            result = "0-1" if mover is p_white else "1-0"
            reason = "NO_LEGAL_MOVE"
            break

        if mover.commit_clock(used_ms):
            result = "0-1" if mover is p_white else "1-0"
            reason = "TIME_FORFEIT"
            break

        depth = int(getattr(mover.engine, "last_depth", 0) or 0)
        nodes = int(getattr(mover.engine, "nodes_searched", 0) or 0)
        white_cp = int(getattr(mover.engine, "last_score", 0) or 0)
        score = fmt_score_stm(white_cp, side == "WHITE",
                              mate_score, mate_threshold)
        pv = str(getattr(mover.engine, "last_pv", "") or "")
        nps = int(nodes / elapsed) if elapsed > 0 else 0

        san = board.san(move)
        info = (f"depth {depth} score {score} nodes {nodes} "
                f"nps {nps} time {used_ms}")
        move_log.append(f"[{mover.name}] move {san}: {info}")
        if pv:
            move_log.append(f"    PV: {pv}")

        board.push(move)
        last_move = move

        if VERBOSE_MOVES:
            clk = ""
            if p_white.mode == "clock" or p_black.mode == "clock":
                clk = (f"  [W {fmt_clock(p_white.clock_ms)}"
                       f" | B {fmt_clock(p_black.clock_ms)}]")
            print(f"      {mover.name:>14} {san:7} "
                  f"[d{depth} {score} {nodes:,}n {nps:,}nps {used_ms}ms]{clk}")
        if SHOW_BOARD:
            print(render_board(board, perspective_white, last_move))

    if error is None and result in ("1-0", "0-1"):
        winner = p_white if result == "1-0" else p_black
    return {
        "round": round_no, "white": p_white, "black": p_black,
        "result": result, "reason": reason, "error": error,
        "winner": winner, "board": board, "start_fen": start_fen,
        "move_log": move_log, "removed": removed, "odds_giver": odds_giver,
    }


# ---------------------------------------------------------------------- #
# PGN
# ---------------------------------------------------------------------- #
def build_pgn(g, round_no, p1, p2):
    game = chess.pgn.Game()
    game.setup(chess.Board(g["start_fen"]))
    game.headers["Event"] = "Odds Match"
    game.headers["Site"] = "Local"
    game.headers["Date"] = datetime.date.today().strftime("%Y.%m.%d")
    game.headers["Round"] = str(round_no)
    game.headers["White"] = g["white"].name
    game.headers["Black"] = g["black"].name
    game.headers["WhiteSettings"] = g["white"].describe()
    game.headers["BlackSettings"] = g["black"].describe()
    game.headers["FEN"] = g["start_fen"]
    game.headers["SetUp"] = "1"
    game.headers["Result"] = g["result"]
    game.headers["Odds"] = odds_label(g["removed"]) + (
        f" given by {g['odds_giver'].name}" if g["odds_giver"] else "")
    node = game
    for mv in g["board"].move_stack:
        node = node.add_variation(mv)
    exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                         comments=False)
    return game.accept(exporter).strip()


# ---------------------------------------------------------------------- #
# Per-game block in the .txt log (matches match.py's layout)
# ---------------------------------------------------------------------- #
def write_game_block(fh, pgn_fh, g, p1, p2, pgn_str):
    if fh is not None:
        white, black = g["white"], g["black"]
        wlab = "Engine 1" if white is p1 else "Engine 2"
        blab = "Engine 1" if black is p1 else "Engine 2"
        out = [f"=== Game {g['round']} ===", f"FEN: {g['start_fen']}",
               f"{wlab} (White): {white.path}    [{white.describe()}]",
               f"{blab} (Black): {black.path}    [{black.describe()}]"]
        if g["odds_giver"] is not None:
            out.append(f"Odds: {odds_label(g['removed'])}  "
                       f"-- given by {g['odds_giver'].name}")
        else:
            out.append("Odds: none")
        if g["error"]:
            out.append(f"Outcome: ERROR -- {g['error']}")
        elif g["result"] == "1/2-1/2":
            out.append(f"Outcome: draw ({g['reason']})")
        elif g["winner"] is not None:
            wl = "Engine 1" if g["winner"] is p1 else "Engine 2"
            wc = "White" if g["winner"] is white else "Black"
            out.append(f"Outcome: {g['winner'].name} ({wl}, {wc}) won "
                       f"-- {g['result']} ({g['reason']})")
        else:
            out.append(f"Outcome: {g['result']} ({g['reason']})")
        out.append("--- Engine Logs ---")
        out.extend(g["move_log"] if g["move_log"] else ["(no moves played)"])
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


def write_summary(fh, p1_name, desc1, p2_name, desc2, tally,
                  total_games, start_t, stopped):
    lines = ["", "=== ODDS MATCH SUMMARY ===",
             f"Engine 1: {p1_name}  [{desc1}]",
             f"Engine 2: {p2_name}  [{desc2}]",
             f"Games scored: {tally['completed']:,}  (of {total_games:,} scheduled)",
             f"Engine 1 Wins: {tally['e1']:,}",
             f"Engine 2 Wins: {tally['e2']:,}",
             f"Draws: {tally['draws']:,}"]
    if int(tally['errors']) > 0:
        lines.append(f"Errors (excluded): {tally['errors']:,}")
    if tally["completed"]:
        score = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
        el, margin = elo(score, tally["completed"])
        lines.append(
            f"Engine 1 score: {tally['e1'] + 0.5*tally['draws']:.2f}"
            f"/{tally['completed']} ({100*score:.2f}%)  =>  "
            f"{el:+.2f} +/- {margin:.1f} Elo")
        lines.append(f"Raw Elo (point estimate): {el:.2f}")
    if stopped:
        lines.append("(match was stopped before completion)")
    if start_t is not None:
        elapsed = time.time() - start_t
        played = tally["completed"] + tally["errors"]
        per = fmt_duration(elapsed / played) if played else "-"
        lines += [f"Duration: {fmt_duration(elapsed)}   (per game: {per})",
                  "",
                  f"Ended:    {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    text = "\n".join(lines)
    print("\n" + text)
    if fh is not None:
        try:
            fh.write(text + "\n")
            fh.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# Worker processes: each builds its engine pair ONCE, then plays whatever
# game numbers the pool hands it. Module-level functions (macOS spawn
# re-imports this file in each child; main() is __main__-guarded below).
# ---------------------------------------------------------------------- #
_W = {}                                  # per-process engines/state


def _init_worker():
    p1 = EnginePolicy(ENGINE_1_PATH, ENGINE_1_MODE, ENGINE_1_TIME_MS,
                      ENGINE_1_DEPTH, ENGINE_1_CLOCK_SECONDS,
                      ENGINE_1_CLOCK_INCREMENT, "engine_1")
    p2 = EnginePolicy(ENGINE_2_PATH, ENGINE_2_MODE, ENGINE_2_TIME_MS,
                      ENGINE_2_DEPTH, ENGINE_2_CLOCK_SECONDS,
                      ENGINE_2_CLOCK_INCREMENT, "engine_2")
    _W["p1"], _W["p2"] = p1, p2
    _W["giver"] = {"engine_1": p1, "engine_2": p2}.get(ODDS_GIVEN_BY)
    _W["mate"] = int(getattr(p1.engine, "MATE_SCORE",
                             getattr(p2.engine, "MATE_SCORE", 1_000_000)))
    _W["thr"] = int(getattr(p1.engine, "MATE_THRESHOLD", _W["mate"] - 1_000))


def _play_one(round_no):
    """Play one game; return picklable strings only (no Board/Engine)."""
    p1, p2 = _W["p1"], _W["p2"]
    p1_white_first = (ENGINE_1_PLAYS_FIRST == "white")
    p1_is_white = p1_white_first if (round_no % 2 == 1) else not p1_white_first
    p_white = p1 if p1_is_white else p2
    p_black = p2 if p1_is_white else p1
    g = play_game(round_no, p_white, p_black, _W["giver"],
                  _W["mate"], _W["thr"], PERSPECTIVE == "white")
    pgn_str = build_pgn(g, round_no, p1, p2)
    buf = io.StringIO()
    write_game_block(buf, None, g, p1, p2, pgn_str)
    if g["error"] is not None:
        code, tag = "err", f"ERR ({g['error'][:48]})"
    elif g["winner"] is None:
        code, tag = "draw", f"draw  {g['reason']}"
    elif g["winner"] is p1:
        code, tag = "e1", f"{p1.name} wins  {g['reason']}"
    else:
        code, tag = "e2", f"{p2.name} wins  {g['reason']}"
    return (round_no, code, tag, g["result"],
            g["white"].name, g["black"].name, buf.getvalue(), pgn_str)


# ---------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------- #
def main():
    # Optional CLI overrides (used by the web dashboard). Each flag maps to an
    # env var so `spawn` workers re-importing this module pick it up too.
    import argparse
    ap = argparse.ArgumentParser(description="engine vs engine material/time odds match")
    ap.add_argument("--engine1"); ap.add_argument("--engine2")
    ap.add_argument("--num-games", type=int); ap.add_argument("--workers", type=int)
    ap.add_argument("--positions", type=int,
                    help="match.py-style count: each position is played twice "
                         "(once per colour), so total games = positions * 2 "
                         "(overrides --num-games)")
    ap.add_argument("--stockfish-elo", type=int); ap.add_argument("--smp", type=int)
    ap.add_argument("--odds-squares", help="comma list of squares to remove, e.g. d1 (queen) or 'none'")
    ap.add_argument("--odds-given-by", choices=("engine_1", "engine_2", "none"))
    ap.add_argument("--tc-seconds", type=float); ap.add_argument("--tc-inc", type=float)
    a = ap.parse_args()
    if a.positions is not None:
        a.num_games = a.positions * 2   # colour-alternating pairs
    _flag_env = {
        "engine1": "ODDS_ENGINE1", "engine2": "ODDS_ENGINE2",
        "num_games": "ODDS_NUM_GAMES", "workers": "ODDS_WORKERS",
        "stockfish_elo": "STOCKFISH_ELO", "smp": "CLAUDECHESS_SMP",
        "odds_squares": "ODDS_SQUARES", "odds_given_by": "ODDS_GIVEN_BY",
        "tc_seconds": "ODDS_TC_SECONDS", "tc_inc": "ODDS_TC_INC",
    }
    for attr, envk in _flag_env.items():
        v = getattr(a, attr)
        if v is not None:
            os.environ[envk] = str(v)
    _apply_env_config()  # refresh this (parent) process's globals from env

    if ENGINE_1_PLAYS_FIRST not in ("white", "black"):
        print(f"ENGINE_1_PLAYS_FIRST must be 'white' or 'black' "
              f"(got {ENGINE_1_PLAYS_FIRST!r})")
        return
    for p in (ENGINE_1_PATH, ENGINE_2_PATH):
        if not os.path.isfile(p):
            print(f"engine file not found: {p!r}")
            return
    if NUM_GAMES < 1:
        print(f"NUM_GAMES must be >= 1 (got {NUM_GAMES})")
        return

    if ODDS_GIVEN_BY not in ("engine_1", "engine_2", "none"):
        print(f"ODDS_GIVEN_BY must be 'engine_1' | 'engine_2' | 'none' "
              f"(got {ODDS_GIVEN_BY!r})")
        return
    for m in (ENGINE_1_MODE, ENGINE_2_MODE):
        if m not in ("time", "depth", "clock"):
            print(f"bad mode {m!r} (must be 'time' | 'depth' | 'clock')")
            return

    # Config -> environment, BEFORE any engine loads / worker spawns
    # (children inherit; stockfish_engine.py reads these at import time).
    os.environ["STOCKFISH_ELO"] = str(STOCKFISH_ELO)
    os.environ["CLAUDECHESS_SMP"] = str(ENGINE_SMP)

    # Names/descriptions derived from config only -- the parent process
    # never loads engines; each worker builds its own pair in _init_worker.
    p1_name = os.path.splitext(os.path.basename(ENGINE_1_PATH))[0]
    p2_name = os.path.splitext(os.path.basename(ENGINE_2_PATH))[0]
    desc1 = describe_policy(ENGINE_1_MODE, ENGINE_1_TIME_MS, ENGINE_1_DEPTH,
                            ENGINE_1_CLOCK_SECONDS, ENGINE_1_CLOCK_INCREMENT)
    desc2 = describe_policy(ENGINE_2_MODE, ENGINE_2_TIME_MS, ENGINE_2_DEPTH,
                            ENGINE_2_CLOCK_SECONDS, ENGINE_2_CLOCK_INCREMENT)
    giver_name = {"engine_1": p1_name, "engine_2": p2_name}.get(ODDS_GIVEN_BY)

    # --- Open the log + PGN files (same naming as match.py) ----------- #
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = f"{p1_name}_vs_{p2_name}_odds_{stamp}_{os.getpid()}.txt"
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
    odds_desc = (f"{odds_label(apply_odds(chess.Board(), chess.WHITE, ODDS_SQUARES))} "
                 f"given by {giver_name}") if giver_name else "none"
    sf_desc = ("FULL strength" if STOCKFISH_ELO <= 0
               else f"UCI_Elo {STOCKFISH_ELO}")

    banner = (f"Odds match: {p1_name}  vs  {p2_name}\n"
              f"Interpreter: {interp}\n"
              f"Engine 1: {desc1}\n"
              f"Engine 2: {desc2}   (STOCKFISH_ELO={STOCKFISH_ELO} -> {sf_desc})\n"
              f"Material odds: {odds_desc}\n"
              f"Games: {NUM_GAMES}   Workers: {N_WORKERS}   ENGINE_SMP: {ENGINE_SMP}\n"
              f"(Engine 1 plays {ENGINE_1_PLAYS_FIRST} in Game 1, colors alternate)\n"
              f"Log: {log_path}\n"
              f"PGN: {pgn_path}\n" + "-" * 72)
    print(banner)
    if fh is not None:
        fh.write(f"{p1_name} vs {p2_name}\n"
                 f"Interpreter: {interp}\n"
                 f"Engine 1: {desc1}   Engine 2: {desc2} ({sf_desc})\n"
                 f"Material odds: {odds_desc}\n"
                 f"Games scheduled: {NUM_GAMES}   Workers: {N_WORKERS}\n"
                 f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fh.flush()

    # --- Run the games (N_WORKERS parallel processes) ------------------ #
    tally = {"e1": 0, "e2": 0, "draws": 0, "errors": 0, "completed": 0}
    start_t = time.time()
    stopped = False
    pool = None

    # --- live progress bar + ETA, pinned to the bottom of the terminal ---- #
    # Same idea as match.py: completed-game lines scroll up; one status line
    # (visual bar + %/ETA/rate) is redrawn in place below them each game.
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
        # Rate measured from the FIRST completed game so the worker-init phase
        # (engines loading, nothing completing) doesn't inflate the ETA.
        if eta_state["first_t"] is None:
            eta_state["first_t"] = time.time()
            eta_state["first_done"] = played
        since = played - eta_state["first_done"]
        dt = time.time() - eta_state["first_t"]
        if since > 0 and dt > 0:
            rate = since / dt
            eta_s = _fmt_dur(remaining / rate)
            rate_s = f"{rate * 60:.2f} games/min"
        else:
            eta_s, rate_s = "estimating...", "--"
        pct = 100 * played / total_games if total_games else 0
        width = 28
        filled = int(width * played / total_games) if total_games else 0
        bar = "#" * filled + "-" * (width - filled)
        return (f">> [{bar}] {played:,}/{total_games:,} ({pct:.2f}%)  |  "
                f"elapsed {_fmt_dur(elapsed)}  |  ETA {eta_s}  |  {rate_s}")

    def _draw_status():
        if not _is_tty:
            return
        sys.stdout.write("\r\033[K" + _status_text())
        sys.stdout.flush()
        eta_state["shown"] = True

    def _clear_status():
        if _is_tty and eta_state["shown"]:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            eta_state["shown"] = False

    total_games = NUM_GAMES
    if N_WORKERS > 1:
        print(f"Spawning {N_WORKERS} workers (each loads its own engine pair)...")
        ctx = multiprocessing.get_context("spawn")
        pool = ctx.Pool(N_WORKERS, initializer=_init_worker)
        results_iter = pool.imap_unordered(_play_one, range(1, NUM_GAMES + 1))
    else:
        print("Loading engines (sequential mode)...")
        _init_worker()
        results_iter = map(_play_one, range(1, NUM_GAMES + 1))

    done = 0
    try:
        for (round_no, code, tag, result, wname, bname,
             block_txt, pgn_str) in results_iter:
            done += 1
            if fh is not None:
                fh.write(block_txt)
                fh.flush()
            if pgn_fh is not None:
                pgn_fh.write(pgn_str + "\n\n")
                pgn_fh.flush()
            if code == "err":
                tally["errors"] += 1
            else:
                tally["completed"] += 1
                tally[{"e1": "e1", "e2": "e2", "draw": "draws"}[code]] += 1

            if tally["completed"]:
                sc = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
                el, mar = elo(sc, tally["completed"])
                run = (f"{p1_name} {tally['e1']:,}W | {tally['draws']:,} D | "
                       f"{p2_name} {tally['e2']:,}W "
                       f"({100*sc:.2f}%, {el:+.2f} +/-{mar:.1f} Elo)")
            else:
                run = "no scored games yet"
            line = (f"[{done:>4}/{NUM_GAMES}] {wname}(W) vs {bname}(B)  ->  "
                    f"{result:>7}  {tag:<34} | {run}")
            _clear_status()            # wipe the pinned bar, print the game
            print(line)                #   line above it, then redraw the bar
            _draw_status()
            if not _is_tty and done % 20 == 0:
                print(_status_text())  # redirected: drop a progress marker
    except KeyboardInterrupt:
        stopped = True
        _clear_status()
        print("\n[interrupted -- writing summary so far]")
    finally:
        _clear_status()
        if pool is not None:
            pool.terminate()
            pool.join()

    write_summary(fh, p1_name, desc1, p2_name, desc2, tally,
                  NUM_GAMES, start_t, stopped)
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
    main()
