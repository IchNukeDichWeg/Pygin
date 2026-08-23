#!/usr/bin/env python3
"""
tuning/eval_bench.py
====================
A fixed set of positions scored once by Stockfish, so any later net can be
checked against it in minutes instead of a 6-hour A/B.

    # once: build the reference (slow, reusable forever)
    python3 tuning/eval_bench.py build --positions 10000 --depth 14 --workers 0

    # per net: score a random sample against it (fast)
    python3 tuning/eval_bench.py test engine_nnue_v10.py --sample 1000 --depth 10

WHAT THIS IS FOR, AND WHAT IT IS NOT
------------------------------------
It is a REGRESSION / SANITY instrument. It answers "is this net broken, or
broken in one particular phase" cheaply and repeatably.

It is NOT a strength predictor, and nothing here should be used to choose
what ships. Every cheap proxy tried on this project has failed to predict
Elo: held-out val was an inverted signal six times running (v8 and v6 held
the two best val numbers and both lost their A/Bs; v10 sat 4th of 5 on val
and won by ~40 Elo), holdout R^2 ranked v10 LAST of four nets immediately
before v10 accepted, and the float-vs-int quantization MAE flagged v10 at
62.64 cp against a ~17 norm for what turned out to be a healthy net. Assume
this one is no better until somebody CALIBRATES it against measured Elo
across several nets. Until then: a big move here means look; a small move
here means nothing either way.

Agreement with Stockfish is also not the same thing as playing well. The
two engines have different search and different eval scales -- a net can
track SF more closely and still lose games. That is why the headline number
is SPEARMAN correlation (scale-free, ordering only) rather than raw cp
error, with a fitted linear scale reported alongside so the cp figures are
comparable at all.

POSITIONS come from this repo's own PGNs -- real game positions from real
A/B campaigns, so the phase and material distribution matches what the
engine actually meets. Positions in check are skipped (their eval is
dominated by the tactic, not the net) and each FEN appears once.
"""

import argparse
import json
import os
import random
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import chess
import chess.pgn


def _fmt(sec):
    sec = int(max(0, sec))
    if sec >= 3600:
        return f"{sec//3600}h{(sec%3600)//60:02d}m"
    if sec >= 60:
        return f"{sec//60}m{sec%60:02d}s"
    return f"{sec}s"


class Progress:
    """Live bar + ETA on a tty; one plain line every `every` items otherwise.

    The isatty guard is not optional. A \r-drawn bar redirected into a file
    or through `tee` becomes an unreadable smear -- and silence is worse: a
    job with no output is indistinguishable from a hung one, which has cost
    this project real hours. So: bar when interactive, periodic lines when
    not, never nothing.
    """

    def __init__(self, total, label="", every=200, width=32):
        self.total, self.label, self.every, self.width = total, label, every, width
        self.t0, self.n, self.last = time.time(), 0, 0
        self.tty = sys.stdout.isatty()

    def step(self, k=1):
        self.n += k
        el = time.time() - self.t0
        rate = self.n / el if el > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        if self.tty:
            frac = self.n / self.total if self.total else 1.0
            fill = int(self.width * frac)
            bar = "#" * fill + "." * (self.width - fill)
            sys.stdout.write(
                f"\r  {self.label}[{bar}] {self.n:,}/{self.total:,} "
                f"{frac*100:5.1f}%  {rate:6.1f}/s  elapsed {_fmt(el)}  ETA {_fmt(eta)}   ")
            sys.stdout.flush()
        elif self.n - self.last >= self.every or self.n == self.total:
            self.last = self.n
            print(f"  {self.label}{self.n:,}/{self.total:,} "
                  f"({self.n/self.total*100:.1f}%)  {rate:.1f}/s  "
                  f"elapsed {_fmt(el)}  ETA {_fmt(eta)}", flush=True)

    def done(self):
        el = time.time() - self.t0
        if self.tty:
            sys.stdout.write("\r" + " " * 110 + "\r")
            sys.stdout.flush()
        print(f"  {self.label}{self.n:,} in {_fmt(el)} "
              f"({el/max(1,self.n):.3f}s each)", flush=True)


DEFAULT_REF = os.path.join(REPO, "data", "eval_bench.json")

