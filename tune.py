"""
tune.py
=======
Step 2 of Texel tuning. Loads a scored-positions CSV (from score_positions.py),
then optimises engine.py's evaluation constants using stochastic coordinate
descent (SCD): for every parameter in random order, try +delta and -delta, keep
whichever reduces the loss, and repeat for N rounds.

SCD is used instead of SPSA because perturbing all 800 parameters simultaneously
(SPSA) produces gradient estimates that are almost entirely noise -- the
cross-parameter interference swamps the true signal for any single value. SCD
changes one parameter at a time so the signal is clean.

    pypy3  tune.py positions.csv                          # 5 rounds, default delta
    pypy3  tune.py positions.csv --rounds 20 --delta 2
    python3 tune.py positions.csv --output-engine engine_tuned.py
    pypy3  tune.py positions.csv --pst                    # also tune PST tables

Output
------
Writes tuned constants to ``engine_tuned.py`` by default (a full copy of
engine.py with updated values so it can be used as a drop-in replacement).
Pass ``--output-engine engine.py`` to overwrite the original.

PyPy is strongly recommended -- ~5x faster than CPython here.

What gets tuned (30 parameters by default)
-------------------------------------------
MG_VALUES / EG_VALUES for PNBRQ  (10)
BISHOP_PAIR, ROOK_OPEN_FILE, ROOK_SEMIOPEN_FILE, TEMPO  (4)
DOUBLED_PAWN, ISOLATED_PAWN, BACKWARD_PAWN  (3)
PASSED_PAWN ranks 1-6  (6)
MOBILITY_WEIGHT for NBRQ  (4)
KING_RING_ATTACK, KING_SHIELD, KING_OPEN_FILE  (3)

PST tables (768 params) are excluded by default. Per-square signal is too
thin for clean coordinate descent -- adjacent squares drift apart producing
spiky incoherent tables. Use --pst to re-enable if desired; otherwise the
tables in engine_tuned.py are preserved unchanged by write_back.
"""

import argparse
import csv
import math
import random
import re
import shutil
import sys
import time

import chess

from engine import Engine


# ====================================================================== #
#  CONFIG
# ====================================================================== #
ENGINE_PATH          = "engine.py"
DEFAULT_OUTPUT       = "engine_tuned.py"
SIGMOID_K            = 400       # cp scale for sigmoid (win-probability)
DEFAULT_ROUNDS       = 5         # coordinate-descent rounds
DEFAULT_DELTA        = 1         # perturbation per param (cp); try 2-3 to explore faster
DEFAULT_BATCH_PARAM  = 50000     # positions sampled per individual-param eval (30 params → big batches)
DEFAULT_BATCH_ROUND  = 20000     # positions for round-level loss eval (accuracy)
# ====================================================================== #

PIECE_ORDER = [chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN]
MOB_PIECES  = [chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN]
PST_NAMES   = [
    "MG_PAWN_TABLE",   "EG_PAWN_TABLE",
    "MG_KNIGHT_TABLE", "EG_KNIGHT_TABLE",
    "MG_BISHOP_TABLE", "EG_BISHOP_TABLE",
    "MG_ROOK_TABLE",   "EG_ROOK_TABLE",
    "MG_QUEEN_TABLE",  "EG_QUEEN_TABLE",
    "MG_KING_TABLE",   "EG_KING_TABLE",
]
SCALAR_NAMES = [
    "ROOK_OPEN_FILE", "ROOK_SEMIOPEN_FILE", "TEMPO",
    "DOUBLED_PAWN", "ISOLATED_PAWN", "BACKWARD_PAWN",
    "BISHOP_PAIR_MG", "BISHOP_PAIR_EG",
    "KING_RING_ATTACK_MG", "KING_RING_ATTACK_EG",
    "KING_SHIELD_MG", "KING_SHIELD_EG",
    "KING_OPEN_FILE_MG", "KING_OPEN_FILE_EG",
]

# Set to True by --pst flag at runtime.
TUNE_PST = False

