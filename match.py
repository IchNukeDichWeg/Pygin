"""
match.py
========
Headless engine-vs-engine match runner -- everything ``engine_battle.py`` does
(subprocess engines with watchdogs, each position played both colours, the same
log-file format and Elo summary) but with NO pygame, so it runs anywhere and,
crucially, under PyPy:

Usage::

    python3 match.py [engine1.py] [engine2.py] [num_positions] [--workers N] [--engine-smp N]
                     [--force-nodes-1 N --force-nodes-2 M]    (explicit per-side node budgets, NO
                                                               calibration. --nodes derives each side's budget
                                                               from THIS box's NPS, which is what makes such a
                                                               run un-poolable with the same run elsewhere;
                                                               naming both budgets removes the machine from the
                                                               experiment so two boxes play the identical
                                                               contest and their pentanomials add. Both are
                                                               required together. Fairness becomes yours to
                                                               own: take the pair from ONE calibrated run and
                                                               reuse it. The mode string records them, so a
                                                               calibrated run resuming a forced state file
                                                               warns.)
                     [--total-time 1d,10h,4m,10s]             (WALL-CLOCK budget: play until it expires,
                                                               then stop. Overrides the position count --
                                                               the schedule becomes the rest of the pool and
                                                               the clock decides. --offset, --seed, --sprt and
                                                               the state file all still apply. Any subset of
                                                               d/h/m/s, e.g. 4h or 30m or "2h 15m".)
                     [--offset N]                             (opening-pool offset; the 4th POSITIONAL still
                                                               works, but this wins and is unambiguous. The
                                                               range is printed at the start, in the log
                                                               header and in the closing summary.)
                     [--push-state]                           (git add+commit+push the state file when the
                                                               run ends -- the campaign lives in git, the
                                                               rented box is disposable. Silent no-op without
                                                               a repo/remote/credentials.)
                     [--seed S]                               (SUBSET_SEED for the pool shuffle; "none" =
                                                               unshuffled. The pool is shuffled BEFORE it is
                                                               sliced, so the same offset under a different
                                                               seed is a DIFFERENT set of openings.)
                     [--tc 10+0.1]                            (time control: base+increment in seconds,
                                                               e.g. 50+0.2 or 10+0.1 or plain 10. Implies
                                                               --mode clock. --tc-seconds/--tc-increment set
                                                               the two halves separately.)
                     [--time-per-move MS] [--fixed-depth D]   (fixed ms or fixed plies per move; each implies
                                                               its own --mode, like --nodes does)
                     [--book1 book.bin] [--book2 book.bin]   (per-engine opening books; book testing)
                     [--start-pos True]                       (all games from startpos, ignore the FEN file)
                     [--sprt]                                 (SPRT early-stop: quit as soon as the result is
                                                               provably good/bad instead of playing the whole
                                                               budget -- default [0, 4] normalized, a=b=0.05;
                                                               override --sprt-elo0/elo1/alpha/beta/model)
                     [--sprt-resume state.json]               (pool this tranche with the earlier ones in that
                                                               file: the LLR continues instead of restarting.
                                                               Refuses to pool a different experiment or an
                                                               overlapping --offset; written on every exit
                                                               path, including Ctrl-C. OPTIONAL: --sprt alone
                                                               auto-names one after the run and prints the
                                                               path at startup and in the summary, so a
                                                               no-decision tranche is never unpoolable)

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

    # run-to-decision campaign: tranche 2 continues tranche 1's LLR
    python3 match.py A.py B.py 5000 0    --workers 0 --sprt --sprt-resume ab.json
    python3 match.py A.py B.py 5000 5000 --workers 0 --sprt --sprt-resume ab.json

    # same thing without naming the file: tranche 1 auto-writes
    # sprt_A_vs_B_<stamp>.json and prints it; feed that path to tranche 2
    python3 match.py A.py B.py 5000 0 --workers 0 --sprt

Progress is streamed to the terminal; a full per-move/PGN log is written to a
file named like ``<e1>_vs_<e2>_<timestamp>_<pid>.txt``.

Run several copies in parallel for more games (with a fixed SUBSET_SEED they all
draw the SAME positions, so results stay directly comparable / poolable).

Press Ctrl-C to stop early -- the summary (with Elo so far) is still written.
"""
# lib/ holds the shared support modules (time_manager, wdl, interruptible,
# smp, shared_tt) since the 2026-07-24 reshuffle. They stay importable by
# their plain names, so nothing else in the tree had to change.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "lib"))

# ====================================================================== #
#  CONFIG  -- edit these
# ====================================================================== #
ENGINE_1 = "engine.py"                       # path to engine 1
ENGINE_2 = "Old Engine/55/engine55.py"       # path to engine 2
                                             # FB-50 fold-in: was v51 while
                                             # SUBSET_SEED said 55 -- the
                                             # default opponent tracks the
                                             # snapshot the seed belongs to
FEN_FILE = "UHO_4060_v4.epd"                 # positions (plain FEN or EPD, one per line). UHO_4060_v4.epd (16 MB, balanced Stockfish openings) is the default. fen.txt (447 KB) is also bundled as a small fallback; a bigger book (UHO_Lichess_4852_v1.epd, 174 MB) is at https://github.com/official-stockfish/books

other_elo = 2900
# PGN-header tag only (cosmetic). Reads the SAME env/default as
# stockfish_engine.py's SF_ELO so the tag can't drift from what the
# limiter is actually set to -- the 2026-07-16 run played at 2700 while
# a stale hardcoded 2600 here went into every PGN header.
import os                   # config-time env read; harmlessly re-imported below
stockfish_elo = 2900        # --sf-elo N; <= 0 = full strength
# FI-88: in CLOCK mode, an engine that manages its own clock (only Stockfish)
# is handed `go wtime/btime/winc/binc` and budgets each move itself. Set True
# (--sf-our-clock) for the pre-2026-07-24 behaviour, where Pygin's own
# time_manager computed the ms for BOTH sides and SF's manager never ran.
# Our engines are unaffected either way -- they have no internal clock.
sf_our_clock = False

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

# --- WDL-based adjudication (OFF until data/wdl_model.json is calibrated) ------- #
# Shortens decided games: a win is adjudicated when BOTH engines' own
# reported scores agree the game is over (leader >= +threshold, opponent
# <= -threshold, each for ADJ_WIN_COUNT consecutive own moves), where the
# threshold is the cp at which the fitted WDL model says P(win) >= ADJ_WIN_P
# at the current phase. A draw is adjudicated late in level games. Needs
# data/wdl_model.json (written by tuning/fit_wdl_model.py); silently stays off without it.
# `--adj off` disables it per run without editing this file. Use that for
# CROSS-FAMILY matches (e.g. vs stockfish_engine.py): the WDL model is fitted
# on THIS engine's score scale, so the two-sided agreement rule loses its
# calibration against a foreign engine's cp reports. Same-family A/Bs only.
ADJUDICATE = True
ENGINE_SMP = 1              # Lazy-SMP workers inside EACH engine subprocess.
                            # --smp N overrides it for one run (--engine-smp
                            # is kept as an alias). Passed to the engine child
                            # as an argument -- it used to be exported as
                            # CLAUDECHESS_SMP, which is gone.
                            # Keep ENGINE_SMP * N_WORKERS * 2 (two engines per
                            # game) <= CPU cores or you oversubscribe and lose
                            # throughput.
ADJ_WIN_P = 0.99            # per-phase cp threshold = model's P(win) 99% point
ADJ_WIN_COUNT = 4           # consecutive own moves (each side) for a win call.
                            # 8 -> 4 (2026-07-18): the two-sided 99%-agreement
                            # rule below (leader >= +thr AND loser <= -thr) is
                            # what makes a false win near-impossible; the move
                            # debounce is only anti-blip. 4 ends decided games
                            # ~8 plies sooner, outcome-identical on real wins
                            # (cutechess resign defaults are 3-4). Draw params
                            # left conservative on purpose (FI-28 endgame-mask).
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
SUBSET_SEED = 57            # FIXED so parallel windows shuffle identically.
                            # ROTATION POLICY (2026-07-18): bump to the new
                            # version number at every vN snapshot -- within an
                            # era every campaign (and its extension tranches,
                            # which NEED the same seed for disjoint offset
                            # shards) shares one 5000-of-241k opening sample;
                            # across eras the book is resampled so the ledger
                            # can't slowly overfit one fixed 2% subset. 42 was
                            # the v31-v49 era seed (campaigns 1-24).
MAX_PLIES = 200             # games longer than this are adjudicated a draw
VERBOSE_MOVES = False       # also print every move to the terminal
                            #   (per-move info is ALWAYS written to the log file)
