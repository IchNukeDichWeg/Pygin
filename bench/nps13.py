#!/usr/bin/env python3
"""
nps13.py -- the house NPS instrument, canonized (FI-84).

    python3 bench/nps13.py <engineA.py> <engineB.py> [--rounds 16] [--depth 13]

Reports the median of PAIRED depth-13 NPS ratios (B relative to A), the
spread, and a sign test. A ratio above 1.0 means B is faster.

WHY THIS EXISTS
---------------
Every bench-class item in the backlog is decided on NPS, and the protocol
that decides them existed only as knowledge -- FI-66 (+0.54%, shipped) and
FI-67 (-0.26%, closed) were each measured with a throwaway script that no
longer exists. Two verdicts, two different harnesses, numbers not comparable.

The protocol, and why each part of it is load-bearing:

  * DEPTH 13, not the d11 bench. The d11 bench cannot resolve sub-1%: a
    9-round read of +1.65% flipped to -0.76% at 16 rounds (FI-66). Almost
    every remaining bench item is priced at 0.3-3%, i.e. inside the range
    d11 cannot see.
  * PAIRED ratios, interleaved A/B/A/B. Absolute NPS drifts with thermal
    state and whatever else the box is doing; a ratio measured back-to-back
    on the same machine in the same second does not.
  * ROUND 1 DISCARDED. The first round pays for page faults, .so load and a
    cold TT, and it pays them unequally between the two engines.
  * MEDIAN over >= 16 ratios, not the mean. One descheduled round produces
    an outlier that a mean happily absorbs and a median ignores.
  * FRESH SUBPROCESS PER MEASUREMENT. csearch.so keeps its eval params and
    TT in PROCESS-WIDE globals, so two engine versions in one process
    silently share them (the .so cross-contamination rule). Every single
    measurement here is its own interpreter.

ACCEPTANCE GATE (the entry's own test): re-measure a known pair and
reproduce its recorded verdict. A harness that cannot recover the number
that produced a shipped decision is not the instrument that produced it.

    python3 bench/nps13.py "Old Engine/53/engine53.py" cengine.py
"""
# Path shim: this script lives in bench/ but drives engines at the repo root.
import os as _os, sys as _sys
_ROOT_ = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_ROOT_, _os.path.join(_ROOT_, "lib")]

import argparse
import json
import statistics
import subprocess
import time

# A quiet middlegame with both sliders and pawn tension -- the same position
# family the CE_LADDER uses, so a d13 search here exercises the whole tree
# (movegen, ordering, TT, qsearch, eval) rather than a tactical corridor.
DEFAULT_FEN = ("r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R "
               "w KQkq - 3 3")

# The child: load ONE engine, run ONE fixed-depth search, print nodes+seconds.
# Kept as a string so each measurement is a genuinely fresh interpreter.
_CHILD = r'''
import importlib.util, json, os, sys, time
root, path, fen, depth = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path[:0] = [root, os.path.join(root, "lib")]
os.chdir(root)                      # engines resolve .so / data relative to cwd
import chess
spec = importlib.util.spec_from_file_location("nps13_engine", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
eng = mod.Engine()
eng.use_book = False                # FB-20: a book move would return instantly
try:
    eng.use_tb = False
except Exception:
    pass
try:
    eng.smp_workers = 1             # single-thread: this measures the core
except Exception:
    pass
try:                                # cold TT, so the search is reproducible
    eng._lib.cs_tt_reset()
except Exception:
    pass
board = chess.Board(fen)
t0 = time.perf_counter()
eng.get_best_move(board, depth)
dt = time.perf_counter() - t0
nodes = getattr(eng, "nodes_searched", 0)
print("NPS13 " + json.dumps({"nodes": nodes, "sec": dt}))
'''


def measure(engine_path, fen, depth):
    """One fixed-depth search in a FRESH process. Returns nodes/second."""
    r = subprocess.run([_sys.executable, "-c", _CHILD, _ROOT_, engine_path,
                        fen, str(depth)],
                       capture_output=True, text=True, cwd=_ROOT_)
    line = next((l for l in r.stdout.splitlines() if l.startswith("NPS13 ")),
                None)
    if line is None:
        raise RuntimeError(f"{engine_path}: no result\n{r.stdout}\n{r.stderr}")
    d = json.loads(line[len("NPS13 "):])
    if d["sec"] <= 0 or d["nodes"] <= 0:
        raise RuntimeError(f"{engine_path}: degenerate measurement {d}")
    return d["nodes"] / d["sec"], d["nodes"]


