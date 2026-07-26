#!/usr/bin/env python3
"""FI-101: is the `--nodes` calibration ratio valid where it is actually used?

    python3 testing/nodes_calibration.py "New logs/cengine_vs_engine53_*.txt"
    python3 testing/nodes_calibration.py --selftest

THE QUESTION. FI-81 calibrates each engine at **fixed depth 11 on 6 fixed
FENs, sequentially, on a quiet pre-spawn machine**, and then the campaign plays
at **fixed 1.75M nodes on pool FENs with every worker loaded**. Those are
different operating points in four ways at once -- depth regime, position mix,
cache/branch profile, and machine load -- and the ratio has always been assumed
to transfer. This reads the ratio back out of a finished campaign's own per-
move `info` lines and puts the two numbers side by side.

Only the RATIO has to transfer, not the absolute NPS, because the ratio is all
that sets the budgets. That distinction matters: absolute NPS collapses under
worker load (measured below), and someone reading only the first number would
conclude the calibration was broken when it was not.

WHAT IT SHOWED, 2026-07-27. cengine vs Old Engine/55 -- two builds of the same
search, so the true ratio is 1.000 and any deviation is instrument error:

  bench NPS (quiet, pre-spawn, d11x6)   4.250M    4.490M   -> ratio 1.0340
  operating NPS (in-campaign, loaded)   3.814M    3.800M   -> ratio 0.9963
  GAP -3.64%, outside the 1% deadband the budgets were set with

**The bench ratio was the wrong one.** It called a 3.4% speed difference
between two engines that do not have one, and engine2 was handed 310,279 nodes
per move against engine1's 300,000 for the whole run. The operating-point
number is a median over 1,674 moves instead of 5 bench rounds, and it landed
within 0.4% of the truth. Same run, end-of-campaign recalibration read
**-2.70% drift** and both calibrations tripped the HOST-03 spread warning: this
Mac is not a quiet box, which is the condition FI-81 assumes.

The load factor line is separate and is NOT a defect: absolute NPS falls under
worker load (0.90x / 0.85x here) and only the RATIO has to transfer, since the
ratio alone sets the budgets. On a 95-worker campaign box the fall is far
larger -- the v54 Texel candidate vs 53 log reads 1.616M / 1.739M against ~4.4M
quiet, a ~2.6x drop over 215,290 moves per side, at an operating ratio of
**1.0763**. That run predates this tool, so its bench ratio was never recorded
and the gap cannot be recovered; recording it is why match.py now writes the
calibration into the log header.

**CAVEAT.** The two sides' NPS are not measured on identical positions -- games
diverge, and each side searched whatever its game reached. Over a full paired
campaign the mix is near-symmetric (every FEN is played from both colours by
both engines), so the bias is small at 215k moves and larger at 1.6k. Read a
gap inside the deadband as reassuring and a gap outside it as "re-measure
deliberately", not as a conviction on its own. The controlled half of FI-101 --
match.py re-running the calibration at campaign END and warning on drift --
lives in match.py, not here.

NULLS ARE INVISIBLE HERE. A null runs the same engine file on both sides, so
both sets of `info` lines carry the same name and cannot be separated. Use
`testing/pair_identity.py` for nulls; it answers the same host question by a
different route (identical mirror pairs).
"""
import glob
import re
import statistics
import sys

_INFO = re.compile(r"^\[([^\]]+)\] move \S+ info .*?\bnps (\d+)")
_BENCH = re.compile(r"engine1 ([\d.]+)M nps, engine2 ([\d.]+)M nps")
_RATIOS = re.compile(r"median ([\d.]+)")


def scan(path):
    """(per-engine nps samples, bench ratio or None, bench NPS pair or None)."""
    nps = {}
    bench_ratio = bench_nps = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _INFO.match(line)
            if m:
                nps.setdefault(m.group(1), []).append(int(m.group(2)))
                continue
            if bench_ratio is None and "median" in line and "round ratios" in line:
                r = _RATIOS.search(line)
                if r:
                    bench_ratio = float(r.group(1))
            if bench_nps is None:
                b = _BENCH.search(line)
                if b:
                    bench_nps = (float(b.group(1)) * 1e6, float(b.group(2)) * 1e6)
    return nps, bench_ratio, bench_nps


