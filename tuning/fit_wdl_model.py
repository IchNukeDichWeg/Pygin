#!/usr/bin/env python3
"""
fit_wdl_model.py
=================
Fits a Stockfish-style Win/Draw/Loss (WDL) model for engine.py: given a
centipawn score and a game phase, predicts the probability that THIS
engine's own reported score of that size, at that point in the game,
actually turns into a win / draw / loss -- using engine.py's own historical
match results as the training data (see the "Stockfish WDL model" writeup
in chat for how win_rate_model() works upstream).

Stage 1 (extract): scan every match log under "New logs/" and "logs/". A
log file is USABLE when the two sides form a near-equal engine-family
pairing per near_equal_pair() (C-era snapshots >= v31, numbered pairs must
be adjacent versions, cengine pairs with its contemporaries -- both sides'
samples are then extracted, since each side's score -> outcome mapping is
unbiased against a near-equal opponent), or when the file is in
NEAR_EQUAL_STOCKFISH_LOGS (a Stockfish match at matched strength; only the
engine-family side extracts). Of the sides a usable log offers, only those
on the selected EVAL FAMILY are extracted (--eval-family, default "hce"):
NNUE cp and hand-crafted cp are different scales and are never pooled into
one fit. Mismatched-strength opponents, odds games and
" copy" duplicates are excluded -- those would bias the fit. For every
usable game, replay the "--- Engine Logs ---" block move by move with
python-chess, pull each usable side's "[<name>] move X: info ... score cp
C" lines (mate-score lines are skipped -- they're a separate UCI reporting
convention), compute the phase at that ply with the exact formula engine.py
uses (PHASE_WEIGHTS / PHASE_MAX, engine.py:816-820), and pair it with that
side's own final result in the game (1.0 win / 0.5 draw / 0.0 loss).
Writes the (cp, phase, result) triples to a CSV.

Stage 2 (fit): bucket samples by phase, fit a 2-parameter logistic
  win_rate(cp) = 1 / (1 + exp((a - cp) / b))
to the empirical win rate per phase bucket (scipy curve_fit), then fit
smooth 3rd-order polynomials a(phase) and b(phase) across buckets -- same
shape Stockfish's own win_rate_model() uses -- so any phase in between
interpolates cleanly instead of jumping between discrete buckets. Prints a
win_rate_model()/wdl() snippet and writes data/wdl_model<_family>.json,
which match.py's adjudication reads directly.

Stage 3 (sync): write the fitted coefficients from data/wdl_model*.json into
cuci.py's own `_WDL_AS`/`_WDL_BS` (hce) or `_WDL_AS_NNUE`/`_WDL_BS_NNUE`
(nnue) constants, so the runtime cannot drift from the JSON. Which PAIR the
UCI layer reads is chosen by the armed eval family, not by this script. v58
armed the net, so the NNUE pair is the LIVE one now; the hce pair is what a
CPU without SIMD falls back to.

Usage:
    python3 fit_wdl_model.py                     # extract + fit + sync, hce
    python3 fit_wdl_model.py --both              # BOTH families, one run

  choosing the eval scale (the two are never pooled -- different cp scales):
    --eval-family {hce,nnue}    which scale to fit (default: hce)
    --nnue                      shorthand for --eval-family nnue; writes
                                data/wdl_model_nnue.json rather than
                                overwriting the hce model
    --both                      fit hce then nnue, each into its own corpus
                                and JSON

  choosing which logs count as training data:
    --min-era N                 minimum engine<N> snapshot to accept
                                (default: the whole C era)
    --cengine-since YYYY-MM-DD  also require dev-build ("cengine") logs to be
                                dated on/after this -- the name alone cannot
                                tell one eval era from another

  which stages to run:
    --extract-only              write the training CSV, do not fit
    --fit-only                  skip extraction, reuse an existing CSV
    --sync-only                 do not fit: push the coefficients already in
                                data/wdl_model*.json into cuci.py and exit
    --data-file PATH            CSV path for extracted samples
                                (default: wdl_training_data_<family>.csv)

Needs numpy + scipy (both already installed in this environment, though not
otherwise used by the project -- only this offline analysis script imports
them; nothing added to engine.py/match.py's runtime dependencies).
"""
# Path shim: this script moved into a subfolder on 2026-07-24 but
# still imports the engine modules that live at the repo root.
import os as _os, sys as _sys
_ROOT_ = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_ROOT_, _os.path.join(_ROOT_, "lib")]

import argparse
import csv
import glob
import math
import os
import re
import sys
from collections import defaultdict

import chess
import numpy as np
from scipy.optimize import curve_fit

import interruptible

# ====================================================================== #
#  CONFIG
# ====================================================================== #
# Anchored to the REPO, not the cwd. These used to be bare relative names, so
# running the script from inside tuning/ scanned tuning/New logs -- which does
# not exist -- and reported "0 files scanned, 0 usable, nothing to fit". That
# reads as "there is no data for this family", which is a different and much
# more alarming statement than "you are in the wrong directory". The fitted
# model was already written repo-relative via __file__; these now match it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIRS = [os.path.join(_REPO, "New logs"), os.path.join(_REPO, "logs")]
DATA_CSV = os.path.join(_REPO, "wdl_training_data.csv")

# Mirrors engine.py's PHASE_WEIGHTS / PHASE_MAX (engine.py:816-820) exactly:
# knights+bishops weight 1, rooks weight 2, queens weight 4, capped at 24.
# If engine.py's phase weights ever change, update this too, or the fitted
# model's phase axis will drift from what the engine reports at runtime.
PHASE_MAX = 24