# Hard bounds (min, max) for each parameter group.
# Prevents tuner from drifting to chess-senseless values on large datasets.
MG_BOUNDS = {chess.PAWN:(60,130), chess.KNIGHT:(290,400), chess.BISHOP:(320,430),
             chess.ROOK:(430,570), chess.QUEEN:(900,1150)}
EG_BOUNDS = {chess.PAWN:(70,130), chess.KNIGHT:(245,340), chess.BISHOP:(255,350),
             chess.ROOK:(460,570), chess.QUEEN:(860,1020)}
SCALAR_BOUNDS = {
    "ROOK_OPEN_FILE":       (10,  45),
    "ROOK_SEMIOPEN_FILE":   ( 3,  25),
    "TEMPO":                ( 2,  25),
    "DOUBLED_PAWN":         ( 5,  35),
    "ISOLATED_PAWN":        ( 3,  28),
    "BACKWARD_PAWN":        ( 2,  22),
    "BISHOP_PAIR_MG":       (10,  55),
    "BISHOP_PAIR_EG":       (10,  55),
    "KING_RING_ATTACK_MG":  ( 2,  20),
    "KING_RING_ATTACK_EG":  ( 0,  12),
    "KING_SHIELD_MG":       ( 2,  22),
    "KING_SHIELD_EG":       ( 0,  10),
    "KING_OPEN_FILE_MG":    ( 8,  40),
    "KING_OPEN_FILE_EG":    ( 2,  22),
}
PASSED_BOUNDS  = (0, 60)    # per rank-to-rank increment for both MG and EG tables
MOBILITY_BOUNDS = (0, 8)    # per piece type


def _bounds_list():
    """Return the (min, max) bound for each parameter, in pack order."""
    b = []
    for pt in PIECE_ORDER: b.append(MG_BOUNDS[pt])
    for pt in PIECE_ORDER: b.append(EG_BOUNDS[pt])
    if TUNE_PST:
        for _ in PST_NAMES:
            for _ in range(64): b.append((-200, 200))
    for n in SCALAR_NAMES: b.append(SCALAR_BOUNDS[n])
    for _ in range(2):         # MG then EG passed pawn increments
        for _ in range(1, 7): b.append(PASSED_BOUNDS)
    for _ in MOB_PIECES:   b.append(MOBILITY_BOUNDS)
    return b


def _clamp(params, bounds):
    for i, (lo, hi) in enumerate(bounds):
        if params[i] < lo: params[i] = lo
        elif params[i] > hi: params[i] = hi


# --------------------------------------------------------------------------- #
# Parameter packing / unpacking
# --------------------------------------------------------------------------- #

def pack_params():
    E = Engine
    p = []
    for pt in PIECE_ORDER: p.append(float(E.MG_VALUES[pt]))
    for pt in PIECE_ORDER: p.append(float(E.EG_VALUES[pt]))
    if TUNE_PST:
        for n in PST_NAMES: p.extend(float(v) for v in getattr(E, n))
    for n in SCALAR_NAMES: p.append(float(getattr(E, n)))
    # PASSED_PAWN_MG and PASSED_PAWN_EG stored as rank-to-rank increments
    # to enforce monotonicity.  increment[r] = PP[r] - PP[r-1], all >= 0.
    for arr in (E.PASSED_PAWN_MG, E.PASSED_PAWN_EG):
        prev = 0
        for r in range(1, 7):
            p.append(float(max(0, arr[r] - prev)))
            prev = arr[r]
    for pt in MOB_PIECES:  p.append(float(E.MOBILITY_WEIGHT[pt]))
    return p