# Both engines report a forced mate as a huge pseudo-cp (~1,000,000), so a
# handful of them swamp any cp-space statistic: 103 of 10,000 reference
# positions (1.03%) sat at |cp| >= 900,000 and pushed the residual MAE to
# 3,070 cp, 6,175 in endgames where mates live, while openings read a sane
# 66. The measured distribution has a clean gap -- real evals stop below
# 5,000, mates start above 900,000 -- so anything past this cut is a mate,
# not an evaluation. They are scored separately (did the two engines AGREE
# it is a forced mate, and for whom) rather than averaged into a cp error.
MATE_CUT = 10_000

# engine.py's phase weights, so buckets line up with the WDL model's
PHASE_W = {chess.PAWN: 0, chess.KNIGHT: 1, chess.BISHOP: 1,
           chess.ROOK: 2, chess.QUEEN: 4, chess.KING: 0}
PHASE_MAX = 24


def phase_of(board):
    p = sum(PHASE_W[pc.piece_type] for pc in board.piece_map().values())
    return min(p, PHASE_MAX)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def harvest(pgn_paths, want, skip_plies, seed):
    """Collect unique, non-check FENs from real games."""
    rng = random.Random(seed)
    seen, out = set(), []
    for path in pgn_paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            while len(out) < want * 3:            # oversample, trim later
                game = chess.pgn.read_game(fh)
                if game is None:
                    break
                board = game.board()
                for i, mv in enumerate(game.mainline_moves()):
                    board.push(mv)
                    if i < skip_plies or board.is_check():
                        continue
                    if board.is_game_over():
                        break
                    if rng.random() > 0.06:       # thin within a game
                        continue
                    fen = board.fen()
                    if fen in seen:
                        continue
                    seen.add(fen)
                    out.append(fen)
        if len(out) >= want * 3:
            break
    rng.shuffle(out)
    return out[:want]


def _score_chunk(args):
    fens, depth = args
    import stockfish_engine
    eng = stockfish_engine.Engine()
    rows = []
    for fen in fens:
        b = chess.Board(fen)
        eng.get_best_move(b, depth)
        rows.append({"fen": fen, "cp": int(eng.last_score),
                     "depth": int(eng.last_depth), "phase": phase_of(b)})
    return rows


def cmd_build(args):
    import multiprocessing as mp
    import glob
    pgns = sorted(glob.glob(os.path.join(REPO, "logs", "*.pgn"))) + \
           sorted(glob.glob(os.path.join(REPO, "New logs", "*.pgn")))
    if not pgns:
        print("no PGNs under logs/ or 'New logs/' to sample from")
        return 1
    print(f"harvesting {args.positions:,} positions from {len(pgns)} PGN files...")
    fens = harvest(pgns, args.positions, args.skip_plies, args.seed)
    print(f"  got {len(fens):,} unique non-check positions")
    if not fens:
        return 1

    n_workers = args.workers or max(1, mp.cpu_count() - 1)
    # Small chunks so progress ticks often. Pool.map() returns only when
    # EVERYTHING is done and so cannot report progress at all; imap_unordered
    # yields per chunk.
    per = max(1, min(50, len(fens) // (n_workers * 8) or 1))
    chunks = [(fens[i:i + per], args.depth) for i in range(0, len(fens), per)]
    print(f"scoring with Stockfish at depth {args.depth} on {n_workers} workers...")
    bar = Progress(len(fens), every=500)
    rows = []
    with mp.Pool(n_workers) as pool:
        for part in pool.imap_unordered(_score_chunk, chunks):
            rows.extend(part)
            bar.step(len(part))
    bar.done()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"built": time.strftime("%Y-%m-%d"),
                   "sf_depth": args.depth, "n": len(rows),
                   "seed": args.seed, "positions": rows}, fh)
    print(f"wrote {args.out}")
    return 0


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #
def spearman(a, b):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * db) if da and db else float("nan")


