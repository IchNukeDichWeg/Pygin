"""
match.py
========
Headless engine-vs-engine match runner -- everything ``engine_battle.py`` does
(subprocess engines with watchdogs, each position played both colours, the same
log-file format and Elo summary) but with NO pygame, so it runs anywhere and,
crucially, under PyPy:

    python3  match.py        # CPython
    pypy3    match.py        # PyPy -> engines ~1.5x faster -> more games/night

Progress is streamed to the terminal; a full per-move/PGN log is written to a
file named like ``<e1>_vs_<e2>_<timestamp>_<pid>.txt`` (same as engine_battle).

Run several copies in parallel for more games (with a fixed SUBSET_SEED they all
draw the SAME positions, so results stay directly comparable / poolable).

Press Ctrl-C to stop early -- the summary (with Elo so far) is still written.
"""

# ====================================================================== #
#  CONFIG  -- edit these
# ====================================================================== #
ENGINE_1 = "engine.py"                       # path to engine 1
ENGINE_2 = "stockfish_engine.py"             # path to engine 2
FEN_FILE = "fen.txt"                   # positions (plain FEN or EPD, one per line)

NUM_POSITIONS = 500          # positions to play; TOTAL GAMES = NUM_POSITIONS * 2
                             #   (each position is played once with each engine as White)

MODE = "time"               # "time"  -> fixed milliseconds per move (TIME_PER_MOVE_MS)
                             # "depth" -> fixed search depth in plies (FIXED_DEPTH)
                             # "clock" -> real clock per side (TC_MINUTES + TC_INCREMENT),
                             #            per-move budget via time_manager.calculate_move_time
TIME_PER_MOVE_MS = 500      # used when MODE == "time"
FIXED_DEPTH = 6              # used when MODE == "depth"
TC_MINUTES = 0.75            # used when MODE == "clock"
TC_INCREMENT = 0.5           # used when MODE == "clock" (seconds added per move)

ENGINE_USE_BOOK = False      # opening books off -> a fair, search-only test
SUBSET_SEED = None         # FIXED so parallel windows shuffle identically -> disjoint shards via [offset]
MAX_PLIES = 200              # games longer than this are adjudicated a draw
VERBOSE_MOVES = False        # also print every move to the terminal
                             #   (per-move info is ALWAYS written to the log file)

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
import sys
import time

import chess
import chess.pgn

from battle_worker import engine_worker
from time_manager import calculate_move_time


# ====================================================================== #
# Engine subprocess handle (parent side) -- ported from engine_battle.py
# ====================================================================== #
class EngineError(Exception):
    """The engine failed to load or raised while searching."""


class EngineTimeout(Exception):
    """The engine did not return a move within the watchdog window."""


class EngineProcess:
    """Owns one engine subprocess and talks to it over a pipe."""

    def __init__(self, ctx, path):
        self.ctx = ctx
        self.path = path
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
            args=(child_conn, self.path, ENGINE_USE_BOOK, PV_UCI),
            daemon=True,
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
                f"{self.name}: no move within {timeout:.1f}s (killed)")
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
    return f"{int(s) // 60}:{int(s) % 60:02d}" if s >= 60 else f"{s:.1f}s"


def build_pgn(round_no, fen, white, black, board, result, now, tc_label, tpm):
    game = chess.pgn.Game()
    game.setup(chess.Board(fen))
    game.headers["Result"] = result   # ensures movetext ends with the correct terminator
    node = game
    for mv in board.move_stack:
        node = node.add_variation(mv)
    exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    movetext = game.accept(exporter).strip() or result
    header = [
        '[Event "Engine Match"]', '[Site "Local"]',
        f'[Date "{now.strftime("%Y.%m.%d")}"]', f'[Round "{round_no}"]',
        f'[White "{white.name}"]', f'[Black "{black.name}"]',
        f'[TimeControl "{tc_label}"]' if tc_label else '',
        f'[Time Per Move "{tpm}ms"]' if tpm is not None else '',
        f'[FEN "{fen}"]', f'[Result "{result}"]',
    ]
    return "\n".join(h for h in header if h) + "\n" + movetext


# ====================================================================== #
# One game
# ====================================================================== #
def play_game(round_no, fen, white, black, e1, mode_cfg):
    """Play a single game. Returns a dict of results + the per-move log lines."""
    board = chess.Board(fen)
    engine_log = []
    error = None
    result = "*"
    reason = ""

    is_clock = (mode_cfg["mode"] == "clock")
    if is_clock:
        init_ms = int(mode_cfg["tc_minutes"] * 60000)
        clocks = {chess.WHITE: init_ms, chess.BLACK: init_ms}
        inc_ms = int(mode_cfg["tc_increment"] * 1000)
    else:
        clocks, inc_ms = None, 0
    clock_started = False

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
            res = mover.request_move(board.fen(), req_mode, req_value, req_timeout)
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


