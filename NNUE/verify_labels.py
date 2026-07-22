#!/usr/bin/env python3
"""NNUE/verify_labels.py -- FI-15 Phase 2 label audit (the F49-30 gate).

    python3 NNUE/verify_labels.py NNUE/datasets/smoke100k.pygdata [--sample 200]

Three checks on a generated dataset:

1. REPRODUCTION (hard gate): sample records with hmc == 0, re-search each
   with the labeling config (CYCLE_DETECT=0, all other toggles = the
   confirmed defaults, cold TT, same node budget) and assert the stored
   label is reproduced exactly.
   hmc == 0 records carry no game-history repetition context, so the
   standalone re-search is byte-deterministic vs generation time.
2. HISTORY DRIFT (report only): the same re-search on hmc > 0 records --
   differences here are the documented residual history dependence
   (in-window repetition scoring), not corruption.
3. CYCLE SHAPING (report): re-search the sample with CYCLE_DETECT=1 in a
   separate process (one process = one config) and report how many labels
   FI-29 would have draw-flattened -- the population F49-30 exists for.
"""

import argparse
import os
import subprocess
import sys

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess

from config import LABEL_NODES
from data_format import read_pygdata


def mk_board(r):
    b = chess.Board(None)
    b.pawns = int(r["pawns"]); b.knights = int(r["knights"])
    b.bishops = int(r["bishops"]); b.rooks = int(r["rooks"])
    b.queens = int(r["queens"]); b.kings = int(r["kings"])
    union = (b.pawns | b.knights | b.bishops | b.rooks | b.queens | b.kings)
    b.occupied_co[chess.WHITE] = int(r["occ_w"])
    b.occupied_co[chess.BLACK] = union & ~int(r["occ_w"])
    b.occupied = union
    b.turn = bool(r["stm"])
    b.castling_rights = int(r["castling"])
    b.ep_square = int(r["ep"]) if r["ep"] >= 0 else None
    b.halfmove_clock = int(r["hmc"])
    return b


def research(records, nodes, cycle_on):
    """Re-search each record, return White-POV scores (this process's
    engine config is fixed by the first construction -- FB-04)."""
    import cengine

    class AuditEngine(cengine.Engine):
        CYCLE_DETECT = bool(cycle_on)   # must mirror gen_data's LabelEngine
        USE_NNUE = False                # (all other toggles = confirmed
                                        # defaults, same as the generator)

    eng = AuditEngine()
    eng.use_book = False
    eng.use_tb = False
    eng.smp_workers = 1
    out = []
    for r in records:
        eng._lib.cs_tt_reset()
        eng.node_limit = nodes
        eng.get_best_move(mk_board(r), 24)
        out.append(eng.last_score)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--nodes", type=int, default=LABEL_NODES)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cycle-on-worker", help=argparse.SUPPRESS,
                    action="store_true")
    args = ap.parse_args()

    d = read_pygdata(args.dataset)
    rng = np.random.default_rng(args.seed)

    if not args.cycle_on_worker:        # dataset health summary first
        s = d["score"].astype(np.int64)
        res = dict(zip(*np.unique(d["result"], return_counts=True)))
        stm = dict(zip(*np.unique(d["stm"], return_counts=True)))
        print(f"dataset: {len(d):,} records "
              f"({os.path.getsize(args.dataset)/1e9:.2f} GB)")
        print(f"  score: mean {s.mean():+.1f}  std {s.std():.0f}  "
              f"pct[5/50/95] {np.percentile(s, [5, 50, 95]).astype(int)}")
        print(f"  result W/D/L: {res.get(1, 0):,}/{res.get(0, 0):,}/"
              f"{res.get(-1, 0):,}   stm W/B: {stm.get(1, 0):,}/"
              f"{stm.get(0, 0):,}   hmc max {d['hmc'].max()}")

    if args.cycle_on_worker:            # subprocess: CYCLE_DETECT=1 pass
        idx = rng.choice(len(d), min(args.sample, len(d)), replace=False)
        recs = np.asarray(d[np.sort(idx)])
        scores = research(recs, args.nodes, cycle_on=True)
        diffs = sum(int(s) != int(r["score"]) for s, r in zip(scores, recs))
        print(f"CYCLE_ON_DIFFS {diffs} / {len(recs)}")
        return

    idx = rng.choice(len(d), min(args.sample, len(d)), replace=False)
    recs = np.asarray(d[np.sort(idx)])
    scores = research(recs, args.nodes, cycle_on=False)

    h0 = [(s, r) for s, r in zip(scores, recs) if r["hmc"] == 0]
    hN = [(s, r) for s, r in zip(scores, recs) if r["hmc"] > 0]
    bad0 = sum(int(s) != int(r["score"]) for s, r in h0)
    badN = sum(int(s) != int(r["score"]) for s, r in hN)
    print(f"reproduction (hmc==0, HARD GATE): {len(h0)-bad0}/{len(h0)} "
          f"exact ({bad0} mismatches)")
    print(f"history drift (hmc>0, report):    {len(hN)-badN}/{len(hN)} "
          f"exact ({badN} differ -- residual in-window repetition context)")

    # cycle-on comparison in a fresh process (one process = one config)
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__), args.dataset,
         "--sample", str(args.sample), "--nodes", str(args.nodes),
         "--seed", str(args.seed), "--cycle-on-worker"],
        capture_output=True, text=True, cwd=REPO_DIR)
    line = [l for l in p.stdout.splitlines() if l.startswith("CYCLE_ON_DIFFS")]
    print(f"FI-29 shaping population (report): "
          f"{line[0].split()[1] if line else '?'} of {args.sample} labels "
          "would differ with CYCLE_DETECT=1 (why F49-30 pins it off)")
    sys.exit(0 if bad0 == 0 else 1)


if __name__ == "__main__":
    main()