def report(path):
    nps, bench_ratio, bench_nps = scan(path)
    print(f"\n{path}")
    if len(nps) != 2:
        names = ", ".join(sorted(nps)) or "none"
        print(f"  cannot split the two sides (engines seen: {names}).")
        if len(nps) == 1:
            print("  A null shares one engine NAME -- use pair_identity.py.")
        return
    (n1, s1), (n2, s2) = sorted(nps.items(), key=lambda kv: -len(kv[1]))[:2]
    # Engine 1 is the reference and is named first in the log's title line;
    # fall back to sample count only if that lookup fails.
    with open(path, encoding="utf-8", errors="replace") as fh:
        title = fh.readline()
    if title.strip().startswith(n2 + " vs "):
        (n1, s1), (n2, s2) = (n2, s2), (n1, s1)
    m1, m2 = statistics.median(s1), statistics.median(s2)
    print(f"  operating NPS (in-campaign, loaded):")
    print(f"    {n1:<20} {m1/1e6:.3f}M   ({len(s1):,} moves)")
    print(f"    {n2:<20} {m2/1e6:.3f}M   ({len(s2):,} moves)")
    op = m2 / m1
    if bench_nps:
        print(f"  bench NPS (quiet, pre-spawn, d11x6):")
        print(f"    {n1:<20} {bench_nps[0]/1e6:.3f}M")
        print(f"    {n2:<20} {bench_nps[1]/1e6:.3f}M")
        print(f"  load factor: {n1} {m1/bench_nps[0]:.2f}x, "
              f"{n2} {m2/bench_nps[1]:.2f}x")
    print(f"  operating ratio (e2/e1) : {op:.4f}")
    if bench_ratio is None:
        print("  bench ratio             : not in this log (pre-FI-101 run; "
              "the calibration was printed to stdout only)")
        return
    gap = op / bench_ratio - 1.0
    print(f"  bench ratio (calibrated): {bench_ratio:.4f}")
    print(f"  GAP                     : {gap*100:+.2f}%"
          + ("   ** outside the 1% deadband the budgets were set with **"
             if abs(gap) > 0.01 else "   (inside the 1% deadband)"))


def selftest():
    """The control: the parser must pull the right numbers out of real log
    lines, and must refuse a log whose two sides it cannot separate."""
    import os
    import tempfile

    log = ("cengine vs engine53\n"
           "Interpreter: cpython 3.12.3\n"
           "Mode: nodes 1750000/move (NPS-calibrated)\n"
           "NPS calibration (start, 5 interleaved bench rounds per engine):\n"
           "  round ratios: 0.99 1.00 1.02 -> median 1.020\n"
           "  engine1 4.00M nps, engine2 4.08M nps\n"
           "[cengine] move a4: info depth 13 score cp 84 nodes 17 nps 1000000 time 1\n"
           "[cengine] move b4: info depth 13 score cp 84 nodes 17 nps 2000000 time 1\n"
           "[cengine] move c4: info depth 13 score cp 84 nodes 17 nps 3000000 time 1\n"
           "[engine53] move a5: info depth 13 score cp 84 nodes 17 nps 2040000 time 1\n"
           "    PV: a2a4 nps 999 -- a continuation line must NOT be counted\n")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.txt")
        with open(p, "w") as fh:
            fh.write(log)
        nps, ratio, bench = scan(p)
        assert sorted(nps) == ["cengine", "engine53"], nps
        assert nps["cengine"] == [1000000, 2000000, 3000000], nps
        assert nps["engine53"] == [2040000], nps      # the PV line was skipped
        assert ratio == 1.020, ratio
        assert bench == (4.00e6, 4.08e6), bench
        # median 2.00M vs 2.04M -> operating ratio 1.020, gap 0.0% vs bench
        assert abs(statistics.median(nps["engine53"])
                   / statistics.median(nps["cengine"]) - 1.02) < 1e-9

        # a null: one name on both sides, and it must SAY so rather than
        # silently reporting a ratio of the engine against itself
        p2 = os.path.join(d, "null.txt")
        with open(p2, "w") as fh:
            fh.write("cengine vs cengine\n"
                     "[cengine] move a4: info depth 1 nodes 1 nps 100 time 1\n")
        assert len(scan(p2)[0]) == 1
    print("nodes_calibration selftest: OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        selftest()
    else:
        for a in args:
            for p in sorted(glob.glob(a)) or [a]:
                report(p.replace(".pgn", ".txt"))