# (the old single-side ENGINE_TAG filter was replaced by NEAR_EQUAL_BASES
#  below -- both sides of a near-equal match are extracted now)

CP_BIN_WIDTH = 20              # cp bucket width for the per-phase win-rate curve
CP_CLIP = 1000                 # ignore |cp| beyond this (matches Stockfish's clamp)
MIN_SAMPLES_PER_CP_BIN = 20    # a cp bucket needs at least this many samples to count
MIN_SAMPLES_PER_PHASE = 500    # a phase bucket needs at least this many samples
                                #   total before it's included in the polynomial fit

# Below this phase, self-play games rarely linger long enough to leave a
# representative sample -- what little data DOES land there skews toward
# already-decisive positions (a game that reaches a near-empty board is
# usually already resolved), not "here's what a 150cp edge means with a
# king and two pawns left." Determined by eyeballing this project's own
# per-phase fits: phase 6-24 forms a smooth, roughly monotonic curve; phase
# 0-5 jumps around with no trend (e.g. phase 2's "a" comes out higher than
# phase 1 AND phase 3). Buckets below this are excluded from the polynomial
# fit and clamped away from at inference time. Re-examine this constant if
# you re-run the fit on a much larger/different corpus.
MIN_PHASE_FOR_FIT = 6


# ====================================================================== #
#  Stage 1: extraction
# ====================================================================== #
GAME_SPLIT_RE = re.compile(r'^=== Game \d+ ===\s*$', re.MULTILINE)
FEN_RE = re.compile(r'^FEN: (.+)$', re.MULTILINE)
WHITE_RE = re.compile(r'^Engine \d \(White\): (.+)$', re.MULTILINE)
BLACK_RE = re.compile(r'^Engine \d \(Black\): (.+)$', re.MULTILINE)
RESULT_RE = re.compile(r'\[Result "([^"]+)"\]')
MOVE_RE = re.compile(
    r'^\[(?P<tag>[^\]]+)\] move (?P<san>[^:\s]+): info depth \d+ score '
    r'(?:cp (?P<cp>-?\d+)|mate (?P<mate>-?\d+))')


def board_phase(board):
    """Mirror engine.py's tapered-eval phase exactly (PHASE_WEIGHTS/PHASE_MAX,
    engine.py:816-820): knights+bishops weight 1, rooks weight 2, queens
    weight 4, capped at PHASE_MAX."""
    phase = (board.knights.bit_count() + board.bishops.bit_count()
              + board.rooks.bit_count() * 2 + board.queens.bit_count() * 4)
    return min(phase, PHASE_MAX)


# Near-equal pairing RULE (2026-07-18, replaces the old hand-maintained
# NEAR_EQUAL_BASES set -- which pre-listed engine52-60 before they existed
# and had no guard against cross-era pairings like engine43-vs-engine51,
# a ~+30 Elo gap that would have silently biased the fit):
#   - A numbered C-era snapshot base ("engine<N>", N >= 31) auto-qualifies
#     the day it exists -- no list edit at each version ship.
#   - Two NUMBERED bases are near-equal only if ADJACENT (|N-M| <= 1): the
#     only snapshot-vs-snapshot pairing this project ever records, and every
#     adjacent step measured within ~14 Elo. Wider gaps compound (v44->v45
#     alone was +13.5) and are refused even though both sides qualify.
#   - "cengine" (the live dev build) pairs with any qualifying base: every
#     recorded cengine log plays it against its CONTEMPORARY snapshot, so
#     the gap is one armed candidate (-5..+18 Elo observed), never an era.
#   - Python-era bases stay excluded ("engine" plain, engine15/19/20, ...):
#     a depth-~8 engine's cp -> outcome conversion is exactly what the v31
#     refit stopped modeling, and excluding plain "engine" also drops the
#     lopsided 29-1-0 cengine-vs-engine gate log (~700 Elo gap).
#   - Odds logs, " copy" duplicates and Stockfish logs are excluded above/
#     below regardless (SF scores are a different eval scale anyway).
# ERA CUTOFF -- OPT-IN, via --min-era / --cengine-since. The WDL model maps
# THIS engine's reported cp to an outcome probability, so in principle every
# sample should come from ONE eval scale.
# v53 (the Texel retune) moved the scale materially -- MG minors and rooks
# came down ~10% (N 353->307, R 489->443) -- so a v52-era cp and a v53-era cp
# of the same number no longer describe the same position. Mixing eras blurs
# the fit toward an average of two scales that the engine never uses.
#
# Two guards are needed, not one:
#   * numbered snapshots: _MIN_C_ERA_SNAPSHOT below rises to 53;
#   * "cengine": the dev build auto-qualifies BY NAME and every log this
#     project has ever written calls it that, so the name alone cannot tell a
#     v52-era cengine log from a v53-era one. The filename date can, hence
#     CENGINE_MIN_DATE -- without it the entire v52 corpus walks back in
#     through the side door.
#
# The default is the v53 era: engine53+ snapshots, and dev-build ("cengine")
# logs dated on/after v53's ship date. Every campaign from FI-12 onward runs
# cengine-vs-engine53 and qualifies automatically. Older corpora are still
# one flag away -- `--min-era 52` refits on the v52 era, and `--min-era 31
# --cengine-since ""` reproduces the pre-v53 behaviour exactly.
CENGINE_MIN_DATE = "2026-07-22"
_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_")