N_WORKERS = 10              # parallel game workers (override via --workers N|auto)
                            #   1  -> sequential (one engine pair, plays all games)
                            #   >1 -> N worker processes, each with its own engine pair

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
import hashlib
import re                # FI-82: --sprt-resume experiment fingerprint
import json
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

from battle_worker import (engine_worker, nnue_label,
                           describe_nnue_source)
from time_manager import calculate_move_time

# Optional SPRT early-stop (--sprt). Imported defensively so a broken/missing
# sprt.py can never take a match down -- the feature just goes unavailable.
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "testing"))          # moved 2026-07-24
    import sprt as _sprt
except Exception:
    _sprt = None

# Don't evaluate the SPRT on a tiny sample (the LLR is noisy early and could
# early-stop on a fluke); wait for this many PAIRS first. 250 pairs = 500
# games -- a decision can't fire before then. (Was 500; halved 2026-07-29 on
# the user's call. The Wald bounds already control the error rates -- this
# floor only guards the pathological first-few-pairs regime, and 500 pairs
# was costing an hour before a landslide could ever be called.)
SPRT_MIN_PAIRS = 250


# ---------------------------------------------------------------------- #
# FI-82: --sprt-resume, tranche pooling for run-to-decision campaigns.
#
# A sequential test that has not decided must KEEP SAMPLING (the FI-30
# lesson), and the next tranche usually runs on another box or another day.
# Pooling that by hand is where campaigns go wrong -- FI-30's tranche 4 was
# played on the WRONG CHECKOUT and only the server reflog caught it. So the
# state file therefore RECORDS every tranche's config in a `runs` list, so a
# pooled figure can always be audited after the fact.
#
# It used to REFUSE to pool anything whose fingerprint (incl. the sha256 of
# both engine .py files) did not match. Removed 2026-07-29 on the user's call:
# adding a COMMENT to cengine.py changed its hash and orphaned a campaign's
# prior games over a difference that cannot affect a single move. Record and
# warn; the operator judges. Corrupt data is still fatal -- that is not a
# config choice.
# ---------------------------------------------------------------------- #
def _sha16(path):
    """First 16 hex of a file's sha256, streamed (FEN pools reach 174 MB)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


SPRT_FP_VERSION = 2      # FB-49: bump when the tuple below changes, so an
                         # old state.json fails LOUDLY instead of pooling.


def _net_sha_for_engine(engine_py):
    """sha16 of the .nnue an engine source names, or None.

    Deliberately a text scan, not an import. It only has to be right when it
    matters -- a net that is named and present -- and being wrong here is
    safe in the conservative direction: a missed net means the fingerprint
    falls back to the engine .py hash, which already changes whenever the
    named path changes."""
    try:
        src = open(engine_py, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = re.findall(r'NNUE_FILE\s*=\s*["\']([^"\']+\.nnue)["\']', src)
    if not m:
        return None
    p = m[-1]
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    return _sha16(p) if os.path.isfile(p) else None


def _net_lines(procs):
    """"Net (name): ..." lines for whichever engines use one.

    Prefers what the loaded engine REPORTED, and falls back to scanning its
    source. Both are needed: the banner is printed before the engine
    processes start, so nothing has reported yet, while the summary runs
    after and should show what actually loaded rather than what the file
    says it would."""
    out = []
    for proc in procs:
        info = getattr(proc, "nnue", None)
        if info is None:                      # not started yet -> read source
            info = describe_nnue_source(proc.path)
        lb = nnue_label(info)
        if lb:
            out.append(f"Net ({proc.name}): {lb}")
    return out


_SHORT_REASON = {
    "CHECKMATE": "mate", "STALEMATE": "stalemate",
    "THREEFOLD_REPETITION": "3-fold", "FIVEFOLD_REPETITION": "5-fold",
    "FIFTY_MOVES": "50-move", "SEVENTYFIVE_MOVES": "75-move",
    "INSUFFICIENT_MATERIAL": "material",
    "MAX_PLIES (adjudicated draw)": "max-plies",
    "ADJUDICATION_WIN": "adj", "ADJUDICATION_DRAW": "adj-draw",
    "TIME_FORFEIT": "time", "VARIANT_WIN": "variant", "VARIANT_DRAW": "variant",
    "VARIANT_LOSS": "variant",
    "WORKER_STARTUP_FAILED": "worker-start", "WORKER_EXCEPTION": "worker-exc",
}


def _games(n):
    """Apostrophe thousands separator, per the user's preferred format."""
    return f"{int(round(n)):,}".replace(",", "'")


def short_reason(reason):
    """Compact form for the scrolling per-game line ONLY.

    The canonical string still goes to the .txt log and every stored record,
    so nothing downstream has to learn these abbreviations -- this is purely
    to stop MAX_PLIES (adjudicated draw) eating half the terminal width."""
    return _SHORT_REASON.get(reason, (reason or "").lower())


def sprt_resume_load(path, offset):
    """Read a tranche state file -> (base_penta, next_offset, prior_runs, note).

    WARNS, never refuses. This used to hard-fail on a fingerprint mismatch so
    that two tranches could not be pooled unless every config field and the
    sha16 of BOTH engine files matched. That is the right rule for a
    certification suite and the wrong one for a research tool: a comment added
    to cengine.py changed its hash and orphaned an entire campaign's prior
    games, for a difference that could not affect a single move. The user's
    call, 2026-07-29 -- record the provenance, print what changed, and let the
    operator judge. Every tranche's config is kept in `runs` so a pooled number
    can always be audited after the fact.

    A corrupt file is still fatal: a bad penta silently poisons the statistic,
    which is different from a config the operator may legitimately have
    changed."""
    with open(path, "r", encoding="utf-8") as fh:
        st = json.load(fh)
    penta = [int(v) for v in st.get("penta", [0] * 5)]
    if len(penta) != 5 or any(v < 0 for v in penta):
        raise ValueError(f"state file: corrupt penta in {path!r}")
    nxt = int(st.get("next_offset", 0))
    prior_runs = list(st.get("runs", []))
    note = (f"Resuming {path!r}: {sum(penta):,} prior pairs "
            f"({'/'.join(str(v) for v in penta)}) over {len(prior_runs)} run(s)"
            f"; the LLR continues from there.")
    if offset < nxt:
        note += (f"\n  ** OVERLAP WARNING: offset {offset} is below the "
                 f"{nxt} this file has already consumed. Pooling a position "
                 f"twice is the SAME evidence counted twice, not more of it. "
                 f"Use --offset {nxt} unless you meant this.")
    if st.get("decision") in ("H0", "H1"):
        note += (f"\n  NOTE: this file already records a {st['decision']} "
                 f"decision -- this run only adds to it.")
    return penta, nxt, prior_runs, note


def sprt_resume_save(path, penta, next_offset, sprt_state, runs):
    """Dump the pooled state atomically.

    Written at the START of a run, every SAVE_EVERY games, and from the finally
    block -- so it survives Ctrl-C, SIGTERM, a kill and a crash alike, and a
    run that dies at game 9,000 does not lose 9,000 games. `runs` is the
    provenance list: one record per tranche, so a pooled figure can be audited
    even though pooling is no longer gated on a fingerprint."""
    st = sprt_state or {}
    tmp = f"{path}.tmp"                  # never truncate a good state file on
    with open(tmp, "w", encoding="utf-8") as fh:      # a crash mid-write
        json.dump({"penta": list(penta),
                   "next_offset": int(next_offset),
                   "pairs": int(sum(penta)),
                   "decision": st.get("decided"),
                   "llr": st.get("llr"),
                   "runs": list(runs)}, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# How often the state file is refreshed mid-run, in completed GAMES.
SAVE_EVERY = 50


def push_state_file(path):
    """Commit+push the state file from whatever box the campaign ran on.

    A campaign spans machines: the state file IS the campaign and the rented
    box is disposable. A tranche played on box A and left only on box A means
    box B resumes from a stale pool and silently discards real games -- which
    nearly cost 2,321 pairs of a 21,806-game verdict.

    Best-effort and silent about the boring failures: no git, no remote, no
    credentials on a fresh rental, nothing to commit. Never raises -- this runs
    next to the summary, and the summary is the point of the run. Adds ONLY the
    state file, never -A, so it cannot sweep up unrelated edits."""
    import subprocess
    name = os.path.basename(path)
    try:
        run = lambda *a: subprocess.run(a, capture_output=True, text=True,
                                        timeout=120,
                                        cwd=os.path.dirname(os.path.abspath(path)) or ".")
        if run("git", "rev-parse", "--git-dir").returncode != 0:
            return                                  # not a repo; nothing to do
        run("git", "add", "--", path)
        run("git", "commit", "-m", f"{name}: tranche state")
        r = run("git", "push")
        print(f"State pushed to git: {name}" if r.returncode == 0 else
              f"State NOT pushed ({name}): {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'push failed'}")
    except Exception as ex:
        print(f"State NOT pushed ({name}): {ex!r}")


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
                  self.book_path, ENGINE_SMP, stockfish_elo),
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
            # payload added later than the protocol; tolerate its absence so
            # an older battle_worker still loads
            self.nnue = msg[1] if len(msg) > 1 else None
            return
        self.kill()
        if msg[0] == "fatal":
            raise EngineError(f"{self.name} failed to load:\n{msg[1]}")
        raise EngineError(f"{self.name}: unexpected reply {msg[0]!r} on load")

    def request_calibrate(self, timeout=120.0):
        """--nodes mode: ask the engine process to bench itself (6-FEN suite
        @ d11, book/tb/1-thread forced) and return its measured NPS."""
        self.conn.send(("calibrate",))
        if not self.conn.poll(timeout):
            self.kill()
            raise EngineTimeout(f"{self.name}: calibration hung (killed)")
        msg = self.conn.recv()
        if msg[0] == "ok":
            return float(msg[1]["nps"])
        raise EngineError(f"{self.name} failed calibration:\n{msg[1]}")

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
def _data_path(name):
    """Resolve a bundled data file. Books/EPDs moved to data/ on 2026-07-24;
    a bare name is still honoured so an explicit --fen-file or a local copy
    beside the runner keeps working."""
    if os.path.isabs(name) or os.path.isfile(name):
        return name
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "data", name)
    return cand if os.path.isfile(cand) else name


