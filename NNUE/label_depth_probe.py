#!/usr/bin/env python3
"""NNUE/label_depth_probe.py -- is LABEL_NODES=5000 deep enough?

    python3 NNUE/label_depth_probe.py NNUE/datasets/<set>.pygdata --sample 300

THE QUESTION. Labels are `--nodes 5000` searches; the engine they steer plays
at ~1.75M nodes, 350x deeper. Every net trained on this corpus has lost to
v12, volume has been measured twice as NOT the constraint, and the training
recipe lane is closed -- so label QUALITY is the remaining lever. Before
paying 10x the CPU to regenerate a corpus at greater depth, measure whether
greater depth even changes the answer.

WHAT IT REPORTS. Re-searches the same positions at rising node budgets and
compares each to the stored 5,000-node label:
  * mean / median / p90 |delta cp|      -- how far labels move
  * share moving more than 25 cp        -- a quarter pawn, enough to matter
  * sign flips                          -- deeper search picks the OTHER side
  * the CONSECUTIVE deltas              -- the one that decides it

READ THE CONSECUTIVE DELTAS, NOT THE TOTAL. If 5k->25k moves labels a lot but
25k->50k and 50k->100k barely move them, the label has CONVERGED and depth is
not the lever -- the 5k label is simply a noisy draw from the same
distribution the deeper ones agree on. If the deltas are still large at the
top budget, the label has not converged and a deeper corpus is worth
generating. A big 5k->100k number alone proves nothing either way.

TT SIZING IS PART OF THE MEASUREMENT, and getting it wrong invalidates the
probe. LABEL_TT_BITS is 16 = a 1.5 MB table, sized for 5,000-node searches.
Running a 100,000-node search in that table thrashes it and would understate
what depth buys -- the probe would then blame depth for a table shortage. So
the table is scaled to hold the SAME entries-per-node ratio the shipped label
config has (65,536 / 5,000 = 13.1), via set_tt_bits between passes. The 5,000
baseline pass keeps TT_BITS=16 exactly, so it reproduces the stored label.

One process, one engine config (FB-04) -- node_limit and TT size are runtime
settings, not config, so every budget runs in this single process.
"""

import argparse
import math
import os
import sys
import time

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

from config import (LABEL_NODES, LABEL_TT_BITS,      # noqa: E402
                    LABEL_MAX_ABS_CP)
from data_format import read_pygdata                   # noqa: E402
from verify_labels import mk_board                     # noqa: E402

# Entries per searched node in the shipped label config. Every deeper pass
# keeps this ratio so the table is never the thing that limits the search.
_ENTRIES_PER_NODE = (1 << LABEL_TT_BITS) / LABEL_NODES


def tt_bits_for(nodes):
    if nodes == LABEL_NODES:
        return LABEL_TT_BITS            # exact, so the baseline reproduces
    return max(LABEL_TT_BITS,
               int(math.ceil(math.log2(nodes * _ENTRIES_PER_NODE))))