NEAR_EQUAL_EXTRA = {
    "cengine",
    "engine_qtt_off",   # v34-era P-44 isolation build, paired vs its
                        # contemporary cengine only -- near-equal like it
    # 2026-08-28 campaign arms. Same shape as engine_qtt_off: a single toggle
    # or table size flipped against an otherwise identical build, so the
    # pairing is near-equal BY CONSTRUCTION -- both sides run the same v12 net
    # and differ by one config line. These are the best WDL corpus available
    # (20,000 games, identical evals both sides) and they were being dropped
    # in full, because the arm names carry no "nnue" for _NNUE_RE to find.
    # Family comes from the log date via NNUE_DEFAULT_SINCE, as for cengine.
    "engine_deadtag_on",    # FI-115 dead-entry TT tag
    "engine_deadtag_off",
    "engine_hash_small",    # TT sizing, 24 MiB vs 192 MiB
    "engine_hash_default",
    "engine_grow_on",       # FI-116 growing TT, vs engine_nnue_v12
}
_BASE_NUM_RE = re.compile(r"^engine(\d+)$")
_MIN_C_ERA_SNAPSHOT = 53

# EVAL-FAMILY guard (2026-07-29). The era guards above know about VERSIONS,
# not about which EVAL produced the cp. A net's output and the hand-crafted
# eval are different scales -- the same blur the v53 guard exists to prevent,
# only bigger -- so sides are ALSO classified by eval family and only the
# selected family is ever extracted (--eval-family, default "hce"). An
# NNUE-vs-HCE match is still a near-equal PAIRING: it contributes its NNUE
# side to the NNUE corpus and its HCE side to the HCE corpus, never both to
# one, exactly like NEAR_EQUAL_STOCKFISH_LOGS contributes one side.
# NNUE arms are engine_nnue*.py (cengine subclasses with USE_NNUE=True). A
# new net changes the output scale with it, so NNUE logs get the same
# date gate cengine has; NNUE_MIN_DATE is the first log of the current net.
# When USE_NNUE becomes the DEFAULT, the un-suffixed names (cengine,
# engine<N>) start reporting NNUE cp under the old names -- set
# NNUE_DEFAULT_SINCE to that ship date then; nothing else needs to change.
_NNUE_RE = re.compile(r"(?:^|_)nnue(?:_|$)")
NNUE_MIN_DATE = "2026-08-22"     # nnue_v12_bf86c4ced057, the net v60 ships
                                 # (added 2026-08-22 01:55). This tracks the
                                 # CURRENT net on purpose: a new net is a new
                                 # cp scale, and leaving this at the old
                                 # nnue_v3 date (2026-07-28) silently pooled
                                 # v3..v12 cp into one fit -- the exact blur
                                 # the family guard below exists to prevent.
                                 # BUMP THIS WHENEVER THE SHIPPED NET CHANGES.
NNUE_DEFAULT_SINCE = "2026-08-03"  # v58 shipped USE_NNUE=True: from this date
                                 # the un-suffixed dev names (cengine) report
                                 # NNUE cp. Was left None until 2026-08-10, so
                                 # every post-v58 cengine/engine58 side was
                                 # classified HCE and polluted that corpus
                                 # with net-scale cp.
NNUE_DEFAULT_VERSION = 58        # numbered snapshots switch family by NUMBER,
                                 # not by log date: engine58+ is NNUE in any
                                 # log, engine57- is HCE forever. The date
                                 # gate alone would have reclassified an
                                 # engine57 side in a post-cutoff log.
GLOBAL_SINCE = None              # --since: refuse EVERY log dated before this,
                                 # snapshots included. Exists for instrument
                                 # breaks: the 2026-08-07 phantom-repetition
                                 # fix (fc82cb7) changed game RESULTS, so all
                                 # earlier logs carry draw-biased outcomes and
                                 # a fit over them learns the bug.
EVAL_FAMILY = "hce"              # "hce" | "nnue" | None = don't filter,
                                 #   for scale-free consumers only (texel.py)


def _base_num(base):
    m = _BASE_NUM_RE.match(base)
    return int(m.group(1)) if m else None


def _log_date(path):
    """The YYYY-MM-DD stamped into a match-log filename, or None."""
    m = _DATE_RE.search(os.path.basename(path or ""))
    return m.group(1) if m else None


def _date_ok(path, since):
    """Is this log dated on/after `since`? No date in the name -> refuse: an
    unknown era is not a safe one. `since` falsy -> no date gate."""
    if not since:
        return True
    d = _log_date(path)
    return d is not None and d >= since


def eval_family(base, path=None):
    """Which eval SCALE this side's reported cp is on: 'nnue', 'hce', or
    None for a base this script does not recognise as an engine arm."""
    if _NNUE_RE.search(base):
        return "nnue"
    if base == "stockfish_engine":
        return "sf"                       # SF's own cp scale -- third family;
                                          # extractable only from logs listed
                                          # in NEAR_EQUAL_STOCKFISH_LOGS
    n = _base_num(base)
    if n is not None:                     # numbered snapshot: family by number
        return "nnue" if n >= NNUE_DEFAULT_VERSION else "hce"
    if base in NEAR_EQUAL_EXTRA:          # dev build: family by log date
        if NNUE_DEFAULT_SINCE and _date_ok(path, NNUE_DEFAULT_SINCE):
            return "nnue"
        return "hce"
    return None


