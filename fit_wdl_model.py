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
log file is USABLE when BOTH sides' engine bases are in NEAR_EQUAL_BASES
(near-equal engine-family pairings per the documented A/B results -- both
sides' samples are then extracted, since each side's score -> outcome
mapping is unbiased against a near-equal opponent), or when the file is in
NEAR_EQUAL_STOCKFISH_LOGS (a Stockfish match at matched strength; only the
engine-family side extracts). Mismatched-strength opponents, odds games and
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
ready-to-paste win_rate_model()/wdl() snippet for engine.py.

Usage:
    python3 fit_wdl_model.py                  # extract + fit
    python3 fit_wdl_model.py --extract-only    # just write the training CSV
    python3 fit_wdl_model.py --fit-only        # reuse a previously written CSV

Needs numpy + scipy (both already installed in this environment, though not
otherwise used by the project -- only this offline analysis script imports
them; nothing added to engine.py/match.py's runtime dependencies).
"""

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

# ====================================================================== #
#  CONFIG
# ====================================================================== #
LOG_DIRS = ["New logs", "logs"]
DATA_CSV = "wdl_training_data.csv"

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


# Engine-family bases whose RECORDED matches were near-equal pairings (A/B
# matches pair same-era builds; every pairing below measured within ~20 Elo
# per the documented A/B results). A log is usable iff BOTH sides are in
# this set -- and then BOTH sides' samples are extracted: against a
# near-equal opponent, each side's cp -> outcome mapping is equally valid.
# Do NOT add snapshots whose recorded matches had large gaps (engine15/19/
# 20 and older: 30-100+ Elo -- biased outcomes); if a future match pairs a
# listed base against something much stronger, delist it first.
NEAR_EQUAL_BASES = {
    # v31 / C-core era (2026-07-08 ->): cengine vs its frozen Old Engine/31
    # snapshot are IDENTICAL engines -- near-equal by construction. The
    # v24-v30 Python-era bases are deliberately DELISTED for the v31 refit:
    # (a) a depth-~8 engine's cp -> outcome conversion is exactly what the
    # refit must stop modeling (the C core converts +300 far more reliably),
    # and (b) delisting "engine" also auto-excludes the lopsided
    # cengine-vs-engine 29-1-0 gate log (a ~700-Elo-gap pairing that would
    # otherwise qualify once "cengine" is listed).
    "cengine", "engine31",
    # Python era (kept for reference; re-add ONLY for a deliberate
    # mixed-era fit): "engine", "engine24" ... "engine30".
}

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
}


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
    # Both sides near-equal engine family -> usable, BOTH sides extracted.
    return wb in NEAR_EQUAL_BASES and bb in NEAR_EQUAL_BASES


def iter_game_blocks(text):
    """Yield the text of each '=== Game N ===' block (marker line excluded)."""
    matches = list(GAME_SPLIT_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield text[start:end]


def process_block(block, samples, stats):
    """Extract (cp, phase, result) samples from one game block -- for EVERY
    side whose base is in NEAR_EQUAL_BASES (both sides of a near-equal
    engine-family match are equally valid training data; a near-equal
    Stockfish match contributes its engine-family side only, since
    "stockfish_engine" is never in the set)."""
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
    if white_base in NEAR_EQUAL_BASES:
        side_score[white_base] = w_score
    if black_base in NEAR_EQUAL_BASES:
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
    for d in log_dirs:
        all_files.extend(sorted(glob.glob(os.path.join(d, "*.txt"))))
    print(f"Scanning {len(all_files)} log files across {log_dirs} ...")

    files_used = 0
    for path in all_files:
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
            process_block(block, samples, stats)
        if files_used % 10 == 0:
            print(f"  ...{files_used} usable files processed, "
                  f"{len(samples):,} samples so far")

    print(f"\nDone. {len(all_files)} files scanned, {files_used} usable, "
          f"{stats['games_used']:,} games parsed, "
          f"{len(samples):,} (cp, phase, result) samples extracted.")
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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extract-only", action="store_true",
                    help="only extract + write the training CSV, skip fitting")
    ap.add_argument("--fit-only", action="store_true",
                    help=f"skip extraction, reuse an existing {DATA_CSV}")
    ap.add_argument("--data-file", default=DATA_CSV,
                    help=f"CSV path for extracted samples (default: {DATA_CSV})")
    args = ap.parse_args()

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
    import datetime
    import json
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "wdl_model.json")
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump({
            "fitted": datetime.date.today().isoformat(),
            "n_samples": len(samples),
            "phase_max": PHASE_MAX,
            "phase_clamp_min": clamp_min,
            "as": [float(c) for c in as_coef],
            "bs": [float(c) for c in bs_coef],
        }, f, indent=1)
    print(f"\nWrote {model_path} (consumed by uci.py UCI_ShowWDL and "
          "match.py adjudication).")


if __name__ == "__main__":
    main()