def sign_test_p(wins, n):
    """Two-sided exact binomial p under H0: ratio is equally likely either
    way. Reported alongside the median because a 0.4% median built from
    16 of 16 rounds in the same direction is a different claim from the
    same median built from 9 of 16."""
    if n == 0:
        return 1.0
    from math import comb
    k = min(wins, n - wins)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main():
    ap = argparse.ArgumentParser(
        description="paired depth-13 NPS ratios (the FI-84 instrument)")
    ap.add_argument("engine_a")
    ap.add_argument("engine_b")
    ap.add_argument("--rounds", type=int, default=17,
                    help="rounds to run; round 1 is DISCARDED, so the default "
                         "17 yields the protocol's 16 usable ratios")
    ap.add_argument("--depth", type=int, default=13,
                    help="search depth (default 13 -- d11 cannot resolve "
                         "sub-1%%, see the module docstring)")
    ap.add_argument("--fen", default=DEFAULT_FEN)
    a = ap.parse_args()

    for p in (a.engine_a, a.engine_b):
        if not _os.path.isfile(_os.path.join(_ROOT_, p)) and not _os.path.isfile(p):
            _sys.exit(f"engine not found: {p}")

    print(f"paired d{a.depth} NPS ratios -- B relative to A")
    print(f"  A = {a.engine_a}")
    print(f"  B = {a.engine_b}")
    print(f"  {a.rounds} rounds (round 1 discarded), fresh process per search\n")

    ratios, nodes_a, nodes_b = [], set(), set()
    t_start = time.time()
    for rnd in range(1, a.rounds + 1):
        # Interleave A,B then B,A on alternate rounds so a monotonic drift in
        # machine speed cannot favour whichever engine always goes first.
        if rnd % 2:
            (nps_a, na), (nps_b, nb) = (measure(a.engine_a, a.fen, a.depth),
                                        measure(a.engine_b, a.fen, a.depth))
        else:
            (nps_b, nb), (nps_a, na) = (measure(a.engine_b, a.fen, a.depth),
                                        measure(a.engine_a, a.fen, a.depth))
        nodes_a.add(na)
        nodes_b.add(nb)
        r = nps_b / nps_a
        tag = "  (discarded)" if rnd == 1 else ""
        if rnd > 1:
            ratios.append(r)
        print(f"  round {rnd:>3}/{a.rounds}  A {nps_a/1e6:6.3f}M  "
              f"B {nps_b/1e6:6.3f}M  ratio {r:.4f}{tag}", flush=True)

    med = statistics.median(ratios)
    wins = sum(1 for r in ratios if r > 1.0)
    p = sign_test_p(wins, len(ratios))
    print(f"\n  {len(ratios)} paired d{a.depth} ratios   "
          f"median {med:.4f}  ({(med - 1) * 100:+.2f}%)")
    print(f"  spread {min(ratios):.3f} .. {max(ratios):.3f}   "
          f"B faster in {wins}/{len(ratios)}  (sign test p={p:.3f})")
    print(f"  elapsed {time.time() - t_start:.0f}s")

    # Node counts are the identity check that comes free: a bench-class item
    # is supposed to be node-IDENTICAL, so differing node counts mean the
    # change altered the tree and this instrument is the wrong gate for it.
    if len(nodes_a) == 1 and len(nodes_b) == 1:
        na, nb = nodes_a.pop(), nodes_b.pop()
        if na == nb:
            print(f"  nodes identical ({na:,}) -- a valid bench-class pair")
        else:
            print(f"  !! nodes DIFFER: A {na:,} vs B {nb:,} -- this pair is "
                  f"NOT node-identical, so an NPS ratio does not decide it")
    else:
        print("  !! node counts varied across rounds -- non-deterministic "
              "search (SMP? book? TB?); the ratios are not trustworthy")

    if p > 0.05:
        print("\n  VERDICT: not resolved -- the sign test does not exclude "
              "chance. More rounds, or the effect is below this instrument.")
    else:
        print(f"\n  VERDICT: {'B faster' if med > 1 else 'B slower'} by "
              f"{abs(med - 1) * 100:.2f}% (median)")


if __name__ == "__main__":
    main()
