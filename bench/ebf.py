#!/usr/bin/env python3
"""
FI-108: effective branching factor -- the selectivity lane's missing gate.

    python3 bench/ebf.py <A.py> [B.py] [--depth 18] [--lo 10] [--cpu N]
    python3 bench/ebf.py --control        # the proven-failing control
    python3 bench/ebf.py --selftest       # statistics only, runs anywhere

WHY THIS EXISTS
---------------
The search lane is 2-for-9 on campaigns with five more items closed pre-A/B,
and every one of those verdicts cost either a 2k screen (~36 min of rented
box) or a full campaign. Nothing cheaper than a game ever stood between an
idea and a slot.

EBF is that cheaper thing. It is one fixed-depth search per position, no games
at all, and it measures the quantity a selectivity change actually claims to
move: how fast the tree grows per ply.

    2026-07-27, 1 thread, matched 256 MB Hash, startpos, d22:
      Pygin      113,885,183 nodes    EBF 1.753
      Stockfish 18 1,326,028 nodes    EBF 1.415

    At equal nodes that is ~62% of Stockfish's depth (ln 1.415 / ln 1.753),
    and it is why the node ratio drifts from ~10x at d12 to ~86x at d22.

THE RULE IS ONE-SIDED. SAY IT OUT LOUD OR IT WILL BE MISUSED.
-------------------------------------------------------------
    claims better pruning + EBF does NOT fall  ->  ABANDON before the screen
    EBF falls                                  ->  says NOTHING about Elo

A falling EBF is equally consistent with OVER-pruning that loses strength:
FI-55, FI-59 and FI-64 all cut nodes and all lost Elo. So this can abandon a
candidate, never confirm one. Identical discipline to `bench/instr_bench.py`,
and for the identical reason.

HOW THE NUMBER IS COMPUTED
--------------------------
  * A LEAST-SQUARES FIT of ln(nodes) against depth over [lo, hi], not a
    two-point ratio. Two points make the answer hostage to one noisy depth;
    the fit uses every iteration and reports r^2 so a bad fit is visible.
  * PER POSITION, then the MEDIAN across the suite. One position is one
    tree shape -- today's 1.753 came from startpos alone and should not be
    quoted as the engine's EBF.
  * THREADS=1, always. Helper nodes are duplicated work: at Threads=4 the
    node count inflates ~37% and the EBF it implies is fiction.
  * FRESH PROCESS PER POSITION, cold TT. csearch.so keeps eval params and the
    table in process-wide globals (the .so cross-contamination rule), and a
    warm table across positions would flatter the later ones.
  * LOW DEPTHS EXCLUDED by default (lo=10). The first iterations are dominated
    by move-count constants and TT effects, not by branching.

THE CONTROL
-----------
`--control` runs the engine against ITSELF with `set_prune(0)` on the B side --
Pygin's own pruning kill switch, used for value-identity verification. Pruning
off MUST raise the EBF sharply. If it does not, the instrument cannot see
selectivity at all and no verdict from it means anything.

That control needs no production change: the driver flips the toggle on a live
handle after construction, so nothing ships differently.
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys

_ROOT_ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT_, os.path.join(_ROOT_, "lib")]

# The house bench suite: opening, two middlegames, Kiwipete, a pawn ending and
# a tactical position. Same six the bench signature uses, so a tree-shape claim
# is measured over the same ground the drift oracle covers.
SUITE = [
    ("startpos",   "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("open-italian", "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3"),
    ("midgame",    "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10"),
    ("kiwipete",   "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("pawn-ending", "8/2k5/3p4/p2P1p2/P2P1P2/8/8/4K3 w - - 0 1"),
    ("tactical",   "r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1"),
]

# One position, one fixed-depth search, nodes recorded at every iteration.
# `noprune` is the control hook -- it disables Pygin's pruning on a live handle.
_CHILD = r'''
import importlib.util, json, os, sys
root, path, fen, depth, cpu, noprune, arm = (
    sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]),
    int(sys.argv[5]), int(sys.argv[6]), sys.argv[7])
sys.path[:0] = [root, os.path.join(root, "lib")]
os.chdir(root)
if cpu >= 0 and hasattr(os, "sched_setaffinity"):
    try: os.sched_setaffinity(0, {cpu})
    except OSError: pass
import chess
spec = importlib.util.spec_from_file_location("ebf_engine", path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
# --arm NAME=VALUE: set a class attr BEFORE construction. Every R10 item is a
# toggle, and arming one on the B side is how the gate is run without staging
# a whole directory. Set on the CLASS, not the instance: cengine pushes its
# toggles to the .so during __init__, so an instance-level set lands too late.
if arm:
    _n, _, _v = arm.partition("=")
    _val = {"1": True, "true": True, "0": False, "false": False}.get(_v.lower())
    if _val is None:
        _val = int(_v) if _v.lstrip("-").isdigit() else _v
    if not hasattr(mod.Engine, _n):
        sys.exit(f"--arm: {mod.Engine.__module__}.Engine has no attribute {_n!r}")
    setattr(mod.Engine, _n, _val)
eng = mod.Engine()
eng.use_book = False
try: eng.use_tb = False
except Exception: pass
try: eng.smp_workers = 1          # helper nodes are duplicated work
except Exception: pass
if noprune:                       # THE CONTROL: Pygin's own kill switch
    eng._lib.set_prune(0)
rows = {}
eng.on_depth = lambda r: rows.__setitem__(int(r["depth"]), int(r["nodes"]))
try: eng._lib.cs_tt_reset()
except Exception: pass
eng.get_best_move(chess.Board(fen), depth)
print("EBF " + json.dumps(rows))
'''


def fit_ebf(rows, lo, hi):
    """Least-squares slope of ln(nodes) vs depth over [lo, hi] -> (ebf, r2, n).

    A two-point ratio makes the answer hostage to one noisy iteration; the fit
    uses every depth in range and r2 exposes a bad fit instead of hiding it.
    """
    pts = [(d, math.log(n)) for d, n in sorted(rows.items())
           if lo <= d <= hi and n > 0]
    if len(pts) < 3:
        return None, None, len(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None, len(pts)
    slope = sxy / sxx
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in pts)
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    return math.exp(slope), r2, len(pts)


def measure(engine_path, fen, depth, cpu=-1, noprune=0, arm=""):
    r = subprocess.run([sys.executable, "-c", _CHILD, _ROOT_, engine_path, fen,
                        str(depth), str(cpu), str(noprune), arm],
                       capture_output=True, text=True, cwd=_ROOT_)
    line = next((l for l in r.stdout.splitlines() if l.startswith("EBF ")), None)
    if line is None:
        raise RuntimeError(f"{engine_path}: no result\n{r.stdout}\n{r.stderr}")
    return {int(k): v for k, v in json.loads(line[4:]).items()}


def run_side(engine_path, depth, lo, cpu, noprune, label, arm=""):
    print(f"\n  {label}")
    ebfs, out = [], {}
    for name, fen in SUITE:
        rows = measure(engine_path, fen, depth, cpu, noprune, arm)
        e, r2, n = fit_ebf(rows, lo, depth)
        top = rows.get(max(rows) if rows else 0, 0)
        if e is None:
            print(f"    {name:<14} EBF   n/a   (only {n} usable depths)")
            continue
        flag = "  ** poor fit **" if r2 is not None and r2 < 0.97 else ""
        print(f"    {name:<14} EBF {e:.3f}   r2 {r2:.4f}   "
              f"nodes@d{max(rows)} {top:>13,}{flag}")
        ebfs.append(e)
        out[name] = e
    if not ebfs:
        raise RuntimeError("no position produced a usable fit")
    med = statistics.median(ebfs)
    print(f"    {'MEDIAN':<14} EBF {med:.3f}   "
          f"(spread {min(ebfs):.3f}..{max(ebfs):.3f} over {len(ebfs)} positions)")
    return med, out


def selftest():
    """The statistics, with a control: the fit must recover a known slope, and
    must REFUSE rather than guess when it has too few points."""
    for true_ebf in (1.4, 1.75, 2.5):
        rows = {d: int(1000 * true_ebf ** d) for d in range(10, 23)}
        e, r2, n = fit_ebf(rows, 10, 22)
        assert abs(e - true_ebf) < 1e-3, (true_ebf, e)
        assert r2 > 0.9999, r2
        assert n == 13, n

    # noise must not shift it much, but must show in r2
    rows = {d: int(1000 * 1.75 ** d * (1.15 if d % 2 else 0.87))
            for d in range(10, 23)}
    e, r2, _ = fit_ebf(rows, 10, 22)
    assert 1.70 < e < 1.80, e
    assert r2 < 0.999, r2          # the noise IS visible, not swallowed

    # THE REFUSAL CONTROL: too few points must return None, never a number.
    # Guessing an EBF from two iterations is how a selectivity verdict gets
    # built on nothing.
    assert fit_ebf({10: 100, 11: 200}, 10, 22)[0] is None
    assert fit_ebf({}, 10, 22)[0] is None
    assert fit_ebf({10: 0, 11: 0, 12: 0}, 10, 22)[0] is None

    # a flat tree (no growth) is EBF 1.0, not a crash
    e, _, _ = fit_ebf({d: 5000 for d in range(10, 20)}, 10, 19)
    assert abs(e - 1.0) < 1e-9, e
    print("ebf selftest: OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("engine_a", nargs="?", default="cengine.py")
    ap.add_argument("engine_b", nargs="?")
    ap.add_argument("--depth", type=int, default=18)
    ap.add_argument("--lo", type=int, default=10,
                    help="first depth in the fit (early plies are constants, "
                         "not branching)")
    ap.add_argument("--cpu", type=int, default=-1)
    ap.add_argument("--control", action="store_true",
                    help="engine vs ITSELF with pruning disabled -- EBF must "
                         "rise sharply, or this instrument sees nothing")
    ap.add_argument("--arm-b", default="",
                    help="NAME=VALUE class attr armed on the B side only, e.g. "
                         "CUTNODE_LMR=1 -- the usual way to gate an R10 toggle")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest(); return 0

    print(f"EBF over {len(SUITE)} positions, ln(nodes)~depth fit on "
          f"d{a.lo}..d{a.depth}, Threads=1, fresh process per position"
          + (f", pinned to cpu {a.cpu}" if a.cpu >= 0 else ""))

    if a.control:
        base, _ = run_side(a.engine_a, a.depth, a.lo, a.cpu, 0,
                           f"A = {a.engine_a} (pruning ON)")
        ctrl, _ = run_side(a.engine_a, a.depth, a.lo, a.cpu, 1,
                           f"B = {a.engine_a} (pruning OFF -- set_prune(0))")
        rise = (ctrl / base - 1.0) * 100.0
        print(f"\n  CONTROL: EBF {base:.3f} -> {ctrl:.3f}  ({rise:+.1f}%)")
        ok = ctrl > base * 1.05
        print(f"  {'PASS' if ok else 'FAIL'} -- pruning off must raise EBF "
              f"sharply. {'It does.' if ok else 'It does NOT, so this '
              'instrument cannot see selectivity and no verdict from it means '
              'anything.'}")
        return 0 if ok else 1

    med_a, _ = run_side(a.engine_a, a.depth, a.lo, a.cpu, 0, f"A = {a.engine_a}")
    if not a.engine_b:
        return 0
    med_b, _ = run_side(a.engine_b, a.depth, a.lo, a.cpu, 0,
                        f"B = {a.engine_b}"
                        + (f"  [armed {a.arm_b}]" if a.arm_b else ""), a.arm_b)
    delta = (med_b / med_a - 1.0) * 100.0
    print(f"\n  A {med_a:.3f}   B {med_b:.3f}   EBF change {delta:+.2f}%")
    if delta >= 0.0:
        print("  ABANDON (if B claims better pruning): the tree does not grow\n"
              "          more slowly, so B is not doing what it claims.")
    else:
        print("  EBF fell. THIS CONFIRMS NOTHING about Elo -- over-pruning\n"
              "          lowers EBF too, and FI-55/FI-59/FI-64 all cut nodes\n"
              "          and lost Elo. Screen it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