def _side_in_era(base, path=None):
    """Is this side a near-equal-class arm from an accepted era? Says nothing
    about WHICH eval family it is -- that is _side_usable's half."""
    if eval_family(base, path) is None:
        return False
    if base == "stockfish_engine":
        return (os.path.splitext(os.path.basename(path or ""))[0]
                in NEAR_EQUAL_STOCKFISH_LOGS)
    if _NNUE_RE.search(base):
        return _date_ok(path, NNUE_MIN_DATE)
    if base in NEAR_EQUAL_EXTRA:
        # Era-gate the dev build by the log's own date (see CENGINE_MIN_DATE).
        return _date_ok(path, CENGINE_MIN_DATE)
    n = _base_num(base)
    return n is not None and n >= _MIN_C_ERA_SNAPSHOT


def _side_usable(base, path=None):
    """Extractable INTO THE SELECTED CORPUS: right era AND right eval family."""
    if not _side_in_era(base, path):
        return False
    return EVAL_FAMILY is None or eval_family(base, path) == EVAL_FAMILY


def near_equal_pair(wb, bb, path=None):
    """Both sides in-era AND the pairing itself is near-equal (see rule).
    This is a STRENGTH judgement only -- an NNUE arm vs its contemporary
    snapshot is near-equal; which of the two sides actually gets extracted
    is the eval-family question, answered per side by _side_usable()."""
    if not (_side_in_era(wb, path) and _side_in_era(bb, path)):
        return False
    wn, bn = _base_num(wb), _base_num(bb)
    if wn is not None and bn is not None:
        return abs(wn - bn) <= 1        # numbered pairs: adjacent eras only
    return True                          # cengine/nnue/qtt_off vs a listed base

# Specific Stockfish match logs where Stockfish was configured within a few
# Elo of the engine, making the engine's own samples from them unbiased.
# Only engine-family sides are ever extracted (Stockfish's scores are on a
# different eval scale regardless); the generic stockfish exclusion still
# applies to every other SF log (unknown / large strength gaps). EMPTY in
# the v31 era: the one qualifying log (SF-2450 vs v25-era engine, -8 +/-14)
# paired the OLD engine -- no near-equal SF pairing exists for the C core
# (limited-SF is retired as an instrument anyway).
NEAR_EQUAL_STOCKFISH_LOGS = {
    # "engine_vs_stockfish_engine_2026-07-04_02-26-12_31615",  # v25 era
    # v58 vs SF-18 @ UCI_Elo 2900, 45.45% over 1,000 games on the corrected
    # harness -- near-equal by measurement. LOST 2026-08-10: the log file was
    # deleted from the repo root before the sf fit ran, so the sf corpus is
    # EMPTY until the next near-equal SF yardstick run. Add that run's
    # basename here (this list is a deliberate allowlist -- a strength-matched
    # SF pairing is a measurement decision, not something to autodetect),
    # then: python3 tuning/fit_wdl_model.py --eval-family sf --since <date>.
    # "cengine_vs_stockfish_engine_2026-08-08_00-45-12_70335",
}


# Skip bookkeeping, so a dropped corpus is never silent (2026-07-29: the
# NNUE arms were dropped in full by name alone, and nothing said so).
# Python-era experiment arms are NOT news -- flag an unplaceable base only
# when it turns up in a CURRENT-era log, i.e. a new or renamed arm.
UNRECOGNISED_BASES = defaultdict(int)     # base -> files skipped
OTHER_FAMILY_PAIRS = defaultdict(int)     # "wb vs bb" -> files skipped


def _note_skip(wb, bb, path):
    """Record a near-equal-eligible-looking log we are dropping anyway."""
    for base in (wb, bb):
        if eval_family(base, path) is None and _date_ok(path, CENGINE_MIN_DATE) \
                and CENGINE_MIN_DATE:
            UNRECOGNISED_BASES[base] += 1
    if _side_in_era(wb, path) and _side_in_era(bb, path):
        OTHER_FAMILY_PAIRS[f"{wb} vs {bb}"] += 1


def classify_file(path):
    """Peek at the first game's header to decide whether this whole match
    log is usable. One log file = one match between a FIXED pair of engines
    (colours alternate per game, identities don't), so a single header check
    is representative of every game in the file -- no need to scan further."""
    base_l = os.path.basename(path).lower()
    if "odds" in base_l:
        return False
    if " copy" in base_l:
        return False    # Finder duplicates of logs that also exist under the
                        # original name -- scanning both double-counts games
    white_path = black_path = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                m = WHITE_RE.match(line)
                if m:
                    white_path = m.group(1)
                m = BLACK_RE.match(line)
                if m:
                    black_path = m.group(1)
                if white_path and black_path:
                    break
    except OSError:
        return False
    if white_path is None or black_path is None:
        return False
    wb = os.path.splitext(os.path.basename(white_path))[0]
    bb = os.path.splitext(os.path.basename(black_path))[0]
    if wb == bb:
        return False    # identical tags -> move lines can't be attributed
    if os.path.splitext(os.path.basename(path))[0] in NEAR_EQUAL_STOCKFISH_LOGS:
        return True     # near-equal SF match; only the engine side extracts
    # Pairing near-equal AND at least one side on the selected eval family
    # -> that side (or both, when they share the family) is extracted.
    if near_equal_pair(wb, bb, path) and (_side_usable(wb, path)
                                          or _side_usable(bb, path)):
        return True
    _note_skip(wb, bb, path)
    return False