def apply_params(params):
    """Patch Engine CLASS attributes in place."""
    E = Engine
    i = 0

    mg = {pt: round(params[i + j]) for j, pt in enumerate(PIECE_ORDER)}
    mg[chess.KING] = 0
    E.MG_VALUES = mg
    i += len(PIECE_ORDER)

    eg = {pt: round(params[i + j]) for j, pt in enumerate(PIECE_ORDER)}
    eg[chess.KING] = 0
    E.EG_VALUES = eg
    i += len(PIECE_ORDER)

    if TUNE_PST:
        for n in PST_NAMES:
            tbl = getattr(E, n)
            for j in range(64):
                tbl[j] = round(params[i]); i += 1

    for n in SCALAR_NAMES:
        setattr(E, n, round(params[i])); i += 1

    # Reconstruct PASSED_PAWN_MG and PASSED_PAWN_EG from increments.
    for attr in ("PASSED_PAWN_MG", "PASSED_PAWN_EG"):
        pp = list(getattr(E, attr))
        val = 0
        for r in range(1, 7):
            val += max(0, round(params[i])); i += 1
            pp[r] = val
        setattr(E, attr, pp)

    mob = dict(E.MOBILITY_WEIGHT)
    for pt in MOB_PIECES:
        mob[pt] = round(params[i]); i += 1
    E.MOBILITY_WEIGHT = mob


# --------------------------------------------------------------------------- #
# Loss
# --------------------------------------------------------------------------- #

def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def optimize_k(engine, boards, targets, k_lo=50, k_hi=500, steps=50):
    """
    Find the K that minimises MSE(sigmoid(score/K), target) on a sample of
    positions.  Run once before tuning when using outcome-label datasets
    (0.0 / 0.5 / 1.0 targets) so the sigmoid scale matches the engine's
    actual score range instead of defaulting to 400.
    """
    best_k = k_lo
    best_loss = float("inf")
    step = (k_hi - k_lo) / steps
    k = k_lo
    while k <= k_hi:
        total = 0.0
        for board, t in zip(boards, targets):
            score = engine._evaluate_static(board)
            diff = sigmoid(score / k) - t
            total += diff * diff
        loss = total / len(boards)
        if loss < best_loss:
            best_loss = loss
            best_k = k
        k += step
    return round(best_k), best_loss


def compute_loss(engine, boards, sf_sigs, params):
    """MSE between sigmoid(engine_score/K) and sigmoid(sf_score/K)."""
    apply_params(params)
    engine._acc_valid = False
    total = 0.0
    for board, target in zip(boards, sf_sigs):
        score = engine._evaluate_static(board)
        diff = sigmoid(score / SIGMOID_K) - target
        total += diff * diff
    return total / len(boards)


# --------------------------------------------------------------------------- #
# Coordinate descent
# --------------------------------------------------------------------------- #

def coord_round(engine, all_boards, all_sigs, params, delta, batch_size, bounds):
    """
    One full pass over all parameters with stochastic coordinate descent.

    For each parameter (in a random order), evaluates loss at +delta and -delta
    on a small random batch. If either beats the current rolling baseline, moves
    in that direction and updates the baseline. Otherwise leaves the parameter
    unchanged.

    Returns (new_params, n_changed).
    """
    n = len(params)
    order = list(range(n))
    random.shuffle(order)

    # Fresh small batch for this round -- same batch across all params so
    # comparisons are fair (no batch-sampling noise between evals).
    idx = random.sample(range(len(all_boards)), min(batch_size, len(all_boards)))
    boards = [all_boards[k] for k in idx]
    sigs   = [all_sigs[k]   for k in idx]

    baseline = compute_loss(engine, boards, sigs, params)
    changed = 0

    lo_arr = [b[0] for b in bounds]
    hi_arr = [b[1] for b in bounds]

    for i in order:
        orig = params[i]
        lo, hi = lo_arr[i], hi_arr[i]

        cand_p = max(lo, min(hi, orig + delta))
        cand_m = max(lo, min(hi, orig - delta))

        params[i] = cand_p
        loss_p = compute_loss(engine, boards, sigs, params) if cand_p != orig else float('inf')

        params[i] = cand_m
        loss_m = compute_loss(engine, boards, sigs, params) if cand_m != orig else float('inf')

        best_loss = loss_p if loss_p < loss_m else loss_m
        if best_loss < baseline - 1e-10:
            params[i] = cand_p if loss_p < loss_m else cand_m
            baseline = best_loss
            changed += 1
        else:
            params[i] = orig   # restore

    return params, changed


# --------------------------------------------------------------------------- #
# Write-back: produce engine_tuned.py (or any target path)
# --------------------------------------------------------------------------- #

