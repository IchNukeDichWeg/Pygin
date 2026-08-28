#!/usr/bin/env python3
"""NNUE/label_teacher_probe.py -- is the HCE labeller a worse teacher than the net?

    python3 NNUE/label_teacher_probe.py NNUE/datasets/<set>.pygdata --sample 4000

THE QUESTION. gen_data.py's LabelEngine pins USE_NNUE = False, so every corpus
we own was labelled by a hand-crafted-eval search. cengine has defaulted to
USE_NNUE = True since v58 armed it on 2026-08-04, and the comment beside that
pin states the policy it no longer follows: "labels now come from the
strongest confirmed search". The teacher has been frozen since before v58
while the student kept improving -- a plausible reason thirteen straight nets
have failed to beat v12, since a student that has caught its teacher learns
nothing more from it. This measures whether the net really is the better
teacher before anyone spends days regenerating a corpus.

SCALES DO NOT COMPARE, SO THIS DOES NOT COMPARE THEM. NNUE cp and HCE cp are
different scales -- it is why fit_wdl_model.py keeps a separate model per
family and refuses to pool them. Subtracting one teacher's label from the
other's would measure the scale gap, not teacher quality. So every metric here
is SCALE-FREE and judged against the same external truth:

  * Spearman rank correlation of each teacher's label with the GAME RESULT.
    Monotone-invariant, so it cannot be moved by rescaling either eval, and
    the result is ground truth that belongs to neither eval.
  * Sign agreement on decisive games -- did the label back the side that won.
  * Rank correlation between the two teachers, as context for how much they
    reorder positions relative to each other.

WHY THE GAME RESULT AND NOT A DEEP SEARCH. A deep NNUE reference would share
its eval with the NNUE candidate and correlate with it for that reason alone,
which flatters the answer we are trying to test. The game result has no such
allegiance. It is noisy per position, which is what the sample size is for.

ONE PROCESS PER CONFIG (FB-04): cengine's toggles are process-wide, so the two
teachers run as subprocesses of this script and never share an interpreter.

BUILT-IN ORACLE: the HCE arm must reproduce the stored labels EXACTLY, since
that is the config that generated them. If it does not, the comparison is
measuring drift rather than teachers, and the script says so.
"""

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

from config import LABEL_NODES, LABEL_TT_BITS            # noqa: E402
from data_format import read_pygdata                     # noqa: E402
from verify_labels import mk_board                       # noqa: E402

NET = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"    # the net v60 ships


