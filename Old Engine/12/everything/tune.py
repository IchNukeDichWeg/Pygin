"""
tune.py
=======
Step 2 of Texel tuning. Loads a scored-positions CSV (from score_positions.py),
then optimises engine.py's evaluation constants using SPSA (Simultaneous
Perturbation Stochastic Approximation) to minimise mean-squared error between
the engine's static evaluation and Stockfish's scores.

    python3 tune.py positions.csv
    python3 tune.py positions.csv --iterations 200 --batch 5000 --lr 0.5
    python3 tune.py positions.csv --write-back          # patches engine.py

PyPy is ~3-5x faster here:  pypy3 tune.py positions.csv

What gets tuned (~800 parameters)
----------------------------------
MG_VALUES / EG_VALUES for PNBRQ  (10 values)
All 12 PST tables MG_/EG_ × PNBRQK  (12 × 64 = 768 values)
BISHOP_PAIR, ROOK_OPEN_FILE, ROOK_SEMIOPEN_FILE, TEMPO  (4)
DOUBLED_PAWN, ISOLATED_PAWN, BACKWARD_PAWN  (3)
PASSED_PAWN ranks 1-6  (6)
MOBILITY_WEIGHT for NBRQ  (4)
KING_RING_ATTACK, KING_SHIELD, KING_OPEN_FILE  (3)

Algorithm
----------
Loss = mean( (σ(engine/K) − σ(sf/K))² )  where σ(x) = 1/(1+exp(-x)), K=400.
SPSA perturbs every parameter simultaneously by ±c_k, estimates the gradient
from the loss difference, and updates.  No analytical gradient needed.
"""

import argparse
import csv
import math
import random
import re
import sys
import time

import chess

from engine import Engine


# ====================================================================== #
#  CONFIG -- tuneable here or via CLI flags
# ====================================================================== #
ENGINE_PATH    = "engine.py"
SIGMOID_K      = 400        # cp scale for sigmoid (win-probability)
SPSA_ALPHA     = 0.602      # step-size decay exponent (standard)
SPSA_GAMMA     = 0.101      # perturbation-size decay exponent (standard)
SPSA_C_INIT    = 1.5        # initial perturbation (cp)
DEFAULT_LR     = 0.5        # learning-rate multiplier
DEFAULT_ITERS  = 150        # SPSA steps
DEFAULT_BATCH  = 5000       # positions evaluated per step (random sample)
REPORT_EVERY   = 10         # print progress every N steps
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
    "BISHOP_PAIR", "ROOK_OPEN_FILE", "ROOK_SEMIOPEN_FILE", "TEMPO",
    "DOUBLED_PAWN", "ISOLATED_PAWN", "BACKWARD_PAWN",
    "KING_RING_ATTACK", "KING_SHIELD", "KING_OPEN_FILE",
]


# --------------------------------------------------------------------------- #
# Parameter packing / unpacking
# --------------------------------------------------------------------------- #

def pack_params():
    """Extract all tunable constants from the Engine CLASS into a flat list."""
    E = Engine
    p = []
    for pt in PIECE_ORDER: p.append(float(E.MG_VALUES[pt]))
    for pt in PIECE_ORDER: p.append(float(E.EG_VALUES[pt]))
    for n in PST_NAMES:    p.extend(float(v) for v in getattr(E, n))
    for n in SCALAR_NAMES: p.append(float(getattr(E, n)))
    for r in range(1, 7):  p.append(float(E.PASSED_PAWN[r]))
    for pt in MOB_PIECES:  p.append(float(E.MOBILITY_WEIGHT[pt]))
    return p