def _replace_scalar(lines, name, value):
    pat = re.compile(rf'^(\s+{re.escape(name)}\s*=\s*)-?\d+')
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            lines[i] = m.group(1) + str(round(value))
            return
    print(f"  [warn] {name} not found", file=sys.stderr)


def _replace_pst(lines, name, values):
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\[', line):
            start = i; break
    if start is None:
        print(f"  [warn] {name} not found", file=sys.stderr); return
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip().endswith(']'):
            end = i; break
    if end is None:
        return
    new = [f"    {name} = ["]
    for rank in range(8):
        row = [round(values[rank * 8 + f]) for f in range(8)]
        new.append("        " + "".join(f"{v:5d}," for v in row))
    new.append("    ]")
    lines[start:end + 1] = new


def _replace_dict_block(lines, name, new_lines):
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\{{', line):
            start = i; break
    if start is None:
        print(f"  [warn] {name} not found", file=sys.stderr); return
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip().endswith('}'):
            end = i; break
    if end is None:
        return
    lines[start:end + 1] = new_lines


def write_back(params, src=ENGINE_PATH, dst=DEFAULT_OUTPUT):
    """Copy src to dst and patch the eval constants with tuned values."""
    apply_params(params)
    E = Engine

    if src != dst:
        shutil.copy(src, dst)
    with open(dst) as f:
        lines = f.read().splitlines()

    # Material dicts
    mg = E.MG_VALUES;  eg = E.EG_VALUES
    _replace_dict_block(lines, "MG_VALUES", [
        "    MG_VALUES = {",
        f"        chess.PAWN: {mg[chess.PAWN]}, chess.KNIGHT: {mg[chess.KNIGHT]}, "
        f"chess.BISHOP: {mg[chess.BISHOP]},",
        f"        chess.ROOK: {mg[chess.ROOK]}, chess.QUEEN: {mg[chess.QUEEN]}, chess.KING: 0,",
        "    }",
    ])
    _replace_dict_block(lines, "EG_VALUES", [
        "    EG_VALUES = {",
        f"        chess.PAWN: {eg[chess.PAWN]}, chess.KNIGHT: {eg[chess.KNIGHT]}, "
        f"chess.BISHOP: {eg[chess.BISHOP]},",
        f"        chess.ROOK: {eg[chess.ROOK]}, chess.QUEEN: {eg[chess.QUEEN]}, chess.KING: 0,",
        "    }",
    ])

    # PST tables (only when --pst; otherwise preserve whatever is already in dst)
    if TUNE_PST:
        for n in PST_NAMES:
            _replace_pst(lines, n, getattr(E, n))

    # Scalar constants
    for n in SCALAR_NAMES:
        _replace_scalar(lines, n, getattr(E, n))

    # PASSED_PAWN_MG / PASSED_PAWN_EG (one-liner each)
    for attr in ("PASSED_PAWN_MG", "PASSED_PAWN_EG"):
        pat = re.compile(r'^(\s+' + attr + r'\s*=\s*)\[.*\]')
        arr = getattr(E, attr)
        for i, line in enumerate(lines):
            m = pat.match(line)
            if m:
                lines[i] = m.group(1) + str([round(v) for v in arr])
                break

    # MOBILITY_WEIGHT
    mob = E.MOBILITY_WEIGHT
    _replace_dict_block(lines, "MOBILITY_WEIGHT", [
        "    MOBILITY_WEIGHT = {",
        f"        chess.KNIGHT: {mob[chess.KNIGHT]}, chess.BISHOP: {mob[chess.BISHOP]}, "
        f"chess.ROOK: {mob[chess.ROOK]}, chess.QUEEN: {mob[chess.QUEEN]},",
        "    }",
    ])

    with open(dst, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Written to {dst}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    global SIGMOID_K
    ap = argparse.ArgumentParser(
        description="Texel-tune engine.py eval constants via coordinate descent.")
    ap.add_argument("positions_csv", help="CSV from score_positions.py, or a .epd file with c9 outcome labels")
    ap.add_argument("--rounds",    type=int,   default=DEFAULT_ROUNDS,
                    help=f"coordinate-descent rounds (default: {DEFAULT_ROUNDS})")
    ap.add_argument("--delta",     type=float, default=DEFAULT_DELTA,
                    help=f"perturbation per param in cp (default: {DEFAULT_DELTA})")
    ap.add_argument("--batch-param", type=int, default=DEFAULT_BATCH_PARAM,
                    help=f"positions per per-param eval (default: {DEFAULT_BATCH_PARAM})")
    ap.add_argument("--batch-round", type=int, default=DEFAULT_BATCH_ROUND,
                    help=f"positions for round-level loss (default: {DEFAULT_BATCH_ROUND})")
    ap.add_argument("--output-engine", default=DEFAULT_OUTPUT,
                    help=f"where to write tuned engine (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--engine", default=ENGINE_PATH,
                    help=f"source engine.py (default: {ENGINE_PATH})")
    ap.add_argument("--init-from", default=None, metavar="ENGINE_FILE",
                    help="load starting parameter values from this engine file instead of engine.py "
                         "(lets you continue a previous tuning run from where it left off)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--pst", action="store_true",
                    help="also tune all 768 PST values (noisy; off by default)")
    args = ap.parse_args()

    global TUNE_PST
    TUNE_PST = args.pst

    if args.seed is not None:
        random.seed(args.seed)

    # Load positions -- supports two formats:
    #   .epd  zurichess quiet-labeled: "<fen4> c9 \"<result>\";"
    #         targets are exact win-probabilities (1.0 / 0.5 / 0.0)
    #   .csv  our own format: fen,score_cp
    #         targets are sigmoid(score_cp / K)
    print(f"Loading {args.positions_csv} ...", flush=True)
    all_boards, all_sigs = [], []
    _RESULT_MAP = {"1-0": 1.0, "1/2-1/2": 0.5, "0-1": 0.0}
    ext = args.positions_csv.lower().rsplit(".", 1)[-1]
    is_epd  = ext == "epd"
    is_book = ext == "book"   # lichess-big3 format: "<fen6> [result]"

    if is_book:
        import re as _re
        _book_pat = _re.compile(r'\[([0-9.]+)\]')
        with open(args.positions_csv) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = _book_pat.search(line)
                    if not m:
                        continue
                    target = float(m.group(1))
                    if target not in (0.0, 0.5, 1.0):
                        continue
                    fen = line[:m.start()].strip()
                    all_boards.append(chess.Board(fen))
                    all_sigs.append(target)
                except Exception:
                    continue
    elif is_epd:
        import re as _re
        _c9_pat = _re.compile(r'c9\s+"([^"]+)"')
        with open(args.positions_csv) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parts  = line.split()
                    fen4   = " ".join(parts[:4])
                    m      = _c9_pat.search(line)
                    if not m:
                        continue
                    target = _RESULT_MAP.get(m.group(1))
                    if target is None:
                        continue
                    all_boards.append(chess.Board(fen4))
                    all_sigs.append(target)
                except Exception:
                    continue
    else:  # CSV
        with open(args.positions_csv, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    board = chess.Board(row["fen"])
                    cp    = float(row["score_cp"])
                except Exception:
                    continue
                all_boards.append(board)
                all_sigs.append(sigmoid(cp / SIGMOID_K))

    if not all_boards:
        sys.exit("No positions loaded.")
    print(f"  {len(all_boards):,} positions loaded.", flush=True)

    engine = Engine()
    engine.use_incremental_eval = False
    engine._acc_valid = False

    # For outcome-label datasets (EPD/book) find the optimal K before tuning.
    # CSV datasets skip this -- their targets are already sigmoid(cp/K) so any
    # fixed K just scales the loss uniformly and 400 is a reasonable default.
    if ext in ("epd", "book"):
        print("  Optimising K on a 5 000-position sample ...", flush=True)
        k_idx    = random.sample(range(len(all_boards)), min(5000, len(all_boards)))
        k_boards = [all_boards[i] for i in k_idx]
        k_sigs   = [all_sigs[i]   for i in k_idx]
        SIGMOID_K, k_loss = optimize_k(engine, k_boards, k_sigs)
        print(f"  Optimal K = {SIGMOID_K}  (loss {k_loss:.6f})", flush=True)

    params = pack_params()
    bounds = _bounds_list()
    _clamp(params, bounds)

    # --init-from: load starting scalars/material from a previously tuned engine
    # so we continue improving rather than restarting from engine.py's defaults.
    if args.init_from:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_init_engine", args.init_from)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _IE = _mod.Engine
        # Overwrite the params vector with values from the init engine,
        # then re-clamp so they stay within bounds.
        _i = 0
        for pt in PIECE_ORDER:
            params[_i] = float(_IE.MG_VALUES[pt]); _i += 1
        for pt in PIECE_ORDER:
            params[_i] = float(_IE.EG_VALUES[pt]); _i += 1
        if TUNE_PST:
            for n in PST_NAMES:
                for v in getattr(_IE, n):
                    params[_i] = float(v); _i += 1
        for n in SCALAR_NAMES:
            params[_i] = float(getattr(_IE, n)); _i += 1
        # PASSED_PAWN: convert back to increments for MG and EG
        for _attr in ("PASSED_PAWN_MG", "PASSED_PAWN_EG"):
            _arr = getattr(_IE, _attr, None) or getattr(_IE, "PASSED_PAWN", [0]*8)
            _prev = 0
            for r in range(1, 7):
                params[_i] = float(max(0, _arr[r] - _prev))
                _prev = _arr[r]; _i += 1
        for pt in MOB_PIECES:
            params[_i] = float(_IE.MOBILITY_WEIGHT[pt]); _i += 1
        _clamp(params, bounds)
        print(f"  Starting params loaded from {args.init_from}", flush=True)

    n_params = len(params)
    print(f"  {n_params} tunable parameters.")

    # Round-level eval batch (fixed sample for consistent comparisons across rounds)
    round_idx    = random.sample(range(len(all_boards)),
                                 min(args.batch_round, len(all_boards)))
    round_boards = [all_boards[k] for k in round_idx]
    round_sigs   = [all_sigs[k]   for k in round_idx]

    initial_loss = compute_loss(engine, round_boards, round_sigs, params)
    best_loss    = initial_loss
    best_params  = list(params)

    pst_note = f" + 768 PST" if TUNE_PST else " (PSTs frozen)"
    print(f"\nStarting {args.rounds} rounds of coordinate descent")
    print(f"  {n_params} params{pst_note}  |  delta={args.delta} cp  "
          f"|  batch-param={args.batch_param}  |  batch-round={args.batch_round}")
    print(f"  Initial loss: {initial_loss:.6f}\n")

    t0 = time.time()

    for rnd in range(1, args.rounds + 1):
        t_rnd = time.time()
        params, n_changed = coord_round(
            engine, all_boards, all_sigs, params,
            args.delta, args.batch_param, bounds)

        round_loss = compute_loss(engine, round_boards, round_sigs, params)
        dt = time.time() - t_rnd
        improved = round_loss < best_loss
        marker   = " *" if improved else ""

        print(f"  Round {rnd:2d}/{args.rounds}  loss={round_loss:.6f}  "
              f"({n_changed:3d} changed)  {dt:.0f}s{marker}", flush=True)

        if improved:
            best_loss   = round_loss
            best_params = list(params)
            # Save after every improvement
            write_back(best_params, src=args.engine, dst=args.output_engine)

        if n_changed == 0:
            print("  Converged — no parameters improved this round.")
            # Try larger delta if we're stuck
            if args.delta < 4:
                args.delta += 1
                print(f"  Increasing delta to {args.delta} to escape plateau.")
            else:
                break

    total_dt = time.time() - t0
    print(f"\nDone in {total_dt:.1f}s")
    print(f"  Loss: {initial_loss:.6f}  ->  {best_loss:.6f}  "
          f"(delta {best_loss - initial_loss:+.6f})")

    if best_params is not params:
        # Final save of best (in case last round wasn't the best)
        write_back(best_params, src=args.engine, dst=args.output_engine)

    print(f"\nTuned engine written to {args.output_engine}")
    print("Run a quick A/B match before replacing engine.py.")


if __name__ == "__main__":
    main()