def iter_game_blocks(text):
    """Yield the text of each '=== Game N ===' block (marker line excluded)."""
    matches = list(GAME_SPLIT_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield text[start:end]


def process_block(block, samples, stats, path=None):
    """Extract (cp, phase, result) samples from one game block -- for EVERY
    side whose base is in NEAR_EQUAL_BASES (both sides of a near-equal
    engine-family match are equally valid training data; a near-equal
    Stockfish match contributes its engine-family side only, since
    "stockfish_engine" is never usable)."""
    fen_m = FEN_RE.search(block)
    white_m = WHITE_RE.search(block)
    black_m = BLACK_RE.search(block)
    if not (fen_m and white_m and black_m):
        stats["skipped_no_header"] += 1
        return
    white_base = os.path.splitext(os.path.basename(white_m.group(1).strip()))[0]
    black_base = os.path.splitext(os.path.basename(black_m.group(1).strip()))[0]

    result_m = RESULT_RE.search(block)
    if not result_m:
        stats["skipped_no_result"] += 1
        return
    result = result_m.group(1)
    if result == "1-0":
        w_score, b_score = 1.0, 0.0
    elif result == "0-1":
        w_score, b_score = 0.0, 1.0
    elif result == "1/2-1/2":
        w_score = b_score = 0.5
    else:
        stats["skipped_unfinished"] += 1
        return

    # tag -> that side's own final result, for every extractable side.
    side_score = {}
    if _side_usable(white_base, path):
        side_score[white_base] = w_score
    if _side_usable(black_base, path):
        side_score[black_base] = b_score
    if not side_score:
        stats["skipped_no_usable_side"] += 1
        return

    try:
        board = chess.Board(fen_m.group(1).strip())
    except ValueError:
        stats["skipped_bad_fen"] += 1
        return

    n_added = 0
    for line in block.splitlines():
        m = MOVE_RE.match(line.strip())
        if not m:
            continue
        s = side_score.get(m.group("tag"))
        if s is not None and m.group("cp") is not None:
            cp_val = int(m.group("cp"))
            if abs(cp_val) <= CP_CLIP:
                samples.append((cp_val, board_phase(board), s))
                n_added += 1
        try:
            board.push_san(m.group("san"))
        except ValueError:
            stats["aborted_replay"] += 1
            break     # board state from here on is unreliable; stop this game
    stats["games_used"] += 1
    stats["samples_added"] += n_added


def extract_all(log_dirs):
    samples = []
    stats = defaultdict(int)
    all_files = []
    missing = []
    for d in log_dirs:
        if not os.path.isdir(d):
            missing.append(d)
            continue
        all_files.extend(sorted(glob.glob(os.path.join(d, "*.txt"))))
    # Say it. An empty scan and an absent directory produce the same "0 usable"
    # ending, and only one of them means what it looks like.
    for d in missing:
        print(f"  !! no such log directory: {d}")
    if not all_files:
        print(f"  !! nothing to scan -- 'no usable samples' below would be "
              f"about the SEARCH PATH, not about the corpus")
    print(f"Scanning {len(all_files)} log files across "
          f"{[os.path.relpath(d, _REPO) for d in log_dirs]} ...")

    files_used = 0
    dropped_old = 0
    for path in all_files:
        if not _date_ok(path, GLOBAL_SINCE):
            dropped_old += 1        # counted, reported after the loop: a
            continue                # silently shrunken corpus reads as bug
        if not classify_file(path):
            continue
        files_used += 1
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError as ex:
            print(f"  [skip] {path}: {ex}")
            continue
        for block in iter_game_blocks(text):
            process_block(block, samples, stats, path)
        if files_used % 10 == 0:
            print(f"  ...{files_used} usable files processed, "
                  f"{len(samples):,} samples so far")

    if dropped_old:
        print(f"  --since {GLOBAL_SINCE}: {dropped_old} log files refused as "
              f"pre-cutoff (phantom-draw era or undated)")
    print(f"\nDone. {len(all_files)} files scanned, {files_used} usable, "
          f"{stats['games_used']:,} games parsed, "
          f"{len(samples):,} (cp, phase, result) samples extracted.")
    if OTHER_FAMILY_PAIRS:
        n = sum(OTHER_FAMILY_PAIRS.values())
        print(f"  {n} in-era file(s) skipped: no '{EVAL_FAMILY}'-family side "
              f"(eval scales are never pooled -- refit with --eval-family "
              f"for the other corpus):")
        for pair, k in sorted(OTHER_FAMILY_PAIRS.items(), key=lambda kv: -kv[1]):
            print(f"      {k:>4}x  {pair}")
    if UNRECOGNISED_BASES:
        print("  WARNING -- current-era logs skipped on an UNRECOGNISED base. "
              "If one of these is a real arm, teach eval_family() about it "
              "or the corpus just shrank in silence:")
        for base, k in sorted(UNRECOGNISED_BASES.items(), key=lambda kv: -kv[1]):
            print(f"      {k:>4}x  {base}")
    for k, v in sorted(stats.items()):
        if k not in ("games_used", "samples_added"):
            print(f"  {k}: {v:,}")
    return samples


def write_csv(samples, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cp", "phase", "result"])
        w.writerows(samples)


def read_csv(path):
    samples = []
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        next(r)   # header
        for row in r:
            samples.append((int(row[0]), int(row[1]), float(row[2])))
    return samples


# ====================================================================== #
#  Stage 2: fitting
# ====================================================================== #
def _logistic(x, a, b):
    return 1.0 / (1.0 + np.exp((a - x) / b))


def fit_phase_curve(cps, wins):
    """Bin (cp, win-indicator) samples into fixed-width cp buckets, fit the
    2-parameter logistic to the empirical win rate per bucket via weighted
    least squares. Returns (a, b) or None if there isn't enough spread of
    populated buckets to fit reliably."""
    cps = np.asarray(cps, dtype=float)
    wins = np.asarray(wins, dtype=float)
    edges = np.arange(-CP_CLIP, CP_CLIP + CP_BIN_WIDTH, CP_BIN_WIDTH)
    idx = np.digitize(cps, edges)
    centers, rates, weights = [], [], []
    for b in range(1, len(edges)):
        mask = idx == b
        n = int(mask.sum())
        if n < MIN_SAMPLES_PER_CP_BIN:
            continue
        centers.append((edges[b - 1] + edges[b]) / 2.0)
        rates.append(float(wins[mask].mean()))
        weights.append(math.sqrt(n))          # more weight to well-populated bins
    if len(centers) < 6:
        return None
    centers = np.array(centers)
    rates = np.array(rates)
    weights = np.array(weights)
    try:
        popt, _ = curve_fit(_logistic, centers, rates, p0=[0.0, 200.0],
                            sigma=1.0 / weights, maxfev=20000)
    except RuntimeError:
        return None
    return float(popt[0]), float(popt[1])


def fit_wdl_model(samples):
    by_phase = defaultdict(list)
    for cp, phase, y in samples:
        by_phase[phase].append((cp, 1.0 if y == 1.0 else 0.0))

    print("\nPer-phase logistic fits (a = 50%-win cp point, b = spread):")
    per_phase = {}
    n_per_phase = {}
    for phase, entries in sorted(by_phase.items()):
        if len(entries) < MIN_SAMPLES_PER_PHASE:
            print(f"  phase {phase:>2}: n={len(entries):>8,}  (skipped, < "
                  f"{MIN_SAMPLES_PER_PHASE} samples)")
            continue
        cps = [e[0] for e in entries]
        wins = [e[1] for e in entries]
        fit = fit_phase_curve(cps, wins)
        if fit is None:
            print(f"  phase {phase:>2}: n={len(entries):>8,}  (skipped, fit failed)")
            continue
        per_phase[phase] = fit
        n_per_phase[phase] = len(entries)
        print(f"  phase {phase:>2}: n={len(entries):>8,}  "
              f"a={fit[0]:+8.2f}  b={fit[1]:7.2f}")

    if len(per_phase) < 4:
        print("\nNot enough well-populated phase buckets for a stable "
              "polynomial fit -- need more games spanning more of the game.")
        return None

    # Fit the polynomial ONLY on phase >= MIN_PHASE_FOR_FIT. Below that,
    # buckets aren't just noisier, they're systematically skewed (see the
    # constant's comment) -- weighting them down by sample count doesn't fix
    # this the way it would for ordinary noise, since e.g. phase 2 actually
    # has a LARGE sample count (79k) despite being the least trustworthy
    # bucket in the whole set. The only sound fix is excluding them outright.
    all_phases = sorted(per_phase)
    fit_phases = [p for p in all_phases if p >= MIN_PHASE_FOR_FIT]
    if len(fit_phases) < 4:
        print(f"\nOnly {len(fit_phases)} phase buckets >= {MIN_PHASE_FOR_FIT} -- "
              "not enough for a stable polynomial fit.")
        return None

    m = np.array([p / PHASE_MAX for p in fit_phases])
    a_vals = np.array([per_phase[p][0] for p in fit_phases])
    b_vals = np.array([per_phase[p][1] for p in fit_phases])
    w = np.array([math.sqrt(n_per_phase[p]) for p in fit_phases])
    as_coef = np.polyfit(m, a_vals, 3, w=w)
    bs_coef = np.polyfit(m, b_vals, 3, w=w)

    # Report fit quality across EVERY phase bucket (including the excluded
    # low ones, so the exclusion decision stays visible/inspectable) -- how
    # far the polynomial is from each bucket's own directly-fit (a, b).
    print(f"\nPolynomial fit uses phase >= {MIN_PHASE_FOR_FIT} only "
          f"({len(fit_phases)} buckets). Residuals shown for all buckets:")
    for p in all_phases:
        mm = p / PHASE_MAX
        av, bv = per_phase[p]
        a_poly = np.polyval(as_coef, mm)
        b_poly = np.polyval(bs_coef, mm)
        excl = "  (excluded from fit)" if p < MIN_PHASE_FOR_FIT else ""
        print(f"  phase {p:>2}: a {av:+7.2f} -> {a_poly:+7.2f}  "
              f"(diff {a_poly - av:+6.2f})   "
              f"b {bv:7.2f} -> {b_poly:7.2f}  (diff {b_poly - bv:+6.2f}){excl}")

    return as_coef, bs_coef, per_phase, MIN_PHASE_FOR_FIT


CUCI_CONSTS = {                       # family -> (as-name, bs-name) in cuci.py
    "hce":  ("_WDL_AS", "_WDL_BS"),
    "nnue": ("_WDL_AS_NNUE", "_WDL_BS_NNUE"),
}


def sync_cuci(root=None, families=("hce", "nnue")):
    """Write the fitted coefficients from data/wdl_model*.json into cuci.py.

    cuci HARDCODES the model because the PyInstaller binary ships without
    data/. That is correct and it also meant a human had to copy four numbers
    after every refit -- which was missed three times running, each caught by
    eye a release later. The fit now does it.

    Driven by the JSON rather than by in-memory coefficients so that
    `--sync-only` can repair a drifted checkout without a 20-minute refit,
    and so the file on disk is always the single source of truth.

    Only families whose JSON exists are touched: an `--eval-family nnue` run
    must not blank the hce constants."""
    import json
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cuci = os.path.join(root, "cuci.py")
    if not os.path.exists(cuci):
        print("sync: no cuci.py here, skipping")
        return []
    with open(cuci, encoding="utf-8") as f:
        src = f.read()
    changed = []
    for fam in families:
        suffix = "" if fam == "hce" else f"_{fam}"
        path = os.path.join(root, "data", f"wdl_model{suffix}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        a_name, b_name = CUCI_CONSTS[fam]
        for name, vals in ((a_name, m["as"]), (b_name, m["bs"])):
            new = f"{name} = {vals!r}"
            pat = re.compile(rf"^{re.escape(name)} = \[.*?\]$", re.M)
            if not pat.search(src):
                print(f"sync: {name} not found in cuci.py -- skipped")
                continue
            if pat.search(src).group(0) != new:
                src = pat.sub(lambda _m: new, src, count=1)
                changed.append(name)
    if changed:
        with open(cuci, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"sync: updated cuci.py -> {', '.join(changed)}")
    else:
        print("sync: cuci.py already matches the JSON models")
    return changed


def print_engine_snippet(as_coef, bs_coef, clamp_min, n_samples):
    def fmt(coefs):
        return ", ".join(f"{c:+.6f}" for c in coefs)

    print("\n" + "=" * 72)
    print("Paste into engine.py (near PHASE_WEIGHTS/PHASE_MAX, engine.py:816):")
    print("=" * 72)
    print(f'''
# WDL model -- fitted by fit_wdl_model.py from {n_samples:,} (cp, phase)
# samples drawn from engine.py's own self-play/A-B match history. Converts
# engine.py's own cp score + phase into a win/draw/loss estimate, mirroring
# Stockfish's win_rate_model(). Refit periodically as more games accumulate;
# see fit_wdl_model.py for how -- do not hand-edit these coefficients.
WDL_AS = [{fmt(as_coef)}]
WDL_BS = [{fmt(bs_coef)}]
# Deep-endgame phase buckets below this are sparse AND skewed toward
# already-decisive positions (games rarely linger there) -- clamp the input
# here rather than trust the polynomial's extrapolation into that region.
WDL_PHASE_CLAMP_MIN = {clamp_min}

def win_rate_model(cp, phase):
    """P(win) for a score of `cp` centipawns at game `phase` (0..PHASE_MAX)."""
    m = min(max(phase, WDL_PHASE_CLAMP_MIN), PHASE_MAX) / PHASE_MAX
    a = ((WDL_AS[0] * m + WDL_AS[1]) * m + WDL_AS[2]) * m + WDL_AS[3]
    b = ((WDL_BS[0] * m + WDL_BS[1]) * m + WDL_BS[2]) * m + WDL_BS[3]
    return 1.0 / (1.0 + math.exp((a - cp) / b))

def wdl(cp, phase):
    """(win, draw, loss) per-mille ints summing to 1000 -- Stockfish's UCI
    'wdl' convention. `cp` is from the side-to-move's own point of view."""
    w = win_rate_model(cp, phase)
    l = win_rate_model(-cp, phase)
    d = max(0.0, 1.0 - w - l)
    win, draw, loss = round(w * 1000), round(d * 1000), round(l * 1000)
    drift = 1000 - (win + draw + loss)     # rounding can miss 1000 by +/-1
    if drift:
        biggest = max((win, 0), (draw, 1), (loss, 2))[1]
        if biggest == 0: win += drift
        elif biggest == 1: draw += drift
        else: loss += drift
    return win, draw, loss
''')


# ====================================================================== #
#  Main
# ====================================================================== #
def main():
    global _MIN_C_ERA_SNAPSHOT, CENGINE_MIN_DATE, EVAL_FAMILY, GLOBAL_SINCE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-family", choices=("hce", "nnue", "sf"),
                    default=EVAL_FAMILY,
                    help="which eval scale to fit (default: %(default)s). The "
                         "two are never pooled; 'nnue' writes its own "
                         "wdl_model_nnue.json instead of overwriting the "
                         "hand-crafted-eval model the runtime loads")
    ap.add_argument("--nnue", dest="nnue", action="store_true",
                    help="shorthand for --eval-family nnue. This is the flag "
                         "worth remembering: refitting the NNUE model is a "
                         "thing you do, whereas '--eval-family hce' is just "
                         "the default spelled out")
    ap.add_argument("--min-era", type=int, default=None,
                    help="minimum engine<N> snapshot to accept (default "
                         "%d = the whole C era; pass 53 to restrict to the "
                         "v53 eval scale once those logs exist)"
                         % _MIN_C_ERA_SNAPSHOT)
    ap.add_argument("--cengine-since", default=None, metavar="YYYY-MM-DD",
                    help="also require dev-build ('cengine') logs to be dated "
                         "on/after this, since the name alone cannot tell "
                         "one eval era from another (default: no date gate)")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="refuse EVERY log dated before this, snapshot logs "
                         "included (undated names are refused too: an "
                         "unknown era is not a safe one). Use after an "
                         "instrument break -- e.g. 2026-08-08 excludes "
                         "everything measured before the phantom-repetition "
                         "fix, whose results are draw-biased")
    ap.add_argument("--sync-only", action="store_true",
                    help="do not fit: just write the coefficients already in "
                         "data/wdl_model*.json into cuci.py, then exit")
    ap.add_argument("--both", action="store_true",
                    help="fit BOTH eval families in one run (hce then nnue), "
                         "each into its own corpus and JSON. Since v58 armed "
                         "the net, the NNUE pair is the one the runtime "
                         "actually reads; the hce pair is the fallback a "
                         "CPU without SIMD would play on")
    ap.add_argument("--extract-only", action="store_true",
                    help="only extract + write the training CSV, skip fitting")
    ap.add_argument("--fit-only", action="store_true",
                    help=f"skip extraction, reuse an existing {DATA_CSV}")
    ap.add_argument("--data-file", default=None,
                    help=f"CSV path for extracted samples (default: {DATA_CSV}, "
                         "or wdl_training_data_<family>.csv)")
    args = ap.parse_args()
    if args.min_era is not None:
        _MIN_C_ERA_SNAPSHOT = args.min_era
    if args.since:
        GLOBAL_SINCE = args.since
    if args.cengine_since is not None:
        CENGINE_MIN_DATE = args.cengine_since or None
    if args.nnue and args.eval_family != "nnue":
        # Both spellings given and disagreeing: --nnue is the explicit ask,
        # --eval-family may just be its default sitting there. Refuse rather
        # than pick, because guessing wrong here silently fits the WRONG
        # corpus and writes it to the WRONG filename.
        if "--eval-family" in sys.argv:
            ap.error("--nnue conflicts with --eval-family "
                     f"{args.eval_family}; pass only one")
        args.eval_family = "nnue"
    if args.sync_only:
        sync_cuci()
        return
    EVAL_FAMILY = args.eval_family
    # Per-family filenames: an NNUE fit must not land on top of the model
    # match.py/uci.py load for the hand-crafted eval.
    suffix = "" if EVAL_FAMILY == "hce" else f"_{EVAL_FAMILY}"
    if args.data_file is None:
        args.data_file = DATA_CSV.replace(".csv", f"{suffix}.csv")
    model_name = f"wdl_model{suffix}.json"
    print(f"Corpus gate: eval family '{EVAL_FAMILY}', engine<N> >= "
          f"{_MIN_C_ERA_SNAPSHOT}"
          + (f", cengine logs on/after {CENGINE_MIN_DATE}"
             if CENGINE_MIN_DATE else ", no cengine date gate")
          + (f", NNUE logs on/after {NNUE_MIN_DATE}"
             if EVAL_FAMILY == "nnue" else ""))

    if args.fit_only:
        if not os.path.exists(args.data_file):
            print(f"No {args.data_file} found -- run without --fit-only first "
                  "to extract training data.")
            sys.exit(1)
        samples = read_csv(args.data_file)
        print(f"Loaded {len(samples):,} samples from {args.data_file}")
    else:
        samples = extract_all(LOG_DIRS)
        if not samples:
            print("No usable samples extracted -- nothing to fit.")
            sys.exit(1)
        write_csv(samples, args.data_file)
        print(f"Wrote {len(samples):,} samples to {args.data_file}")

    if args.extract_only:
        return

    result = fit_wdl_model(samples)
    if result is None:
        sys.exit(1)
    as_coef, bs_coef, _per_phase, clamp_min = result
    print_engine_snippet(as_coef, bs_coef, clamp_min, len(samples))

    # Machine-readable model for the runtime consumers (uci.py's UCI_ShowWDL
    # and match.py's adjudication) -- they load this file lazily and stay
    # dormant while it doesn't exist.
    # Every consumer picks its own family: cuci.py binds _WDL_AS/_WDL_AS_NNUE
    # from the eval the engine actually armed (--sync-only writes both pairs),
    # match.py's _wdl_win_threshold reads the per-family file, and
    # tuning/eval_bench.py reads the _nnue one directly. lib/wdl.py used to be
    # a fifth reader; it resolved the path relative to lib/ itself, so it had
    # returned None for every call since the 2026-07-24 reshuffle and was
    # deleted rather than fixed.
    import datetime
    import json
    model_path = os.path.join(os.path.dirname(os.path.dirname(
                                  os.path.abspath(__file__))), "data",
                              model_name)
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump({
            "fitted": datetime.date.today().isoformat(),
            "eval_family": EVAL_FAMILY,
            "n_samples": len(samples),
            "phase_max": PHASE_MAX,
            "phase_clamp_min": clamp_min,
            "as": [float(c) for c in as_coef],
            "bs": [float(c) for c in bs_coef],
        }, f, indent=1)
    print(f"\nWrote {model_path} (consumed by uci.py UCI_ShowWDL and "
          "match.py adjudication).")
    # The step that kept being forgotten. cuci hardcodes the model (the
    # PyInstaller binary ships without data/), so a refit that does not reach
    # cuci.py leaves the shipped engine on the old curve.
    if EVAL_FAMILY != "sf":
        # cuci never reports WDL on Stockfish's scale -- the sf model exists
        # only for match.py's per-side adjudication threshold. No constants
        # to sync, and trying would KeyError on purpose rather than invent.
        sync_cuci(families=(EVAL_FAMILY,))


if __name__ == "__main__":
    # Ctrl-C during extraction exits WITHOUT writing. Salvaging a partial
    # sample set here would overwrite the existing multi-million-row CSV
    # with a truncated one -- the destructive outcome, not the safe one.
    with interruptible.salvage():
        if "--both" in sys.argv:
            # Each family needs its OWN corpus (different sides are
            # extractable), so this is two full passes, not one fit reused.
            # Run them as separate main() calls rather than threading a family
            # loop through main's globals: EVAL_FAMILY and the filename suffix
            # are module state, and re-entering cleanly beats mutating it.
            base = [a for a in sys.argv[1:]
                    if a != "--both" and not a.startswith("--eval-family")]
            for _fam in ("hce", "nnue"):
                print("\n" + "=" * 72)
                print(f"  eval family: {_fam}")
                print("=" * 72)
                sys.argv = [sys.argv[0], "--eval-family", _fam] + base
                main()
        else:
            main()