def apply_params(params):
    """
    Patch Engine CLASS attributes in place so that any live Engine instance
    immediately uses the new values.

    PST tables are mutated in-place (slice-assign) so that existing
    engine.mg_tables / engine.eg_tables references still point to the right
    lists. Everything else replaces the class attribute.
    """
    E = Engine
    i = 0

    # Material values (KING always 0)
    mg = {pt: round(params[i + j]) for j, pt in enumerate(PIECE_ORDER)}
    mg[chess.KING] = 0
    E.MG_VALUES = mg
    i += len(PIECE_ORDER)

    eg = {pt: round(params[i + j]) for j, pt in enumerate(PIECE_ORDER)}
    eg[chess.KING] = 0
    E.EG_VALUES = eg
    i += len(PIECE_ORDER)

    # PST tables -- must mutate in place so engine.mg_tables refs stay valid.
    for n in PST_NAMES:
        tbl = getattr(E, n)      # the class-level list object
        for j in range(64):
            tbl[j] = round(params[i]); i += 1

    # Scalar constants
    for n in SCALAR_NAMES:
        setattr(E, n, round(params[i])); i += 1

    # PASSED_PAWN (ranks 0 and 7 are fixed at 0)
    pp = list(E.PASSED_PAWN)
    for r in range(1, 7):
        pp[r] = round(params[i]); i += 1
    E.PASSED_PAWN = pp

    # Mobility weights
    mob = dict(E.MOBILITY_WEIGHT)
    for pt in MOB_PIECES:
        mob[pt] = round(params[i]); i += 1
    E.MOBILITY_WEIGHT = mob

    assert i == len(params), f"param mismatch: applied {i}, have {len(params)}"


# --------------------------------------------------------------------------- #
# Loss
# --------------------------------------------------------------------------- #

def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def compute_loss(engine, boards, sf_sigs, params):
    """MSE between engine sigmoid and Stockfish sigmoid over the batch."""
    apply_params(params)
    engine._acc_valid = False   # force from-scratch eval (no stale accumulator)
    total = 0.0
    for board, target in zip(boards, sf_sigs):
        score = engine._evaluate_static(board)
        diff = sigmoid(score / SIGMOID_K) - target
        total += diff * diff
    return total / len(boards)


# --------------------------------------------------------------------------- #
# SPSA
# --------------------------------------------------------------------------- #

def spsa_step(engine, all_boards, all_sigs, params, step, cfg):
    """One SPSA update. Returns (new_params, avg_loss)."""
    n = len(params)

    # Sample a random batch
    indices = random.sample(range(len(all_boards)), min(cfg["batch"], len(all_boards)))
    boards = [all_boards[k] for k in indices]
    sigs   = [all_sigs[k]   for k in indices]

    # SPSA scheduling
    ck = cfg["c_init"] / (step ** cfg["gamma"])
    ak = cfg["lr"] / ((step + cfg["A"]) ** cfg["alpha"])

    # Bernoulli perturbation vector
    delta = [random.choice((-1.0, 1.0)) for _ in range(n)]

    params_p = [p + ck * d for p, d in zip(params, delta)]
    params_m = [p - ck * d for p, d in zip(params, delta)]

    loss_p = compute_loss(engine, boards, sigs, params_p)
    loss_m = compute_loss(engine, boards, sigs, params_m)

    g_scale = (loss_p - loss_m) / (2.0 * ck)
    new_params = [p - ak * g_scale * d for p, d in zip(params, delta)]

    return new_params, (loss_p + loss_m) * 0.5


# --------------------------------------------------------------------------- #
# Write-back: patch engine.py in place
# --------------------------------------------------------------------------- #

def _replace_scalar(lines, name, value):
    """Replace `    NAME = <int>` in lines."""
    pat = re.compile(rf'^(\s+{re.escape(name)}\s*=\s*)-?\d+')
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            lines[i] = m.group(1) + str(round(value))
            return
    print(f"  [warn] could not find scalar {name} in engine.py", file=sys.stderr)


def _replace_pst(lines, name, values):
    """Replace a PST list constant (multi-line) in lines."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\[', line):
            start = i; break
    if start is None:
        print(f"  [warn] could not find {name} in engine.py", file=sys.stderr)
        return
    end = None
    for i in range(start, len(lines)):
        stripped = lines[i].rstrip()
        if i > start and stripped.endswith(']'):
            end = i; break
        if i > start and stripped == '    ]':
            end = i; break
    if end is None:
        print(f"  [warn] could not find closing ] for {name}", file=sys.stderr)
        return
    new = [f"    {name} = ["]
    for rank in range(8):
        row = [round(values[rank * 8 + f]) for f in range(8)]
        new.append("        " + "".join(f"{v:5d}," for v in row))
    new.append("    ]")
    lines[start:end + 1] = new


def _replace_material_dict(lines, name, mapping):
    """Replace MG_VALUES / EG_VALUES dict block in lines."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\{{', line):
            start = i; break
    if start is None:
        print(f"  [warn] could not find {name}", file=sys.stderr)
        return
    end = None
    for i in range(start, len(lines)):
        if i > start and lines[i].rstrip().endswith('}'):
            end = i; break
    if end is None:
        return
    p = mapping[chess.PAWN]
    n = mapping[chess.KNIGHT]
    b = mapping[chess.BISHOP]
    r = mapping[chess.ROOK]
    q = mapping[chess.QUEEN]
    new = [
        f"    {name} = {{",
        f"        chess.PAWN: {p}, chess.KNIGHT: {n}, chess.BISHOP: {b},",
        f"        chess.ROOK: {r}, chess.QUEEN: {q}, chess.KING: 0,",
        "    }",
    ]
    lines[start:end + 1] = new