def load_fens(path):
    """Load and validate every position in ``path`` (plain FEN or EPD)."""
    path = _data_path(path)
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

    SCALE (FB-54, corrected 2026-07-26): this is now the SAME scale as the
    GSPRT bounds and as Fishtest sprt_calc's elo0/elo1. Every "norm" figure
    quoted in improvements.md / final_improvements.md from BEFORE this date
    was a factor sqrt(2) = 1.414 larger; divide an old one by 1.414 to
    compare it with a new one (v55's +21.18 becomes +14.98).
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
    # FB-54: divide by sqrt(2)*sigma, NOT sigma. This is the inverse of
    # sprt.py's `_score_from_elo` for the "normalized" model
    # (score = 0.5 + elo*ln10/800 * sqrt(2*var)), which is the scale the
    # GSPRT bounds printed on the very next line are expressed in, and the
    # scale of Fishtest sprt_calc's elo0/elo1 fields. Omitting the sqrt(2)
    # made the printed figure read a factor 1.414 LARGER than the bounds
    # beside it -- two numbers in one block on two different scales, the
    # bigger one mislabelled as the standard.
    nelo = (game_mean - 0.5) / (math.sqrt(2.0) * sigma) * (800.0 / math.log(10))
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
_WDL_THR = {}               # eval family -> thr(phase) | None (model missing)
_WDL_FAMILY = {}            # engine path -> "nnue" | "hce"


def _eval_family(engine):
    """Which eval SCALE this engine reports centipawns on.

    The WDL model converts cp to P(win), and an NNUE arm and a hand-crafted
    arm do not share a scale -- so in an NNUE-vs-HCE match a single model is
    wrong for one of the two sides. Adjudication needs BOTH sides to agree a
    game is decided, and it reads each side's OWN score, so the threshold has
    to be chosen per side too.

    Read from the engine SOURCE (the same scan that fills the net line in the
    banner), so it costs nothing per move and is cached per path. Caveat worth
    knowing: FI-104 can disarm the net at RUNTIME on a build with no SIMD dot
    kernel, and a source scan cannot see that -- such a box would be judged on
    the nnue model while playing HCE. It also refuses to arm at all there, so
    the case is a non-SIMD host, which is not one anybody runs A/Bs on."""
    path = getattr(engine, "path", None)
    if path not in _WDL_FAMILY:
        try:
            _WDL_FAMILY[path] = ("nnue" if describe_nnue_source(path)["on"]
                                 else "hce")
        except Exception:
            _WDL_FAMILY[path] = "hce"
    return _WDL_FAMILY[path]


def _wdl_win_threshold(phase, family="hce"):
    """cp at which the fitted WDL model puts P(win) at ADJ_WIN_P for this
    phase, or None while the model doesn't exist (adjudication then silently
    stays off). Loaded once per process PER FAMILY -- each game worker reads
    the file on its first adjudication check.

    'hce' reads data/wdl_model.json; 'nnue' prefers data/wdl_model_nnue.json
    (tuning/fit_wdl_model.py --nnue) and FALLS BACK to the hce model when that
    file does not exist. The fallback is what the code did unconditionally
    before this existed, so a tree with no NNUE model behaves exactly as it
    always has -- but it says so once, because an NNUE arm judged on
    hand-crafted thresholds is a thing an operator should know about rather
    than discover in a draw rate."""
    if family not in _WDL_THR:
        path = "wdl_model.json"      # bound before the try: the except prints it
        try:
            import json
            path = _data_path("wdl_model.json")
            if family == "nnue":
                nnue_path = _data_path("wdl_model_nnue.json")
                if os.path.exists(nnue_path):
                    path = nnue_path
                else:
                    print("[match] NOTE: no data/wdl_model_nnue.json -- the "
                          "NNUE side is being adjudicated on the "
                          "hand-crafted-eval model. Same behaviour as before "
                          "per-family models existed; refit with "
                          "tuning/fit_wdl_model.py --nnue to fix the scale.",
                          file=sys.stderr, flush=True)
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
            _WDL_THR[family] = thr
        except (OSError, ValueError, KeyError) as _e:
            # HOST-07: adjudication used to disable itself in SILENCE here.
            # That is an A/B-integrity hazard, not a cosmetic one: a campaign
            # split across two machines where one loads the model and the
            # other does not produces halves with different game lengths and
            # different draw rates, which must never be pooled.
            _WDL_THR[family] = None
            print(f"[match] WARNING: WDL adjudication is OFF for the "
                  f"'{family}' side -- could not load {os.path.basename(path)} "
                  f"({type(_e).__name__}: {_e}). Games will run to natural "
                  f"end / MAX_PLIES. If the OTHER half of a split campaign "
                  f"loaded it, DO NOT pool the two halves.",
                  file=sys.stderr, flush=True)
    t = _WDL_THR[family]
    return None if t is None else t(phase)


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
            req_mode = "time"
            if mode_cfg.get("sf_our_clock"):
                req_value = budget          # pre-FI-88: our budget drives BOTH
                req_timeout = budget / 1000.0 * TIME_OVERSHOOT_FACTOR + TIME_GRACE
            else:
                # FI-88 (default): carry the raw clocks too. A clock-managing
                # engine budgets its own move, so the watchdog can no longer be
                # a multiple of OUR budget -- SF outspending it on a critical
                # move is the whole point. The honest bound is its REMAINING
                # clock: past that it has flagged anyway.
                req_value = (budget, clocks[chess.WHITE], clocks[chess.BLACK],
                             inc_ms, inc_ms)
                req_timeout = clocks[color] / 1000.0 + TIME_GRACE
        elif mode_cfg["mode"] == "depth":
            req_mode, req_value = "depth", mode_cfg["depth"]
            req_timeout = DEPTH_SAFETY_CAP
        elif mode_cfg["mode"] == "nodes":
            # --nodes: per-side NPS-calibrated budget (set in _worker_loop);
            # the node count is load-immune but WALL time stretches under
            # load, hence the generous depth-style watchdog.
            req_mode = "nodes"
            req_value = getattr(mover, "node_budget", None) or mode_cfg["nodes"]
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
        if mode_cfg.get("adjudicate", ADJUDICATE):
            thr = _wdl_win_threshold(_phase24(board),
                                     _eval_family(mover))
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
        elif mode_cfg["mode"] == "nodes":
            out.append(f"Mode: Nodes = {mode_cfg['nodes']}/move "
                       f"(engine1 reference; engine2 NPS-scaled per worker)")
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


# --- FI-81's --nodes calibration constants. Module level since FI-101 runs
# the same calibration a SECOND time at the end of the campaign. ---
# FI-101 follow-up, measured 2026-07-27 on an idle 96-core box: a NULL
# (cengine.py against itself) calibrated to a median of 0.989 -- a 1.1% budget
# gap between IDENTICAL binaries -- and 0.989 misses a 1% deadband by one
# thousandth, so it was applied. engine2 played the whole match on 1,730,710
# nodes against engine1's 1,750,000; identity read 0% and the end
# recalibration read 1.0014, i.e. the true ratio was 1.000 all along.
#
# Three changes, in increasing order of cost:
#   * DISCARD ROUND 1. Same reason nps13 discards it: the first bench pays for
#     page-in and a cold cache, and it pays unequally between the two engines.
#     Free, and it removes the roughest sample.
#   * MORE ROUNDS. The median of 5 is noisy; 9 leaves 8 usable and cuts the
#     standard error ~40%. Costs a few seconds once per campaign.
#   * A WIDER DEADBAND, 1% -> 2%. This is the one with a real cost: a candidate
#     genuinely 1-2% slower now gets an EQUAL budget instead of a scaled one,
#     so a real NPS regression of that size is absorbed rather than charged
#     (~1-2 Elo at the house's 1.16 Elo/1% conversion). That is the correct
#     trade. A mis-snapped null is not worth 1-2 Elo of precision: the same
#     failure produced a +48.96 Elo reading on identical engines on 2026-07-26,
#     and near-mirror determinism AMPLIFIES a consistent budget error because
#     it repeats in every pair instead of averaging out.
CAL_ROUNDS = 9
CAL_DEADBAND = 0.02
CAL_MAX_SPREAD = 0.05      # HOST-03: warn above 5% round-to-round spread


def calibrate_nodes(engine1, engine2, when):
    """Measure engine2/engine1 NPS and return the budget ratio (FI-81).

    One sequential calibration pair on the quiet pre-spawn machine. engine1 is
    the reference (gets exactly N); engine2's budget is scaled by the measured
    NPS ratio so equal budgets = equal wall time and an NPS-costly candidate
    still pays in nodes exactly as it would on the clock (~1 Elo per 1% NPS
    house pricing).

    Interleaved repeated benches, median per-round ratio: single benches swing
    several % on a busy/thermal machine, and in nodes mode ANY consistent
    budget error is AMPLIFIED by near-mirror determinism (the first null test
    read -40 Elo on identical engines from a 3.7% ratio error). Adjacent-in-
    time round pairs share thermal state, so per-round ratios are far tighter
    than one long bench; the median rejects outlier rounds. A 1% deadband snaps
    measurement noise to exactly 1.0 (identical builds MUST get equal budgets);
    a real candidate's sub-1% NPS cost is absorbed as <1 Elo of budget error on
    naturally-divergent games -- documented limitation, fine at screen
    precision.

    `raw` is the median BEFORE the deadband, and it is the field FI-101's
    end-of-campaign comparison uses: two calibrations that both snapped to
    1.000 can still be 1.9% apart, and that drift is exactly what the check
    exists to see.
    """
    ctx = mp.get_context("spawn")
    c1 = EngineProcess(ctx, engine1, BOOK_ENGINE1)
    c2 = EngineProcess(ctx, engine2, BOOK_ENGINE2)
    ratios, nps1_list, nps2_list = [], [], []
    try:
        c1.start()
        c2.start()
        for rnd in range(CAL_ROUNDS):
            n1 = c1.request_calibrate()
            n2 = c2.request_calibrate()
            if rnd == 0:
                continue          # discard: cold cache, paid unequally
            nps1_list.append(n1)
            nps2_list.append(n2)
            ratios.append(n2 / n1)
    finally:
        c1.kill()
        c2.kill()
    ratios.sort()
    raw = ratios[len(ratios) // 2]                   # median round ratio
    # The deadband is now STATISTICAL as well as fixed (2026-07-27). A fixed
    # threshold cannot tell a REAL 2% NPS difference from a noisy measurement
    # of zero -- and this box produced a 1.0213 median on two builds that
    # differ by one qsearch threshold, applied it, and handed the baseline a
    # 2.1% node advantage for a whole tranche. What separates the two cases is
    # whether the rounds AGREE: a genuine difference is reproducible round to
    # round (tight spread), a spurious one is not. So if the offset from 1.0 is
    # smaller than the measurement's own half-spread, the calibration cannot
    # distinguish this ratio from 1.000 and must not pretend to.
    half_spread = (ratios[-1] - ratios[0]) / 2.0
    ratio = 1.0 if abs(raw - 1.0) < max(CAL_DEADBAND, half_spread) else raw
    spread = (ratios[-1] - ratios[0]) / raw if raw else 0.0
    lines = [f"NPS calibration ({when}, {CAL_ROUNDS} interleaved bench rounds "
             f"per engine, round 1 discarded):",
             "  round ratios: " + " ".join(f"{r:.3f}" for r in ratios)
             + f" -> median {raw:.3f}"
             + (f" (half-spread {half_spread:.3f} >= offset "
                f"{abs(raw - 1.0):.3f} -> 1.000)"
                if ratio == 1.0 and abs(raw - 1.0) >= CAL_DEADBAND
                else " (deadband -> 1.000)" if ratio == 1.0 else "")]
    # HOST-03: the median is robust to ONE bad round, not to a noisy box. This
    # number sets the node budget for every game in the campaign, so a wide
    # sample set has to be seen, not averaged away in silence. Rented
    # "identical" hardware has been measured 50% apart (FI-64), and a co-tenant
    # process is invisible from in here.
    if spread > CAL_MAX_SPREAD:
        warn = (f"[match] WARNING: NPS calibration spread {spread*100:.1f}% "
                f"exceeds {CAL_MAX_SPREAD*100:.0f}% (min {ratios[0]:.3f}, max "
                f"{ratios[-1]:.3f}). The node budget is unreliable -- something "
                f"else is likely using this machine. Re-run the calibration on "
                f"a quiet box before trusting this campaign.")
        print(warn, file=sys.stderr, flush=True)
        lines.append("  " + warn)
    return {"ratio": ratio, "raw": raw, "nps1": sorted(nps1_list)[len(nps1_list) // 2],
            "nps2": sorted(nps2_list)[len(nps2_list) // 2], "ratios": ratios,
            "spread": spread, "lines": lines}


def calibration_drift(cal_start, cal_end, interrupted=False):
    """FI-101: did the host stay the same machine for the whole campaign?

    Compares the two RAW medians. Anything past the deadband means the budget
    the games were played at stopped matching the hardware they were played on
    -- thermal drift, a co-tenant that arrived mid-run, a box that was never
    idle. FI-100 showed what that costs when it happens at the START: the same
    binary on both sides scored +48.96 Elo. Mid-campaign it is worse, because
    nothing in the summary looks wrong at all.
    """
    drift = cal_end["raw"] / cal_start["raw"] - 1.0
    if abs(drift) <= CAL_DEADBAND:
        # The check still RUNS -- a host that degrades mid-run would skew the
        # tranche and this is what catches it. But two lines of "nothing
        # happened" on every clean run is noise, so a held host says nothing.
        return []
    lines = [f"NPS calibration drift (FI-101): start {cal_start['raw']:.4f} -> "
             f"end {cal_end['raw']:.4f}  ({drift*100:+.2f}%)"]
    if interrupted:
        # The end bench runs BEFORE _shutdown_workers, so on an interrupted run
        # it competes with up to N still-live engine processes mid-search. The
        # reading is contended, not the host degrading -- and the START
        # calibration is what actually set every budget, so a bogus end value
        # must not be dressed up as "treat the Elo as suspect". Seen live: a
        # Ctrl-C'd tranche read 1.0289 -> 0.9775 (-5.00%), where a ratio below
        # 1.0 was not physically possible for that engine pair.
        lines.append("  (run was INTERRUPTED: the end bench ran while the "
                     "worker pool was still live, so this reading is "
                     "contended. The START calibration set the budgets and is "
                     "the one that matters.)")
        return lines
    if abs(drift) > CAL_DEADBAND:
        lines.append(f"  ** WARNING: drift exceeds the {CAL_DEADBAND*100:.0f}% "
                     f"deadband. The node budgets were set for a machine this "
                     f"one stopped being; treat the Elo above as suspect and "
                     f"re-run on a quiet box. **")
    else:
        lines.append("  within the deadband -- the host held for the whole run")
    return lines


def write_summary(fh, e1, e2, tally, total_games, start_t, stopped,
                  n_workers=None, sprt_info=None, mode_desc=None,
                  startup_fail=None, cal_lines=None, openings=None):
    lines = ["", "=== BATTLE SUMMARY ===",
             f"Engine 1: {e1.name}", f"Engine 2: {e2.name}",
             *_net_lines([e1, e2]),
             f"Games scored: {tally['completed']:,}  (of {total_games:,} scheduled)",
             f"Workers: {n_workers}",
             *( [f"Mode: {mode_desc}"] if mode_desc else [] ),
             # The opening range belongs at BOTH ends: the closing summary is
             # what gets pasted into a report, and a pooled figure whose shards
             # cannot be checked for overlap is not auditable.
             *( [f"Openings: {openings}"] if openings else [] ),
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
    # FB-59: workers that died during engine LOAD never played a game, so
    # they are invisible in the counts above -- and if the deaths correlate
    # with one engine (a stale .so, a missing net) the campaign is BIASED,
    # not merely short. Say so where the numbers are read.
    if startup_fail and startup_fail.get("n"):
        lines.append(f"!! Worker startup failures: {startup_fail['n']} "
                     f"-- these workers never contributed a game; treat the "
                     f"sample as short AND possibly one-sided")
        for _w in startup_fail.get("why", [])[:3]:
            lines.append(f"   {_w}")

    si = sprt_info
    sprt_decided = bool(si and si.get("decided"))
    if si and si.get("cfg") and si.get("llr") is not None:
        cfg = si["cfg"]
        lines.append(
            f"SPRT[{cfg['elo0']:g}, {cfg['elo1']:g}] {cfg['model']} "
            f"(alpha={cfg['alpha']:g} beta={cfg['beta']:g}): "
            f"LLR {si['llr']:+.3f} in [{si['lower']:+.3f}, {si['upper']:+.3f}]"
            # WHOSE LLR. The pentanomial is scored from ENGINE 1, so the test
            # asks "is ENGINE 1 >= elo1 better than engine 2" -- the CANDIDATE
            # belongs in slot 1. With the candidate in slot 2 the sign flips
            # and the printed LLR answers the mirror question, which is not
            # the same magnitude (the bounds are not symmetric about the
            # result): one real run read -1.989 for the baseline while the
            # candidate stood at +0.665, and pooled it "decided" ACCEPT H0 on
            # a claim nobody had made. Nothing in the output said which side
            # it meant. Now it does.
            f"  <- for {e1.name} (Engine 1) as the CANDIDATE; put the "
            f"change in slot 1")
        if si.get("base_pairs"):         # FI-82: say what the LLR is made of
            lines.append(
                f"SPRT pooling: LLR covers {si['base_pairs']:,} pairs from "
                f"earlier tranches PLUS this run's ptnml above (the Elo and "
                f"ptnml lines are THIS tranche only)")
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
    # FI-103: outside the llr-is-not-None branch on purpose -- a tranche that
    # ended before SPRT_MIN_PAIRS still wrote a state file, and that is exactly
    # the run whose ptnml would otherwise be stranded.
    if si and si.get("resume_path"):
        lines.append(
            f"SPRT state file: {si['resume_path']}"
            + ("  (auto-named and STABLE -- the next run with the same "
               "engines AND instrument pools with it automatically; --tag "
               "names it explicitly)"
               if si.get("resume_auto") else ""))
    # An SPRT stop is a CONCLUSION, not an interruption, so don't mislabel it.
    if stopped and not sprt_decided:
        lines.append("(match was stopped before completion)")
    if cal_lines:                       # FI-101: start-vs-end calibration
        lines += cal_lines
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
        # FB-59: a worker that dies HERE -- stale .so, missing net, import
        # error -- never enters the game loop, so it is not counted as a
        # failed game. It simply never contributes: the run continues at
        # reduced throughput with no signal, and if the failure correlates
        # with ONE engine (it usually does) the campaign is biased, not just
        # slow. Report it on out_q as a distinguishable record instead.
        try:
            e1.start()
            e2.start()
        except Exception as ex:
            import traceback
            # EngineError already begins with the failing engine's name, so
            # do NOT prepend a guess -- an attribution that can be wrong is
            # worse than none when the whole point is telling WHICH side died.
            out_q.put({"round": -1, "fen": "", "result": "*",
                       "reason": "WORKER_STARTUP_FAILED",
                       "error": str(ex).strip().splitlines()[0],
                       "traceback": traceback.format_exc()})
            return
        if mode_cfg["mode"] == "nodes":
            # Budgets were calibrated ONCE in the parent (sequentially, on a
            # quiet machine) -- benching per worker during parallel startup
            # measured each side under different contention and biased the
            # very first null test (-45 on identical engines, ratio 0.52).
            e1.node_budget = int(mode_cfg["nodes_e1"])
            e2.node_budget = int(mode_cfg["nodes_e2"])
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


_DUR_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """'1d,10h,4m,10s' -> seconds. Any subset, any order, commas optional.

    Raises ValueError on anything it cannot read rather than guessing: a
    mistyped budget that silently became 0 or 10x would waste the run it was
    meant to bound."""
    total, seen = 0, False
    for part in re.split(r"[,\s]+", str(text).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([dhms])", part.lower())
        if not m:
            raise ValueError(f"--total-time: cannot read {part!r}; "
                             f"use forms like 1d,10h,4m,10s")
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        seen = True
    if not seen or total <= 0:
        raise ValueError(f"--total-time: {text!r} is not a positive duration")
    return total


def fmt_duration_short(sec):
    sec = int(sec)
    out = []
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if sec >= n:
            out.append(f"{sec // n}{unit}")
            sec %= n
    return "".join(out) or "0s"


class _TimeStop(Exception):
    """Raised when --total-time expires. Same unwind path as _SPRTStop: the
    games already played are a complete, poolable result, not a truncation."""


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
    global ENGINE_SMP, stockfish_elo, sf_our_clock
    # Apply the engine SMP override FIRST -- the moment any worker (and hence
    # any engine subprocess) is spawned it inherits the current environment,
    # so this must happen before mp.get_context("spawn") or any .start() call
    # below. CLI flag wins over the CONFIG constant so you can do e.g.
    # ``python3 match.py --engine-smp 8`` without editing the file.
    smp_override = None
    for flag in ("--smp", "--engine-smp"):      # --engine-smp kept as an alias
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                smp_override = int(sys.argv[i + 1])
                del sys.argv[i:i + 2]
    if smp_override is not None:
        ENGINE_SMP = max(1, int(smp_override))
        print(f"[match] engine SMP: {ENGINE_SMP} worker(s) per engine")
    if "--sf-our-clock" in sys.argv:        # FI-88 opt-out
        sys.argv.remove("--sf-our-clock")
        sf_our_clock = True
        print("[match] clock mode: OUR time_manager budgets Stockfish too "
              "(pre-FI-88 behaviour)")
    if "--sf-elo" in sys.argv:
        i = sys.argv.index("--sf-elo")
        if i + 1 < len(sys.argv):
            stockfish_elo = int(sys.argv[i + 1])
            del sys.argv[i:i + 2]
            print(f"[match] stockfish_engine.py Elo cap: "
                  f"{'full strength' if stockfish_elo <= 0 else stockfish_elo}")

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
        ADJUDICATE, FEN_FILE, BOOK_ENGINE1, BOOK_ENGINE2, START_POS, \
        SUBSET_SEED
    argv = sys.argv[1:]
    workers_str = None
    nodes_budget = None
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
    sprt_resume_path = None    # FI-82: pooled-tranche state file
    push_state = False         # --push-state
    force_nodes_1 = None       # --force-nodes-1/2: explicit per-side budgets,
    force_nodes_2 = None       #   no calibration -> comparable ACROSS boxes
    total_time = None          # --total-time: wall-clock budget
    campaign_tag = None        # --tag: names the state file explicitly
    offset_opt = None          # --offset (overrides the 4th positional)
    seed_opt = None            # --seed
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
        elif argv[i] == "--tc" and i + 1 < len(argv):
            # "50+0.2" / "10+0.1" / "10" -- the whole time control in one arg,
            # written the way it is spoken. Implies clock mode.
            spec = argv[i + 1].strip()
            try:
                base, _, inc = spec.partition("+")
                TC_SECONDS = float(base)
                TC_INCREMENT = float(inc) if inc else 0.0
            except ValueError:
                print(f"ERROR: --tc wants BASE[+INC] in seconds, got {spec!r} "
                      f"(e.g. 10+0.1)")
                return
            MODE = "clock"
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
            MODE = "time"                    # the flag implies its mode, as --nodes does
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
            MODE = "depth"                   # the flag implies its mode, as --nodes does
            i += 2
        elif argv[i] == "--adj" and i + 1 < len(argv):
            v = argv[i + 1].strip().lower()
            if v not in ("on", "off"):
                print("--adj takes 'on' or 'off'")
                sys.exit(2)
            ADJUDICATE = (v == "on")
            i += 2
        elif argv[i] == "--fen-file" and i + 1 < len(argv):
            FEN_FILE = argv[i + 1]
            i += 2
        elif argv[i] == "--nodes" and i + 1 < len(argv):
            # Opt-in fixed-node mode (machine-load-immune SCREENS; 10k GSPRT
            # confirmations stay 50+0.2 timed per doctrine). Each side's
            # per-move budget is scaled by its own bench NPS measured at
            # worker startup (engine1 = reference), so an NPS-costly
            # candidate still pays in nodes exactly as it would on the
            # clock. Results are NOT comparable to timed campaigns.
            nodes_budget = int(argv[i + 1])
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
        elif argv[i] == "--sprt-resume" and i + 1 < len(argv):
            sprt_resume_path = argv[i + 1]      # FI-82: pool with prior
            i += 2                              # tranches in this file
        elif argv[i] == "--offset" and i + 1 < len(argv):
            offset_opt = int(argv[i + 1])       # named form; wins over the
            i += 2                              # 4th positional
        elif argv[i] == "--seed" and i + 1 < len(argv):
            seed_opt = argv[i + 1].strip()      # "none"/"off" = unshuffled
            i += 2
        elif argv[i] == "--push-state":
            push_state = True                   # git add+commit+push the state
            i += 1                              # file when the run ends
        elif argv[i] == "--force-nodes-1" and i + 1 < len(argv):
            force_nodes_1 = max(1, int(argv[i + 1]))   # skip calibration for
            i += 2                                     # this side
        elif argv[i] == "--force-nodes-2" and i + 1 < len(argv):
            force_nodes_2 = max(1, int(argv[i + 1]))
            i += 2
        elif argv[i] == "--total-time" and i + 1 < len(argv):
            total_time = parse_duration(argv[i + 1])   # wall-clock budget;
            i += 2                                     # overrides the count
        elif argv[i] == "--tag" and i + 1 < len(argv):
            campaign_tag = re.sub(r"[^A-Za-z0-9._-]", "_", argv[i + 1].strip())
            i += 2
        elif argv[i].startswith("--"):
            # An unknown --flag used to fall through to `positional`, where it
            # became engine1/engine2/num_positions/offset. `--depth 4` (the
            # flag is --fixed-depth) silently tried to parse "--depth" as an
            # OFFSET. A typo must never quietly change what the run measures.
            print(f"ERROR: unknown option {argv[i]!r}. See the Usage block at "
                  f"the top of match.py.")
            return
        else:
            positional.append(argv[i])
            i += 1
    engine1 = positional[0] if len(positional) > 0 else ENGINE_1
    engine2 = positional[1] if len(positional) > 1 else ENGINE_2
    # Positional arg is the number of POSITIONS; each is played twice (both
    # colours), so total games = num_positions * 2.
    num_positions = max(1, int(positional[2]) if len(positional) > 2 else NUM_GAMES)
    if total_time is not None:
        # The clock decides when to stop, so the SCHEDULE must never be the
        # binding constraint -- take the rest of the pool and let the deadline
        # end it. Offset still applies; everything else about sizing is moot.
        num_positions = 10 ** 9
    # The 4th positional still works so old commands do not silently change
    # meaning, but --offset wins -- an opening range is the one field that
    # must never be ambiguous, and it is now printed at both ends of the run.
    offset = int(positional[3]) if len(positional) > 3 else 0
    if offset_opt is not None:
        offset = offset_opt
    if seed_opt is not None:
        SUBSET_SEED = (None if seed_opt.lower() in ("none", "off", "unseeded")
                       else int(seed_opt))

    if workers_str is None:
        n_workers = max(1, int(N_WORKERS))
    elif workers_str.lower() == "auto" or int(workers_str) == 0:
        # FB-50: `--workers 0` derived from cores ALONE while ENGINE_SMP
        # multiplies the threads inside each of the TWO engines per worker, so
        # `--smp 4 --workers 0` launched ~4x more threads than `--smp 1` did.
        # Every timed SMP number measured that way is the scheduler, not the
        # engine.
        #
        # DELIBERATELY NOT the invariant at line 120 (SMP*N*2 <= cores): at
        # smp=1 that would mean cores/2 workers, but every campaign in the
        # ledger ran at cores-1 (the v55 A/B: 108 workers on 112 threads,
        # ~1.9x oversubscribed). Enforcing the invariant would change the
        # DEFAULT density and break comparability with every banked result.
        # Dividing by ENGINE_SMP holds that density CONSTANT across thread
        # counts, which is what makes an SMP A/B comparable to a 1-thread one.
        cores = mp.cpu_count()
        smp = max(1, int(ENGINE_SMP))
        n_workers = max(1, (cores - 1) // smp)
        if smp > 1:                      # announce it; never change silently
            print(f"[match] --workers 0 with --smp {smp}: {n_workers} workers "
                  f"x {smp} threads x 2 engines = {n_workers * smp * 2} "
                  f"threads on {cores} cores (FB-50: the derivation divides by "
                  f"ENGINE_SMP; it used to ignore it entirely)")
    else:
        n_workers = max(1, int(workers_str))

    for p in (engine1, engine2):
        if not os.path.isfile(p):
            print(f"ERROR: engine file not found: {p!r}")
            return

    if nodes_budget is not None:
        MODE = "nodes"                   # --nodes overrides the module default
    if MODE not in ("clock", "time", "depth", "nodes"):
        # A bad --mode used to survive until the mode-label dicts below and
        # die on a KeyError, after the workers had already started.
        print(f"ERROR: unknown --mode {MODE!r} (clock | time | depth | nodes)")
        return
    mode_cfg = {"mode": MODE, "time_ms": TIME_PER_MOVE_MS, "depth": FIXED_DEPTH,
                "tc_seconds": TC_SECONDS, "tc_increment": TC_INCREMENT,
                "nodes": nodes_budget,
                # FI-88, same re-import reason as ADJUDICATE below: --sf-our-clock
                # sets a module global in the PARENT, and play_game runs in a
                # spawned child that would re-import the default.
                "sf_our_clock": sf_our_clock,
                # Carried in mode_cfg because `spawn` workers RE-IMPORT this
                # module: the module-level ADJUDICATE would come back as its
                # default in every child, which is why this used to travel by
                # environment variable. mode_cfg is already an argument to
                # _worker_loop -> play_game, so it is the honest channel.
                "adjudicate": ADJUDICATE}
    tc_label = f"{TC_SECONDS:.2f}+{TC_INCREMENT:.2f}" if MODE == "clock" else None
    tpm = TIME_PER_MOVE_MS if MODE == "time" else None

    cal_start = None                    # FI-101: compared against at the end
    forced_both = force_nodes_1 is not None and force_nodes_2 is not None
    if MODE == "nodes" and forced_both:
        # CROSS-BOX MODE. The calibration exists to give each engine an equal
        # TIME-equivalent budget on THIS machine, which is exactly what makes a
        # --nodes run un-poolable with the same run on a different box: the
        # budgets are functions of that box's NPS. Naming both budgets outright
        # removes the machine from the experiment, so two boxes play the
        # identical contest and their pentanomials add.
        #
        # The cost is that fairness becomes the caller's problem: pass the same
        # number twice and a slower engine is simply handicapped. Get the pair
        # from ONE calibrated run and reuse it everywhere.
        mode_cfg["nodes_e1"] = int(force_nodes_1)
        mode_cfg["nodes_e2"] = int(force_nodes_2)
        print(f"Forced --nodes budgets (NO calibration, cross-box comparable):"
              f"\n  engine1: {mode_cfg['nodes_e1']:,} nodes/move"
              f"\n  engine2: {mode_cfg['nodes_e2']:,} nodes/move")
    elif MODE == "nodes":
        if force_nodes_1 is not None or force_nodes_2 is not None:
            print("ERROR: --force-nodes-1 and --force-nodes-2 must be given "
                  "TOGETHER. Forcing one side and calibrating the other would "
                  "hand one engine a budget derived from this box's NPS and "
                  "the other a fixed one -- neither fair here nor comparable "
                  "elsewhere.")
            return
        print(f"Calibrating --nodes budgets ({CAL_ROUNDS} interleaved bench "
              "rounds per engine)...")
        cal_start = calibrate_nodes(engine1, engine2, "start")
        for _ln in cal_start["lines"]:
            print(_ln)
        mode_cfg["nodes_e1"] = int(nodes_budget)
        mode_cfg["nodes_e2"] = max(1, int(round(nodes_budget * cal_start["ratio"])))
        print(f"  engine1: {cal_start['nps1']/1e6:.2f}M nps -> "
              f"{mode_cfg['nodes_e1']:,} nodes/move")
        print(f"  engine2: {cal_start['nps2']/1e6:.2f}M nps -> "
              f"{mode_cfg['nodes_e2']:,} nodes/move")

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
    # One description, printed at the top, written into the log header, and
    # repeated in the closing summary. The seed is part of it: the pool is
    # SHUFFLED before slicing, so the same offset under a different seed is a
    # different set of openings, and an offset alone is not provenance.
    openings_desc = (f"positions [{offset}, {offset + len(fens)}) of "
                     f"{FEN_FILE} (shuffle seed "
                     f"{SUBSET_SEED if SUBSET_SEED is not None else 'unseeded'})")

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
                  # The mode string is the experiment's identity in the state
                  # file, so a forced pair must read differently from a
                  # calibrated one: two boxes with the same forced budgets pool
                  # silently, and a calibrated run against them warns.
                  "nodes": (f"nodes e1={mode_cfg.get('nodes_e1')}/"
                            f"e2={mode_cfg.get('nodes_e2')} FORCED"
                            if forced_both else
                            f"nodes {nodes_budget}/move (NPS-calibrated)"),
                  "clock": f"clock {tc_label}"}[MODE])
    # Short, filesystem-safe name for the INSTRUMENT. It goes in the auto-named
    # state file so two campaigns that differ only in budget cannot silently
    # pool: `cengine vs cengine_pc` at 1.75M nodes and the same pair at 5M are
    # different experiments that would otherwise share one file.
    instrument = ({"time": f"t{TIME_PER_MOVE_MS}ms",
                   "depth": f"d{FIXED_DEPTH}",
                   "nodes": f"n{nodes_budget}",
                   "clock": f"tc{TC_SECONDS:g}+{TC_INCREMENT:g}"}[MODE])
    if int(ENGINE_SMP) != 1:
        instrument += f"_smp{int(ENGINE_SMP)}"
    workers_desc = (f"{n_workers} parallel" if parallel else "1 sequential")
    _nn_lines = "".join(x + "\n" for x in _net_lines([e1, e2]))
    banner = (f"Match: {e1.name}  vs  {e2.name}\n"
              + _nn_lines +
              f"Interpreter: {interp}\n"
              f"Mode: {mode_desc}   |   "
              f"{len(fens)} positions x 2 colours = {total_games} games\n"
              f"Workers: {workers_desc}\n"
              f"Openings: {openings_desc}\n"
              + (f"Budget: {fmt_duration_short(total_time)} wall-clock "
                 f"(overrides the game count)\n" if total_time is not None else "")
              + f"Log: {log_path}\n" + "-" * 72)
    print(banner)
    if fh is not None:
        # FI-101: the calibration used to exist only on stdout, so a --nodes
        # log could never be re-checked after the fact. It is the number every
        # game's budget came from; it belongs in the file.
        cal_hdr = ("\n".join(cal_start["lines"]) + "\n"
                   f"  engine1 {cal_start['nps1']/1e6:.2f}M nps, "
                   f"engine2 {cal_start['nps2']/1e6:.2f}M nps\n"
                   if cal_start is not None else "")
        # The OPENING RANGE is a run's provenance: two runs that share it are
        # replays of the same games, not independent samples, and pooling them
        # overstates the precision. It lived only in argv, so a finished run
        # could not be checked -- and a wrong guess about it was made about
        # this very field. Positions, not games: each is played twice.
        fh.write(f"{e1.name} vs {e2.name}\n"
                 f"Interpreter: {interp}\nMode: {mode_desc}\n"
                 f"Openings: {openings_desc}\n"
                 f"{cal_hdr}"
                 f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fh.flush()

    startup_fail = {"n": 0, "why": []}      # FB-59
    tally = {"e1": 0, "e2": 0, "draws": 0, "errors": 0, "completed": 0,
              "penta": {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}, "penta_incomplete": 0}
    start_t = time.time()
    deadline = (start_t + total_time) if total_time is not None else None
    stopped = False
    interrupted = False        # Ctrl-C / SIGTERM: skip the in_q sentinel dance
    _install_signal_handlers()

    # SPRT early-stop state. cfg is None unless --sprt was passed AND sprt.py
    # imported; then the result loop evaluates the LLR as pairs complete and
    # stops the match the moment a bound is crossed.
    if sprt_enable and _sprt is None:
        print("!! --sprt requested but testing/sprt.py could not be imported -- "
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
    # FI-103: --sprt ALWAYS writes a state file, named after the run when the
    # user did not pick one. A no-decision tranche is only worth the compute if
    # a later tranche can pool with it, and that pooling needs the pentanomial
    # + fingerprint this file holds. Forgetting the flag used to mean the state
    # existed nowhere but in the printed ptnml, i.e. re-typed by hand or lost.
    # EVERY run keeps state now, not just --sprt ones, and the name is STABLE
    # (it used to carry a timestamp, which made it unresumable by construction
    # -- the next run auto-named a different file and started from zero).
    # Same two engines -> same file -> the next run continues the pool.
    sprt_resume_auto = not sprt_resume_path
    if sprt_resume_auto:
        # The INSTRUMENT is in the name because the same two engine FILENAMES
        # get reused across campaigns constantly. What the name still cannot
        # see is a change INSIDE an engine file (a toggle flipped, a margin
        # retuned) -- that is what --tag is for, and it is the honest answer
        # now that nothing is hashed. Name the campaign and its state follows
        # it; forget to, and you get one file per engine-pair-and-instrument.
        sprt_resume_path = (f"sprt_{campaign_tag}.json" if campaign_tag else
                            f"sprt_{e1.name}_vs_{e2.name}_{instrument}.json")
    # Prior runs against these same two engines, pooled into every LLR
    # evaluation below. Read before the first game so any warning costs zero
    # compute. No longer gated on a fingerprint -- see sprt_resume_load.
    base_penta = [0] * 5
    prior_runs = []
    prior_next_off = 0     # high-water mark; never allowed to move backwards
    if os.path.exists(sprt_resume_path):
        try:
            base_penta, prior_next_off, prior_runs, note = sprt_resume_load(
                sprt_resume_path, offset)
        except (ValueError, OSError, json.JSONDecodeError) as ex:
            print(f"ERROR: {ex}")
            return
        print(note)
    else:
        print(f"State file {sprt_resume_path!r} does not exist yet -- "
              f"this is run 1; it is created now and refreshed every "
              f"{SAVE_EVERY} games.")
    # This run's provenance record. Pooling is no longer blocked on config
    # equality, so the config has to be RECORDED instead -- otherwise a pooled
    # number could not be audited at all.
    this_run = {"engine1": e1.name, "engine2": e2.name,
                "offset": offset, "positions": len(fens),
                "seed": SUBSET_SEED, "fen_file": FEN_FILE,
                "mode": mode_desc, "smp": int(ENGINE_SMP),
                "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    # The gate is gone, so the CHECK has to be loud instead. Compare against
    # the last recorded run and say what moved. This is the part that used to
    # be a refusal: pooling a 1-thread tranche with a 4-thread one, or two
    # different node budgets, is meaningless -- but it is the operator's call,
    # not the tool's.
    # The last record that actually CARRIES a config. A file may hold
    # metadata-only entries (a hand-seeded pool documents itself with a note),
    # and comparing against one of those silenced the check entirely -- the
    # guard has to skip them, not take prior_runs[-1] on faith.
    prev = next((r for r in reversed(prior_runs) if "engine1" in r), None)
    if prev:
        moved = [(k, prev.get(k), this_run[k])
                 for k in ("engine1", "engine2", "mode", "smp", "seed",
                           "fen_file")
                 if k in prev and prev.get(k) != this_run[k]]
        if moved:
            print("  ** CONFIG CHANGED since the last run in this file -- "
                  "pooling anyway (nothing is gated on it):")
            for k, was, now in moved:
                print(f"       {k}: {was!r} -> {now!r}")
            print("     Different instruments must NOT be pooled. Use --tag "
                  "to give this campaign its own file if that is what you "
                  "meant.")
    runs_log = list(prior_runs) + [this_run]
    # Create it NOW, before a single game is played. A file that only appears
    # at the end is not a crash-safety net -- it is a summary.
    try:
        sprt_resume_save(sprt_resume_path, base_penta,
                         max(prior_next_off, offset), None, runs_log)
    except OSError as ex:
        print(f"!! could not create state file {sprt_resume_path!r}: {ex}")

    # llr/lower/upper/decision refreshed per completed pair; last_n throttles.
    # base_pairs lets the summary say how much of the LLR is pooled history.
    sprt_state = {"cfg": sprt_cfg, "llr": None, "lower": None, "upper": None,
                  "decision": None, "decided": None, "last_n": 0,
                  "base_pairs": sum(base_penta),
                  "resume_path": sprt_resume_path, "resume_auto": sprt_resume_auto}

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
                # FI-82: the LLR is POOLED, so the projection must be too --
                # and the remaining work is what is left of THIS tranche.
                n_pairs = sum(tally["penta"].values()) + sum(base_penta)
                if abs(L) > 1e-6 and n_pairs > 0:
                    bound = hi_b if L > 0 else lo_b
                    proj_pairs = n_pairs * bound / L      # same sign as L
                    side = "accept" if L > 0 else "reject"
                    rem_games = max(0.0, (proj_pairs - n_pairs) * 2)
                    # A near-zero LLR projects to absurd or infinite distances;
                    # quoting "would need 4'000'000'000 more games" reads as a
                    # measurement rather than the "no trend yet" it really is.
                    usable = math.isfinite(rem_games) and rem_games < 5e6
                    if not usable:
                        seg += " -> runs to budget (no usable trend yet)"
                    elif n_pairs < proj_pairs and rem_games <= total_games - played:
                        proj = (_fmt_dur(rem_games / rate) if rate else "…")
                        seg += (f" -> ~{proj} to {side} "
                                f"(approx. {_games(rem_games)} more games)")
                    else:
                        seg += (f" -> runs to budget (approx. "
                                f"{_games(rem_games)} more games to {side})")
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
    shard_used = {"max_pair": -1}        # FI-82: next_offset comes from here

    def _accumulate_pair(g):
        pair_id = (g["round"] - 1) // 2
        slot_idx = (g["round"] - 1) % 2
        # FI-82: how far into the shard this tranche actually got. An
        # interrupted run must not burn positions it never played -- but it
        # must not hand the next tranche a position it DID play either, so
        # the mark is the highest pair touched, not the count completed
        # (results arrive out of order across workers).
        if pair_id > shard_used["max_pair"]:
            shard_used["max_pair"] = pair_id
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

    def _save_state_now():
        """Refresh the state file mid-run. Best-effort: a campaign must never
        die because a disk hiccuped, and the ptnml on stdout is the recovery
        path either way. Writes via a .tmp + os.replace, so a kill during the
        write cannot leave a truncated file behind."""
        try:
            pooled = [base_penta[k] + tally["penta"][k] for k in range(5)]
            # HIGH-WATER MARK. A run that (re)played an EARLIER shard must
            # not drag next_offset backwards -- that would hand the next run
            # a range this file has already consumed, turning one overlap
            # into a permanent one.
            nxt = max(prior_next_off,
                      offset + min(len(fens), shard_used["max_pair"] + 1))
            this_run["positions_played"] = min(len(fens),
                                               shard_used["max_pair"] + 1)
            sprt_resume_save(sprt_resume_path, pooled, nxt, sprt_state,
                             runs_log)
        except (OSError, ValueError):
            pass

    def _update_sprt():
        """Refresh the SPRT LLR from the live pentanomial counts and latch a
        decision when a bound is crossed. Cheap (a 5-bucket GSPRT), re-run
        only when a NEW pair has completed and we're past the minimum sample.
        Any stats hiccup is swallowed -- the match must never die on it."""
        cfg = sprt_state["cfg"]
        if cfg is None or sprt_state["decided"] is not None:
            return
        # FI-82: pooled counts, so a resumed campaign neither restarts its LLR
        # nor re-waits out SPRT_MIN_PAIRS on the new tranche alone.
        counts = [base_penta[k] + tally["penta"][k] for k in range(5)]
        n_pairs = sum(counts)
        if n_pairs < SPRT_MIN_PAIRS or n_pairs == sprt_state["last_n"]:
            return
        sprt_state["last_n"] = n_pairs
        try:
            r = _sprt.evaluate(counts, cfg["elo0"], cfg["elo1"],
                               cfg["model"], cfg["alpha"], cfg["beta"])
        except Exception as ex:
            # FB-54 rider: a silently-caught error in the SEQUENTIAL TEST is
            # the one place a wrong number looks like a right one -- the run
            # would just keep playing with a stale LLR on screen. Say it once
            # (not per pair) and carry on; the match must never die on stats.
            if not sprt_state.get("err_shown"):
                sprt_state["err_shown"] = True
                print(f"\n!! SPRT evaluation failed ({type(ex).__name__}: "
                      f"{ex}) -- the LLR shown is STALE from here on; the "
                      f"ptnml in the summary is still authoritative.",
                      file=sys.stderr, flush=True)
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
                tag = f"draw  {short_reason(g['reason'])}"
            elif g["winner"] is e1:
                tally["e1"] += 1
                tag = f"{e1.name} wins  {short_reason(g['reason'])}"
            else:
                tally["e2"] += 1
                tag = f"{e2.name} wins  {short_reason(g['reason'])}"
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
        if played % SAVE_EVERY == 0:
            _save_state_now()      # a run killed at game 9,000 keeps 9,000
        line = (f"[{played:>4}/{total_games}] "
                f"{wn} vs {bn}  ->  {g['result']:>7}  {tag:<24} | {run}")
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
                # FB-59: a worker that died during engine LOAD reports here
                # instead of silently never contributing.
                if isinstance(r, dict) and r.get("reason") == "WORKER_STARTUP_FAILED":
                    startup_fail["n"] += 1
                    startup_fail["why"].append(r.get("error", "?"))
                    _clear_status()
                    print(f"\n!! WORKER STARTUP FAILED ({startup_fail['n']}): "
                          f"{r.get('error', '?')}", file=sys.stderr, flush=True)
                    if startup_fail["n"] >= max(1, n_workers // 4):
                        raise EngineError(
                            f"{startup_fail['n']} of {n_workers} workers could "
                            f"not load their engines -- aborting rather than "
                            f"running a short, possibly one-sided campaign")
                    continue
                g = _unpack_result(r, e1, e2)
                handle_result(g, g["round"])
                if deadline and time.time() >= deadline:
                    raise _TimeStop()
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
                if deadline and time.time() >= deadline:
                    raise _TimeStop()
                if sprt_state["decided"]:
                    raise _SPRTStop()

    except _TimeStop:
        stopped = True
        # Take the TERMINATE path, not the sentinel dance. --total-time sizes
        # the schedule to the whole remaining pool, so N sentinels would queue
        # behind a backlog of hundreds of thousands of jobs and never be seen:
        # the joins time out one by one and the process hangs for minutes after
        # the summary is already printed. _shutdown_workers' own docstring says
        # this; it simply never applied at A/B budgets before.
        interrupted = True
        _clear_status()
        print(f"\n[--total-time {fmt_duration_short(total_time)} reached -- "
              f"stopping; the games played are a complete result]")
    except _SPRTStop:
        stopped = True
        _clear_status()
        dec = sprt_state["decided"]
        verdict = ("ACCEPT H1 -- change is good (ship)" if dec == "H1"
                   else "ACCEPT H0 -- change rejected")
        _pooled = sum(tally["penta"].values()) + sum(base_penta)
        print(f"\n[SPRT decided: {verdict} @ {_pooled:,} pairs"
              + (f" ({sum(base_penta):,} pooled from earlier tranches)"
                 if sum(base_penta) else "")
              + f", LLR {sprt_state['llr']:+.3f} -- stopping early]")
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
        # FI-101: re-measure the calibration on the machine the campaign
        # ACTUALLY ran on. Bounded (~10s) and wrapped, because nothing here is
        # allowed to cost the summary -- that is the point of the whole run.
        cal_lines = None
        if cal_start is not None:
            try:
                print("Re-measuring the NPS calibration (FI-101)...")
                cal_lines = calibration_drift(
                    cal_start, calibrate_nodes(engine1, engine2, "end"),
                    interrupted=interrupted)
            except Exception as ex:
                cal_lines = [f"NPS calibration drift (FI-101): "
                             f"end-of-run measurement failed -- {ex}"]
        write_summary(fh, e1, e2, tally, total_games, start_t, stopped,
                      n_workers=n_workers, sprt_info=sprt_state,
                      mode_desc=mode_desc, startup_fail=startup_fail,
                      cal_lines=cal_lines, openings=openings_desc)
        # FI-82: pooled state, written on EVERY exit path (clean finish,
        # Ctrl-C/SIGTERM, SPRT decision) -- the tranche that follows resumes
        # from here, and next_offset is what makes its shard disjoint.
        if sprt_resume_path:
            pooled = [base_penta[k] + tally["penta"][k] for k in range(5)]
            # Positions this tranche consumed: the whole shard on a clean
            # finish, only what it reached when it was cut short.
            next_off = max(prior_next_off,
                           offset + min(len(fens), shard_used["max_pair"] + 1))
            this_run["positions_played"] = min(len(fens),
                                               shard_used["max_pair"] + 1)
            try:
                sprt_resume_save(sprt_resume_path, pooled,
                                 next_off, sprt_state, runs_log)
                print(f"\nSPRT state written to: {sprt_resume_path} "
                      f"({sum(pooled):,} pooled pairs; next tranche must use "
                      f"--offset >= {next_off})")
                # The state file lives on whatever box ran the tranche, and a
                # rented VM has no git credentials -- setting them up per box
                # is not a workflow anyone will keep doing. So print the state
                # INLINE: the run summary is pasted anyway, and this one line
                # is everything needed to rebuild the file elsewhere. Zero
                # extra steps on the box, nothing to configure.
                print(f"STATE-LINE {os.path.basename(sprt_resume_path)}: "
                      f"penta {'/'.join(str(v) for v in pooled)} "
                      f"pairs {sum(pooled)} next_offset {next_off} "
                      f"llr {sprt_state.get('llr')}")
                if push_state:
                    push_state_file(sprt_resume_path)
            except OSError as ex:
                print(f"\n!! could not write SPRT state {sprt_resume_path!r}: "
                      f"{ex}  (the ptnml above is the recovery path)")
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