def progress(done, total, t0, stage):
    el = time.time() - t0
    rate = done / el if el > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    line = (f"  {stage}  {done:,}/{total:,} ({100.0*done/total:5.1f}%)  "
            f"{rate:6.1f} pos/s  elapsed {el/60:5.1f}m  ETA {eta/60:5.1f}m")
    if sys.stderr.isatty():
        print("\r" + line + "   ", end="", file=sys.stderr, flush=True)
    elif done == total or done % max(1, total // 10) == 0:
        print(line, file=sys.stderr, flush=True)


def worker(npz_path, family, nodes):
    """Search every sampled position under ONE eval family, print scores."""
    d = np.load(npz_path)
    recs = d["recs"]
    import cengine

    class T(cengine.Engine):
        CYCLE_DETECT = False           # mirrors gen_data's LabelEngine
        TT_BITS = LABEL_TT_BITS        # ditto -- table size changes the search
        USE_NNUE = (family == "nnue")
        if family == "nnue":
            NNUE_FILE = NET
            LAZY_NNUE = True

    eng = T()
    eng.use_book = False
    eng.use_tb = False
    eng.smp_workers = 1
    out, t0 = [], time.time()
    for i, r in enumerate(recs, 1):
        eng._lib.cs_tt_reset()
        eng.node_limit = nodes
        eng.get_best_move(mk_board(r), 24)
        out.append(int(eng.last_score))
        if i % 25 == 0 or i == len(recs):
            progress(i, len(recs), t0, f"{family:4s}")
    if sys.stderr.isatty():
        print(file=sys.stderr)
    print(json.dumps(out))


def spearman(a, b):
    from scipy.stats import spearmanr
    return float(spearmanr(a, b).statistic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--sample", type=int, default=4000)
    ap.add_argument("--nodes", type=int, default=LABEL_NODES)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reference-nodes", type=int, default=0,
                    help="also search at this much deeper budget and ask "
                         "which shallow teacher better predicts it. The "
                         "game result is unbiased but noisy and dominated "
                         "by easy positions; a deep search is the sharper "
                         "reference for where a teacher actually differs.")
    ap.add_argument("--worker", choices=("hce", "nnue"), help=argparse.SUPPRESS)
    ap.add_argument("--npz", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return worker(args.npz, args.worker, args.nodes)

    if not os.path.isfile(os.path.join(REPO_DIR, NET)):
        sys.exit(f"missing net {NET} -- the nnue arm cannot run")

    d = read_pygdata(args.dataset)
    h0 = np.flatnonzero(d["hmc"] == 0)      # no repetition context -> exact
    rng = np.random.default_rng(args.seed)
    idx = rng.choice(h0, min(args.sample, len(h0)), replace=False)
    recs = np.asarray(d[np.sort(idx)])
    npz = os.path.join(REPO_DIR, ".teacher_probe_sample.npz")
    npz2 = npz
    np.savez(npz, recs=recs)

    stored = recs["score"].astype(np.int64)     # White POV cp
    result = recs["result"].astype(np.int64)    # White POV +1/0/-1
    print(f"dataset: {len(d):,} records, {len(h0):,} with hmc==0; "
          f"probing {len(recs):,} at {args.nodes:,} nodes")
    print(f"result mix W/D/L: {(result==1).sum():,}/{(result==0).sum():,}/"
          f"{(result==-1).sum():,}\n")

    scores = {}
    try:
        for fam in ("hce", "nnue"):
            p = subprocess.run(
                [sys.executable, os.path.abspath(__file__), args.dataset,
                 "--worker", fam, "--npz", npz, "--nodes", str(args.nodes)],
                capture_output=True, text=True, cwd=REPO_DIR)
            if p.returncode != 0:
                sys.exit(f"{fam} arm failed:\n{p.stderr[-2000:]}")
            scores[fam] = np.array(json.loads(p.stdout.strip().splitlines()[-1]),
                                   dtype=np.int64)
    finally:
        pass          # npz is removed at the end -- the reference passes reuse it

    exact = int((scores["hce"] == stored).sum())
    if args.nodes == LABEL_NODES:
        print(f"ORACLE -- hce reproduces the stored labels: {exact}/{len(recs)}"
              + ("" if exact == len(recs) else
                 "  <- NOT exact; this is drift, read nothing below as teachers"))
    else:
        # At any other budget the stored 5,000-node labels SHOULD differ, so
        # the reproduction count is not an oracle here and must not read like
        # a failure -- it cried wolf on the first 50,000-node run.
        print(f"(no oracle at {args.nodes:,} nodes: the stored labels are "
              f"{LABEL_NODES:,}-node searches, so {exact}/{len(recs)} matching "
              "is expected, not drift)")

    print("\nAGREEMENT WITH THE GAME RESULT (scale-free, the whole point):")
    for fam in ("hce", "nnue"):
        rho = spearman(scores[fam], result)
        dec = result != 0
        sign_ok = ((np.sign(scores[fam][dec]) == np.sign(result[dec])).mean()
                   if dec.sum() else float("nan"))
        print(f"  {fam:4s}  Spearman rho {rho:+.4f}   "
              f"sign agreement on decisive games {100*sign_ok:5.2f}%")

    if args.reference_nodes:
        ref = {}
        for fam in ("hce", "nnue"):
            p2 = subprocess.run(
                [sys.executable, os.path.abspath(__file__), args.dataset,
                 "--worker", fam, "--npz", npz2,
                 "--nodes", str(args.reference_nodes)],
                capture_output=True, text=True, cwd=REPO_DIR)
            if p2.returncode != 0:
                sys.exit(f"{fam} reference arm failed:\n{p2.stderr[-2000:]}")
            ref[fam] = np.array(
                json.loads(p2.stdout.strip().splitlines()[-1]), dtype=np.int64)
        print(f"\nAGREEMENT WITH A {args.reference_nodes:,}-NODE REFERENCE "
              "(sharper than the game result):")
        print("  the cross-family column is the fair one -- a reference shares "
              "its eval\n  with the same-family candidate and flatters it.")
        for rf in ("hce", "nnue"):
            a = spearman(scores["hce"], ref[rf])
            b = spearman(scores["nnue"], ref[rf])
            mark = "nnue better" if b - a > 0.002 else (
                   "hce better" if a - b > 0.002 else "no difference")
            print(f"  vs {rf:4s}@{args.reference_nodes:,}   "
                  f"hce@{args.nodes:,} {a:+.4f}   "
                  f"nnue@{args.nodes:,} {b:+.4f}   "
                  f"({b-a:+.4f}, {mark})")

    r_h = spearman(scores["hce"], result)
    r_n = spearman(scores["nnue"], result)
    print(f"\n  teacher-vs-teacher rank correlation: "
          f"{spearman(scores['hce'], scores['nnue']):+.4f}  "
          "(1.0 would mean they order positions identically)")

    print()
    d_rho = r_n - r_h
    if d_rho > 0.01:
        print(f"THE NET IS THE BETTER TEACHER: rho {r_h:+.4f} -> {r_n:+.4f} "
              f"({d_rho:+.4f}).\nRegenerating with USE_NNUE=True gives strictly "
              "better targets than the\ncorpus we have, independent of depth.")
    elif d_rho < -0.01:
        print(f"THE HCE IS THE BETTER TEACHER: rho {r_h:+.4f} -> {r_n:+.4f} "
              f"({d_rho:+.4f}).\nThe frozen labeller is not the plateau; look "
              "elsewhere.")
    else:
        print(f"NO MEANINGFUL DIFFERENCE: rho {r_h:+.4f} vs {r_n:+.4f} "
              f"({d_rho:+.4f}).\nSwapping the labeller alone would not change "
              "the targets; depth is the\nremaining lever.")
    if os.path.exists(npz):
        os.remove(npz)
    print("\nThis ranks LABEL QUALITY against game outcomes. It does not say a "
          "net trained\non those labels plays better -- only an A/B says that.")


if __name__ == "__main__":
    main()