def cmd_test(args):
    with open(args.ref) as fh:
        ref = json.load(fh)
    rows = ref["positions"]
    rng = random.Random(args.seed)
    if args.sample and args.sample < len(rows):
        rows = rng.sample(rows, args.sample)
    print(f"reference: {ref['n']:,} positions @ SF depth {ref['sf_depth']} "
          f"(built {ref['built']}); sampling {len(rows):,}")

    import importlib.util
    spec = importlib.util.spec_from_file_location("cand", args.engine)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    eng = mod.Engine()
    eng.use_book = False

    ours, theirs, phases = [], [], []
    bar = Progress(len(rows), label=f"depth {args.depth}  ")
    for r in rows:
        b = chess.Board(r["fen"])
        eng.get_best_move(b, args.depth)
        ours.append(float(eng.last_score))
        theirs.append(float(r["cp"]))
        phases.append(r["phase"])
        bar.step()
    bar.done()
    print()

    # Mate scores are not evaluations -- split them out before any cp maths.
    keep = [i for i in range(len(ours))
            if abs(ours[i]) < MATE_CUT and abs(theirs[i]) < MATE_CUT]
    mate_idx = [i for i in range(len(ours)) if i not in set(keep)]
    n_mate = len(mate_idx)
    if n_mate:
        both = sum(1 for i in mate_idx
                   if abs(ours[i]) >= MATE_CUT and abs(theirs[i]) >= MATE_CUT)
        same = sum(1 for i in mate_idx
                   if abs(ours[i]) >= MATE_CUT and abs(theirs[i]) >= MATE_CUT
                   and (ours[i] > 0) == (theirs[i] > 0))
        print(f"  mate positions   {n_mate} of {len(ours)} excluded from cp stats "
              f"-- both saw a mate in {both}, same side in {same}")
    ours = [ours[i] for i in keep]
    theirs = [theirs[i] for i in keep]
    phases = [phases[i] for i in keep]
    if len(ours) < 20:
        print("  too few non-mate positions to score"); return 1

    # scale-free first: a different cp scale must not look like disagreement
    rho = spearman(ours, theirs)
    n = len(ours)
    mo, mt = sum(ours) / n, sum(theirs) / n
    cov = sum((o - mo) * (t - mt) for o, t in zip(ours, theirs))
    var = sum((t - mt) ** 2 for t in theirs)
    slope = cov / var if var else float("nan")
    resid = [o - slope * t for o, t in zip(ours, theirs)]
    mae = sum(abs(x - sum(resid) / n) for x in resid) / n
    agree = sum(1 for o, t in zip(ours, theirs)
                if (o > 25) == (t > 25) and (o < -25) == (t < -25)) / n

    print(f"  Spearman rho     {rho:+.4f}   (ordering agreement; scale-free)")
    print(f"  fitted scale     {slope:.3f}   (ours = {slope:.3f} x SF)")
    print(f"  residual MAE     {mae:6.1f} cp  (after removing that scale)")
    print(f"  sign agreement   {agree*100:5.1f}%  (+/-25cp deadband)")
    print()
    print(f"  {'phase':>10}  {'n':>6}  {'rho':>7}  {'MAE':>7}")
    for lo, hi, name in ((0, 6, "endgame"), (7, 14, "middle"), (15, 24, "opening")):
        idx = [i for i, p in enumerate(phases) if lo <= p <= hi]
        if len(idx) < 20:
            print(f"  {name:>10}  {len(idx):>6}  {'-':>7}  {'-':>7}")
            continue
        o = [ours[i] for i in idx]
        t = [theirs[i] for i in idx]
        rr = [o[k] - slope * t[k] for k in range(len(idx))]
        m = sum(abs(x - sum(rr) / len(rr)) for x in rr) / len(rr)
        print(f"  {name:>10}  {len(idx):>6}  {spearman(o, t):+7.4f}  {m:7.1f}")
    print("\n  Regression instrument only -- NOT calibrated against Elo. "
          "A big move means look; a small one means nothing.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="score a fixed position set with Stockfish")
    b.add_argument("--positions", type=int, default=10000)
    b.add_argument("--depth", type=int, default=14)
    b.add_argument("--workers", type=int, default=0, help="0 = cpu_count-1")
    b.add_argument("--skip-plies", type=int, default=8,
                   help="ignore the first N plies of each game (book territory)")
    b.add_argument("--seed", type=int, default=59)
    b.add_argument("--out", default=DEFAULT_REF)
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("test", help="score an engine/net against the reference")
    t.add_argument("engine", help="engine .py (e.g. engine_nnue_v10.py)")
    t.add_argument("--sample", type=int, default=1000)
    t.add_argument("--depth", type=int, default=10)
    t.add_argument("--seed", type=int, default=59)
    t.add_argument("--ref", default=DEFAULT_REF)
    t.set_defaults(func=cmd_test)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