def write_summary(fh, e1, e2, tally, total_games, start_t, stopped):
    lines = ["", "=== BATTLE SUMMARY ===",
             f"Engine 1: {e1.name}", f"Engine 2: {e2.name}",
             f"Games scored: {tally['completed']}  (of {total_games} scheduled)",
             f"Engine 1 Wins: {tally['e1']}", f"Engine 2 Wins: {tally['e2']}",
             f"Draws: {tally['draws']}",
             f"Errors/Skipped (excluded): {tally['errors']}"]
    if tally["completed"]:
        score = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
        el, margin = elo(score, tally["completed"])
        lines.append(
            f"Engine 1 score: {tally['e1'] + 0.5*tally['draws']:.1f}/{tally['completed']} "
            f"({100*score:.1f}%)  =>  {el:+.0f} +/- {margin:.0f} Elo")
    if stopped:
        lines.append("(match was stopped before completion)")
    if start_t is not None:
        elapsed = time.time() - start_t
        played = tally["completed"] + tally["errors"]
        per = fmt_duration(elapsed / played) if played else "-"
        lines += ["",
                  f"Ended:    {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                  f"Duration: {fmt_duration(elapsed)}   (per game: {per})"]
    text = "\n".join(lines)
    print("\n" + text)
    if fh is not None:
        try:
            fh.write(text + "\n")
            fh.flush()
        except Exception:
            pass


# ====================================================================== #
# Main
# ====================================================================== #
def main():
    # Optional command-line overrides so parallel windows can run DIFFERENT
    # matchups or DISJOINT position shards without editing this file:
    #     pypy3 match.py [engine1] [engine2] [num_positions] [offset]
    # `offset` skips that many positions into the (seeded) shuffled pool, so
    # parallel windows with a FIXED SUBSET_SEED and offsets 0, N, 2N, ... each
    # play a non-overlapping slice. Any omitted argument falls back to CONFIG.
    argv = sys.argv[1:]
    engine1 = argv[0] if len(argv) > 0 else ENGINE_1
    engine2 = argv[1] if len(argv) > 1 else ENGINE_2
    num_positions = int(argv[2]) if len(argv) > 2 else NUM_POSITIONS
    offset = int(argv[3]) if len(argv) > 3 else 0

    for p in (engine1, engine2):
        if not os.path.isfile(p):
            print(f"ERROR: engine file not found: {p!r}")
            return

    mode_cfg = {"mode": MODE, "time_ms": TIME_PER_MOVE_MS, "depth": FIXED_DEPTH,
                "tc_minutes": TC_MINUTES, "tc_increment": TC_INCREMENT}
    tc_label = f"{TC_MINUTES:g}+{TC_INCREMENT:g}" if MODE == "clock" else None
    tpm = TIME_PER_MOVE_MS if MODE == "time" else None

    # Positions -> seeded shuffle, then take the slice [offset : offset+n].
    # With a FIXED SUBSET_SEED every window shuffles identically, so distinct
    # offsets give DISJOINT shards (no overlap across parallel windows).
    all_fens = load_fens(FEN_FILE)
    pool = list(all_fens)
    import random as _r
    (_r.Random(SUBSET_SEED) if SUBSET_SEED is not None else _r).shuffle(pool)
    n = max(1, min(num_positions, len(pool)))
    fens = pool[offset:offset + n]
    if not fens:                       # offset past the end -> nothing to play
        print(f"ERROR: offset {offset} leaves no positions (pool size {len(pool)})")
        return
    total_games = len(fens) * 2

    ctx = mp.get_context("spawn")
    e1 = EngineProcess(ctx, engine1)
    e2 = EngineProcess(ctx, engine2)

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
    banner = (f"Match: {e1.name}  vs  {e2.name}\n"
              f"Interpreter: {interp}\n"
              f"Mode: {mode_desc}   |   Positions: {len(fens)}  ->  {total_games} games\n"
              f"Log: {log_path}\n" + "-" * 72)
    print(banner)
    if fh is not None:
        fh.write(f"{e1.name} vs {e2.name}\n"
                 f"Interpreter: {interp}\nMode: {mode_desc}\n"
                 f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fh.flush()

    tally = {"e1": 0, "e2": 0, "draws": 0, "errors": 0, "completed": 0}
    start_t = time.time()
    stopped = False

    try:
        e1.start()
        e2.start()

        schedule = []
        rno = 0
        for fen in fens:
            rno += 1
            schedule.append((rno, fen, e1, e2))      # E1 White
            rno += 1
            schedule.append((rno, fen, e2, e1))      # E2 White

        for round_no, fen, white, black in schedule:
            g = play_game(round_no, fen, white, black, e1, mode_cfg)
            write_game_block(fh, pgn_fh, g, e1, mode_cfg, tc_label, tpm)

            # Tally + one progress line.
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

            wn, bn = white.name, black.name
            if tally["completed"]:
                sc = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
                el, mar = elo(sc, tally["completed"])
                run = (f"E1 {tally['e1']}-{tally['e2']}-{tally['draws']} "
                       f"({100*sc:.1f}%, {el:+.0f}+/-{mar:.0f})")
            else:
                run = "no scored games yet"
            print(f"[{round_no:>4}/{total_games}] {wn}(W) vs {bn}(B)  ->  "
                  f"{g['result']:>7}  {tag:<34} | {run}")

    except KeyboardInterrupt:
        stopped = True
        print("\n[interrupted -- writing summary so far]")
    except EngineError as ex:
        stopped = True
        print(f"\nENGINE LOAD/RUN ERROR: {ex}")
    finally:
        write_summary(fh, e1, e2, tally, total_games, start_t, stopped)
        for eng in (e1, e2):
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