def progress(done, total, t0, stage):
    """Count / percent / rate / elapsed / ETA. A silent script is
    indistinguishable from a hung one; \\r only when stdout is a tty."""
    el = time.time() - t0
    rate = done / el if el > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    line = (f"  {stage}  {done:,}/{total:,} ({100.0*done/total:5.1f}%)  "
            f"{rate:5.2f} pos/s  elapsed {el/60:5.1f}m  ETA {eta/60:5.1f}m")
    if sys.stdout.isatty():
        print("\r" + line + "   ", end="", flush=True)
    elif done == total or done % max(1, total // 10) == 0:
        print(line, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--budgets", default="25000,50000,100000",
                    help="comma-separated node budgets to compare against "
                         f"the stored {LABEL_NODES}-node label")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    d = read_pygdata(args.dataset)

    # hmc == 0 ONLY: those records carry no repetition context, so a
    # standalone re-search is byte-deterministic against generation time.
    # Anything else mixes history drift into the depth signal.
    h0 = np.flatnonzero(d["hmc"] == 0)
    if len(h0) == 0:
        sys.exit("no hmc==0 records in this dataset -- cannot probe cleanly")
    rng = np.random.default_rng(args.seed)
    idx = rng.choice(h0, min(args.sample, len(h0)), replace=False)
    recs = np.asarray(d[np.sort(idx)])
    print(f"dataset: {len(d):,} records, {len(h0):,} with hmc==0; "
          f"probing {len(recs):,}")
    print(f"budgets: {LABEL_NODES:,} (stored) -> "
          + " -> ".join(f"{b:,}" for b in budgets))
    for b in [LABEL_NODES] + budgets:
        print(f"  {b:>9,} nodes  TT_BITS {tt_bits_for(b):2d} "
              f"= {(1 << tt_bits_for(b)) * 24 / 2**20:7.1f} MiB")
    print()

    import cengine

    class AuditEngine(cengine.Engine):
        CYCLE_DETECT = False            # mirrors gen_data's LabelEngine
        USE_NNUE = False
        TT_BITS = LABEL_TT_BITS

    eng = AuditEngine()
    eng.use_book = False                # startpos would answer from the book
    eng.use_tb = False                  # and a TB hit is not a search at all
    eng.smp_workers = 1

    boards = [mk_board(r) for r in recs]
    stored = np.array([int(r["score"]) for r in recs], dtype=np.int64)

    scores = {}
    for b in [LABEL_NODES] + budgets:
        eng._lib.set_tt_bits(int(tt_bits_for(b)))
        out, t0 = [], time.time()
        for i, bd in enumerate(boards, 1):
            eng._lib.cs_tt_reset()      # cold table per position, as gen_data
            eng.node_limit = b
            eng.get_best_move(bd, 24)
            out.append(eng.last_score)
            if i % 10 == 0 or i == len(boards):
                progress(i, len(boards), t0, f"{b:>7,}n")
        if sys.stdout.isatty():
            print()
        scores[b] = np.array(out, dtype=np.int64)

    repro = int((scores[LABEL_NODES] == stored).sum())
    print(f"\nbaseline reproduction: {repro}/{len(recs)} exact"
          + ("" if repro == len(recs) else
             "  <- NOT exact: build/config drift, read the rest with care"))

    def cmp(a, b, label):
        """Compare two score vectors, EXCLUDING mate scores.

        A deeper search that finds a mate returns +/-30000ish, and a handful
        of those swamp any mean: the first version of this probe reported a
        mean |delta| of 1,034cp next to a MEDIAN of 16, which is not a
        measurement, it is 69 mate scores. gen_data drops |score| >
        LABEL_MAX_ABS_CP from the corpus anyway, so those positions are not
        training targets and do not belong in the label-movement figure.
        They are counted on their own line instead -- deeper search finding
        mates the shallow one missed is a real effect, just a different one.
        """
        keep = (np.abs(a) <= LABEL_MAX_ABS_CP) & (np.abs(b) <= LABEL_MAX_ABS_CP)
        mates = int((~keep).sum())
        aa, bb = a[keep], b[keep]
        ad = np.abs((bb - aa).astype(np.int64))
        flips = int(((aa > 0) & (bb < 0)).sum() + ((aa < 0) & (bb > 0)).sum())
        print(f"  {label:22s} median |d| {np.median(ad):6.1f}cp   "
              f"mean {ad.mean():6.1f}   p90 {np.percentile(ad, 90):6.1f}"
              f"   >25cp {100.0*(ad > 25).mean():5.1f}%   "
              f"flips {100.0*flips/max(1, len(ad)):4.1f}%"
              f"   mate-scored {mates:3d}")
        return float(np.median(ad))

    print("\nvs the STORED label (total movement):")
    for b in budgets:
        cmp(stored, scores[b], f"{LABEL_NODES:,} -> {b:,}")

    print("\nCONSECUTIVE steps -- this is the one that decides it:")
    chain = [LABEL_NODES] + budgets
    steps = [cmp(scores[lo], scores[hi], f"{lo:,} -> {hi:,}")
             for lo, hi in zip(chain, chain[1:])]

    print()
    if len(steps) < 2:
        print("Only one budget above the baseline, so there is no consecutive\n"
              "step to compare against -- the convergence verdict needs at\n"
              "least two (e.g. --budgets 25000,50000,100000).")
        return
    # Median, not mean: robust to the mate tail that made the first version
    # of this verdict meaningless.
    first, last = steps[0], steps[-1]
    if last <= 0.35 * first:
        print(f"CONVERGING: the last doubling moves labels {last:.1f}cp "
              f"against {first:.1f}cp for the first step.\nDepth is NOT the "
              "lever -- a deeper corpus would buy a quieter version of the "
              "same label.")
    else:
        print(f"NOT CONVERGED: the last doubling still moves labels "
              f"{last:.1f}cp against {first:.1f}cp for the first step.\n"
              "Deeper labels are genuinely different targets, not just "
              "quieter ones; a deeper corpus is worth its CPU.")
    print("\nNeither line is an Elo claim. It says whether the corpus can "
          "differ, not whether\na net trained on it plays better -- only an "
          "A/B says that.")


if __name__ == "__main__":
    main()