def _replace_passed_pawn(lines, values):
    """Replace PASSED_PAWN one-liner."""
    pat = re.compile(r'^(\s+PASSED_PAWN\s*=\s*)\[.*\]')
    rounded = [round(v) for v in values]
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            lines[i] = m.group(1) + str(rounded)
            return
    print("  [warn] could not find PASSED_PAWN", file=sys.stderr)


def _replace_mobility(lines, mapping):
    """Replace MOBILITY_WEIGHT dict block."""
    start = None
    for i, line in enumerate(lines):
        if re.match(r'\s+MOBILITY_WEIGHT\s*=\s*\{', line):
            start = i; break
    if start is None:
        print("  [warn] could not find MOBILITY_WEIGHT", file=sys.stderr)
        return
    end = None
    for i in range(start, len(lines)):
        if i > start and lines[i].rstrip().endswith('}'):
            end = i; break
    if end is None:
        return
    kn = mapping[chess.KNIGHT]
    bi = mapping[chess.BISHOP]
    ro = mapping[chess.ROOK]
    qu = mapping[chess.QUEEN]
    new = [
        "    MOBILITY_WEIGHT = {",
        f"        chess.KNIGHT: {kn}, chess.BISHOP: {bi}, "
        f"chess.ROOK: {ro}, chess.QUEEN: {qu},",
        "    }",
    ]
    lines[start:end + 1] = new


def write_back(params, path=ENGINE_PATH):
    """Patch engine.py in place with the optimised parameter values."""
    apply_params(params)
    E = Engine

    with open(path) as f:
        lines = f.read().splitlines()

    _replace_material_dict(lines, "MG_VALUES", E.MG_VALUES)
    _replace_material_dict(lines, "EG_VALUES", E.EG_VALUES)
    for n in PST_NAMES:
        _replace_pst(lines, n, getattr(E, n))
    for n in SCALAR_NAMES:
        _replace_scalar(lines, n, getattr(E, n))
    _replace_passed_pawn(lines, E.PASSED_PAWN)
    _replace_mobility(lines, E.MOBILITY_WEIGHT)

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Patched {path}")


def print_params(params):
    """Print all tuned values in copy-paste Python format."""
    E = Engine
    apply_params(params)
    print("\n" + "=" * 60)
    print("TUNED VALUES (copy into engine.py)")
    print("=" * 60)

    names_p = ["PAWN", "KNIGHT", "BISHOP", "ROOK", "QUEEN"]
    mg_vals = [E.MG_VALUES[pt] for pt in PIECE_ORDER]
    eg_vals = [E.EG_VALUES[pt] for pt in PIECE_ORDER]
    print(f"\n    MG_VALUES = {{")
    print(f"        chess.PAWN: {mg_vals[0]}, chess.KNIGHT: {mg_vals[1]}, "
          f"chess.BISHOP: {mg_vals[2]},")
    print(f"        chess.ROOK: {mg_vals[3]}, chess.QUEEN: {mg_vals[4]}, chess.KING: 0,")
    print(f"    }}")
    print(f"    EG_VALUES = {{")
    print(f"        chess.PAWN: {eg_vals[0]}, chess.KNIGHT: {eg_vals[1]}, "
          f"chess.BISHOP: {eg_vals[2]},")
    print(f"        chess.ROOK: {eg_vals[3]}, chess.QUEEN: {eg_vals[4]}, chess.KING: 0,")
    print(f"    }}")

    for n in PST_NAMES:
        tbl = getattr(E, n)
        print(f"\n    {n} = [")
        for rank in range(8):
            row = [tbl[rank * 8 + f] for f in range(8)]
            print("        " + "".join(f"{v:5d}," for v in row))
        print("    ]")

    for n in SCALAR_NAMES:
        print(f"    {n} = {getattr(E, n)}")

    print(f"    PASSED_PAWN = {E.PASSED_PAWN}")

    mob = E.MOBILITY_WEIGHT
    print(f"    MOBILITY_WEIGHT = {{")
    print(f"        chess.KNIGHT: {mob[chess.KNIGHT]}, "
          f"chess.BISHOP: {mob[chess.BISHOP]}, "
          f"chess.ROOK: {mob[chess.ROOK]}, "
          f"chess.QUEEN: {mob[chess.QUEEN]},")
    print("    }")
    print("=" * 60)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Texel-tune engine.py eval constants against Stockfish scores.")
    ap.add_argument("positions_csv", help="CSV from score_positions.py")
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERS,
                    help=f"SPSA steps (default: {DEFAULT_ITERS})")
    ap.add_argument("--batch",      type=int, default=DEFAULT_BATCH,
                    help=f"positions per step (default: {DEFAULT_BATCH})")
    ap.add_argument("--lr",         type=float, default=DEFAULT_LR,
                    help=f"learning-rate multiplier (default: {DEFAULT_LR})")
    ap.add_argument("--write-back", action="store_true",
                    help="patch engine.py in place when done")
    ap.add_argument("--engine",     default=ENGINE_PATH,
                    help=f"path to engine.py (default: {ENGINE_PATH})")
    ap.add_argument("--seed",       type=int, default=None,
                    help="random seed for reproducibility")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    # Load positions
    print(f"Loading {args.positions_csv} ...", flush=True)
    all_boards = []
    all_sigs   = []
    with open(args.positions_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                board = chess.Board(row["fen"])
                cp    = float(row["score_cp"])
            except Exception:
                continue
            all_boards.append(board)
            all_sigs.append(sigmoid(cp / SIGMOID_K))

    if not all_boards:
        sys.exit("No positions loaded -- check CSV format (header: fen,score_cp).")

    print(f"  {len(all_boards):,} positions loaded.")

    # Engine instance -- incremental eval disabled so each call recomputes.
    engine = Engine()
    engine.use_incremental_eval = False
    engine._acc_valid = False

    # Initial params from current engine constants.
    params = pack_params()
    n_params = len(params)
    print(f"  {n_params} tunable parameters.")

    # SPSA config
    iters = args.iterations
    cfg = {
        "alpha":  SPSA_ALPHA,
        "gamma":  SPSA_GAMMA,
        "c_init": SPSA_C_INIT,
        "lr":     args.lr,
        "A":      max(1, iters // 10),   # stability constant
        "batch":  args.batch,
    }

    print(f"\nStarting SPSA: {iters} steps, batch={args.batch}, lr={args.lr}")
    print(f"  A={cfg['A']}, c_init={SPSA_C_INIT}, K={SIGMOID_K}")

    # Baseline loss (no perturbation)
    sample_idx = random.sample(range(len(all_boards)), min(args.batch, len(all_boards)))
    sample_boards = [all_boards[k] for k in sample_idx]
    sample_sigs   = [all_sigs[k]   for k in sample_idx]
    loss0 = compute_loss(engine, sample_boards, sample_sigs, params)
    print(f"  Initial loss: {loss0:.6f}\n")

    t0 = time.time()
    recent_losses = []

    for step in range(1, iters + 1):
        params, loss = spsa_step(engine, all_boards, all_sigs, params, step, cfg)
        recent_losses.append(loss)

        if step % REPORT_EVERY == 0:
            avg_loss = sum(recent_losses) / len(recent_losses)
            dt = time.time() - t0
            eta = dt / step * (iters - step)
            print(f"  step {step:4d}/{iters}  loss={avg_loss:.6f}  "
                  f"elapsed={dt:.0f}s  ETA={eta:.0f}s", flush=True)
            recent_losses = []

    # Final params
    apply_params(params)
    final_loss = compute_loss(engine, sample_boards, sample_sigs, params)
    dt = time.time() - t0
    print(f"\nDone in {dt:.1f}s.  Loss: {loss0:.6f} -> {final_loss:.6f}  "
          f"(delta {final_loss - loss0:+.6f})")

    print_params(params)

    if args.write_back:
        print(f"\nWriting back to {args.engine} ...")
        write_back(params, args.engine)
        print("Done. Run a quick A/B match before committing the new values.")
    else:
        print("\nRun with --write-back to patch engine.py, or copy the values above.")


if __name__ == "__main__":
    main()
