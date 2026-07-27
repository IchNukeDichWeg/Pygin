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

THE RESOLUTION FLOOR (learned the hard way, 2026-07-25)
-------------------------------------------------------
The sign test bounds WITHIN-run noise. It says nothing about run-to-run
drift, and the missing control was the obvious one: re-measure the SAME
BINARY. On an idle, pinned Gold 6330, one unchanged build read **+0.06%**
and then **+0.23%** vs the same baseline -- a 0.17-point gap at p=0.720 and
p=0.001 respectively. Both runs were internally clean; the instrument was
simply being asked a question finer than it can answer.

So: a tight spread and a p=0.001 do NOT license a 0.2% claim. Use
`--repeat 3` and treat the between-run spread as the floor. Anything priced
under ~1% needs the repeat; anything priced under the observed floor is not
decidable here at all, and no number of rounds fixes that (FI-72, priced
<=0.3%, was reverted for exactly this reason rather than for reading bad).

TIME-TO-DEPTH MODE, `--threads N` (FI-95)
-----------------------------------------
At Threads>1 this instrument switches the QUESTION it asks. Raw NPS counts
helper nodes that may be pure duplication, so under Lazy SMP **more NPS can
mean less search** -- the quantity that matters is how long the engine takes
to finish a given depth. `--threads N` therefore ranks A's seconds over B's
(so >1.0 still means "B better") and stops warning about varying node counts,
which are EXPECTED once helpers race the shared TT.

Everything else is unchanged: paired, interleaved, round 1 discarded, fresh
subprocess each, `--cpu` pinning, `--repeat` for the between-run spread.

    python3 bench/nps13.py A.py B.py --threads 4 --repeat 3

**`--cpu` IS THREAD-AWARE, AND HAS TO BE** (fixed 2026-07-27, found by the
acceptance run itself). The affinity set is `cpu .. cpu+threads-1`, not `{cpu}`.
The first acceptance attempt used `--threads 4 --cpu 2`, which pinned all four
engine threads to ONE core to timeshare it: within a run A swung 0.528s..1.942s
(3.7x), and three repeats of a NULL -- the same binary against itself -- read
-9.99%, +1.51%, -11.79%, a **13.30 percentage-point** between-run floor. None of
that was SMP noise or a busy box; it was four threads on one core. The header
now prints the cpu RANGE so the mistake cannot look correct again.

**THE SEARCH MUST DOMINATE THE MEASUREMENT.** d13 is sized for ONE thread. At
Threads=4 the same tree finishes several times sooner -- 0.35s on an idle
4-core pin -- and at that length the ratio measures thread spin-up, the TT reset
and WHICH HELPER WON THE RACE, not the engine. Measured on a null with the
affinity fix in place: individual ratios spanned 0.40..2.42 (6x) and three
repeats read +5.29%, +28.72%, +22.93% -- a 23-point floor, WORSE than the
contended run it replaced. Raising the depth is the fix; more rounds only
median the same stochastic quantity. The instrument now refuses to be read
quietly below ~1s per search and prints the deeper command to run.

Rule of thumb: **+3 plies per doubling of threads.** d13 at 1 thread, ~d16 at
4. `--repeat 3` is mandatory in this mode, and the between-run spread, not the
sign test, is the floor.

**HOW MANY ROUNDS Threads>1 ACTUALLY NEEDS -- measured 2026-07-27, and it is
not 16.** Six null runs on an idle, correctly-pinned 4-core set at d16:

    16 rounds   +1.71%  +3.04%  +3.08%     3-run spread 1.38pp
    32 rounds   +0.63%  -6.41%  -3.30%     3-run spread 7.04pp
    pooled      mean -0.21%,  SD 3.84pp

Two things follow, and the second is the one that costs money.

1. **There is NO bias.** The null is centred on 1.00 (mean -0.21%). An earlier
   read of "+2.6% systematic" came from three same-sign 16-round runs and was
   simply wrong.
2. **A 3-run spread does NOT estimate the floor.** The same instrument on the
   same work produced 1.38pp and then 7.04pp. Reading `--repeat 3`'s range as
   the resolution floor is safe at Threads=1, where the underlying variance is
   small; at Threads>1 it is a 3-sample range of a wide distribution and will
   happily report a floor 5x too good. Use the SD across repeats, and do not
   trust a floor derived from three numbers.

**MORE ROUNDS DO NOT HELP -- MEASURED, and it refutes the obvious model.**
On an idle box with verified 1-core-per-CPU topology (lscpu CPU n -> CORE n,
so the pin really was four distinct physical cores):

    16 rounds   between-run spread   1.38pp
    32 rounds                        7.04pp
    150 rounds                      11.29pp   (+7.77%, -3.52%, +0.06%)

The floor gets WORSE with more rounds, which is impossible if the per-round
ratios were i.i.d. -- so they are not. Two things are going on: the ratio
distribution is heavy-tailed (0.332..2.020 on a NULL, a 6x spread, because
which helper first finds the line that ends an iteration is genuinely random),
and the between-run component is dominated by something SLOW relative to a run.
A 150-round run spans 8.4 minutes and is therefore MORE exposed to that drift,
not less. Predicting ~1.5pp from SD ∝ 1/sqrt(n) was wrong.

**SO THE FLOOR IS ~10pp AND ROUND COUNT WILL NOT MOVE IT.** What that costs:

  * The instrument can PROBABLY see a large regression -- but this is a SMOKE,
    not the control. The crippled-helper build (helpers muted) reads **-35.5%**
    over 5 rounds at d13 on a BUSY Mac. That is far outside the floor and the
    direction is right, but it is not the 150-round d16 idle-box run the
    control calls for, and it must not be quoted as one.
  * It CANNOT see the range that matters for candidates. FI-47 S-06 is priced
    +0-15 Elo, i.e. 0-13% of time-to-depth -- inside the floor. **nps13
    --threads cannot decide S-06**, and no round count fixes that.

If the SMP lane needs finer resolution, the design change is to sum time over
a SUITE of positions per round rather than repeating one (the bench's shape),
which would cut relative variance by ~sqrt(#positions). That is a rewrite, not
a parameter, and it is unbuilt.

**Acceptance status: HALF DONE, and the half that is done is the informative
one.** The NULL ran properly on an idle box at 16/32/150 rounds: centred (mean
-0.21%) with a floor of ~10pp that round count does not move. The CRIPPLED
control has only been smoked on a busy Mac (-35.5%, 5 rounds, d13) and still
owes its 150-round d16 idle-box run -- `testing/stage_crippled_helper.sh`
stages it.

That outstanding run is worth doing but it cannot change the verdict, because
the verdict comes from the null: the floor is ~10pp, FI-47 S-06 is priced
0-13% of time-to-depth, so **nps13 --threads cannot decide S-06** whatever the
cripple reads. A confirmed detection would only tell us the instrument is
usable for LARGE regressions, which is the case it is least needed for.

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
# Pin to ONE cpu. On a dual-socket box each fresh subprocess otherwise lands
# wherever the scheduler likes, and a cross-socket A/B pair reads ~8% apart --
# measured 2026-07-24 on a 112-thread Intel Gold 6330: raw NPS was bimodal at
# ~1.53M / ~1.66M, every outlier ratio (0.915 .. 1.075) was a socket mismatch,
# and the same-socket rounds all read 0.994-0.996. Pinning turns that lottery
# into a constant.
cpu = int(sys.argv[5]) if len(sys.argv) > 5 else -1
threads = int(sys.argv[6]) if len(sys.argv) > 6 else 1
pinned = []
if cpu >= 0 and hasattr(os, "sched_setaffinity"):
    try:
        # FI-95 FIX: the affinity set must be as wide as the THREAD COUNT.
        # A one-cpu set at Threads=4 puts all four engine threads on a single
        # core to timeshare it, and time-to-depth becomes a scheduler lottery:
        # the first acceptance run read a 3.7x swing WITHIN a run and a 13.30
        # percentage-point floor BETWEEN runs on a null (same binary both
        # sides). That is not SMP noise, it is four threads on one core.
        avail = sorted(os.sched_getaffinity(0))
        want = [c for c in range(cpu, cpu + max(1, threads)) if c in avail]
        os.sched_setaffinity(0, set(want) or {cpu})
        pinned = want or [cpu]
    except OSError:
        pass                        # not permitted here; fall back to unpinned
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
    # FI-95: threads > 1 switches the QUESTION. At Threads>1 raw NPS counts
    # helper nodes that may be pure duplication, so MORE NPS can mean LESS
    # search -- the quantity that matters is time-to-depth. The child still
    # reports both; the parent decides which one it is ranking.
    eng.smp_workers = threads
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
print("NPS13 " + json.dumps({"nodes": nodes, "sec": dt, "cpus": pinned}))
'''


def measure(engine_path, fen, depth, cpu=-1, threads=1):
    """One fixed-depth search in a FRESH process.

    Returns (nps, nodes, seconds). FI-95: `seconds` is the figure of merit at
    Threads>1 -- time-to-depth -- because NPS there counts helper nodes that
    may be duplicated work."""
    r = subprocess.run([_sys.executable, "-c", _CHILD, _ROOT_, engine_path,
                        fen, str(depth), str(cpu), str(threads)],
                       capture_output=True, text=True, cwd=_ROOT_)
    line = next((l for l in r.stdout.splitlines() if l.startswith("NPS13 ")),
                None)
    if line is None:
        raise RuntimeError(f"{engine_path}: no result\n{r.stdout}\n{r.stderr}")
    d = json.loads(line[len("NPS13 "):])
    if d["sec"] <= 0 or d["nodes"] <= 0:
        raise RuntimeError(f"{engine_path}: degenerate measurement {d}")
    return d["nodes"] / d["sec"], d["nodes"], d["sec"]


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
    ap.add_argument("--cpu", type=int, default=1,
                    help="pin every measurement to this cpu (default 1; -1 "
                         "disables). Load-bearing on multi-socket hosts -- see "
                         "the note in the child. No effect on macOS, which has "
                         "no sched_setaffinity.")
    ap.add_argument("--threads", type=int, default=1,
                    help="FI-95: engine threads per side. >1 switches the "
                         "figure of merit from NPS to TIME-TO-DEPTH, because "
                         "under Lazy SMP raw NPS counts helper nodes that may "
                         "be pure duplication -- MORE NPS can mean LESS "
                         "search. Ratios stay 'B relative to A' and >1.0 still "
                         "means B is better (faster to the same depth).")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat the WHOLE run N times and report the "
                         "between-run spread. The within-run spread and sign "
                         "test do NOT bound run-to-run drift: measured "
                         "2026-07-25, one identical binary read +0.06%% and "
                         "then +0.23%%. Use --repeat 3 before believing any "
                         "effect under ~1%%.")
    a = ap.parse_args()

    for p in (a.engine_a, a.engine_b):
        if not _os.path.isfile(_os.path.join(_ROOT_, p)) and not _os.path.isfile(p):
            _sys.exit(f"engine not found: {p}")

    _what = "TIME-TO-DEPTH" if a.threads > 1 else "NPS"
    print(f"paired d{a.depth} {_what} ratios -- B relative to A")
    print(f"  A = {a.engine_a}")
    print(f"  B = {a.engine_b}")
    # FI-95: the pin must be as wide as the thread count, and it must SAY so.
    # "pinned to cpu 2" at Threads=4 read like a sane setup while actually
    # putting four threads on one core -- the acceptance run's 13.30-point
    # floor. The range is printed so that can never look right again.
    _pin = (f", pinned to cpu {a.cpu}" if a.threads <= 1
            else f", pinned to cpus {a.cpu}-{a.cpu + a.threads - 1}")
    print(f"  {a.rounds} rounds (round 1 discarded), fresh process per search"
          + (_pin if a.cpu >= 0 else "")
          + (f", Threads={a.threads}" if a.threads > 1 else "") + "\n")

    t_start = time.time()
    medians = []
    for run in range(1, a.repeat + 1):
        if a.repeat > 1:
            print(f"  --- run {run}/{a.repeat} ---")
        medians.append(run_once(a))
    if a.repeat > 1:
        drift = max(medians) - min(medians)
        print(f"\n  {a.repeat} independent runs of the SAME pair: "
              + ", ".join(f"{(m - 1) * 100:+.2f}%" for m in medians))
        print(f"  between-run spread {drift * 100:.2f} percentage points -- "
              f"this, NOT the sign test, is the resolution floor.")
        print(f"  Effects smaller than {drift * 100:.2f}% are UNRESOLVED by "
              f"this instrument no matter how good the p-value looks.")
    print(f"\n  total elapsed {time.time() - t_start:.0f}s")


def run_once(a):
    """One complete measurement; returns the median ratio."""
    _what = "TIME-TO-DEPTH" if a.threads > 1 else "NPS"
    ratios, nodes_a, nodes_b, secs = [], set(), set(), []
    t_start = time.time()
    for rnd in range(1, a.rounds + 1):
        # Interleave A,B then B,A on alternate rounds so a monotonic drift in
        # machine speed cannot favour whichever engine always goes first.
        if rnd % 2:
            (nps_a, na, sa), (nps_b, nb, sb) = (
                measure(a.engine_a, a.fen, a.depth, a.cpu, a.threads),
                measure(a.engine_b, a.fen, a.depth, a.cpu, a.threads))
        else:
            (nps_b, nb, sb), (nps_a, na, sa) = (
                measure(a.engine_b, a.fen, a.depth, a.cpu, a.threads),
                measure(a.engine_a, a.fen, a.depth, a.cpu, a.threads))
        nodes_a.add(na)
        nodes_b.add(nb)
        if rnd > 1:
            secs += [sa, sb]
        # FI-95: at Threads>1 rank on TIME-TO-DEPTH (A's seconds over B's, so
        # >1.0 still means "B better"); at Threads=1 the two are equivalent and
        # NPS is kept for continuity with every banked number.
        r = (sa / sb) if a.threads > 1 else (nps_b / nps_a)
        tag = "  (discarded)" if rnd == 1 else ""
        if rnd > 1:
            ratios.append(r)
        if a.threads > 1:
            print(f"  round {rnd:>3}/{a.rounds}  A {sa:6.3f}s  "
                  f"B {sb:6.3f}s  ratio {r:.4f}{tag}", flush=True)
        else:
            print(f"  round {rnd:>3}/{a.rounds}  A {nps_a/1e6:6.3f}M  "
                  f"B {nps_b/1e6:6.3f}M  ratio {r:.4f}{tag}", flush=True)

    med = statistics.median(ratios)
    med_time = statistics.median(secs) if secs else None
    wins = sum(1 for r in ratios if r > 1.0)
    p = sign_test_p(wins, len(ratios))
    print(f"\n  {len(ratios)} paired d{a.depth} {_what} ratios   "
          f"median {med:.4f}  ({(med - 1) * 100:+.2f}%)")
    print(f"  spread {min(ratios):.3f} .. {max(ratios):.3f}   "
          f"B faster in {wins}/{len(ratios)}  (sign test p={p:.3f})")
    print(f"  elapsed {time.time() - t_start:.0f}s")

    # FI-95: a TIME-TO-DEPTH measurement has to be long enough that the SEARCH
    # dominates it. At Threads=4 the tree is finished several times faster than
    # at Threads=1, so the depth that gave a ~1s single-thread search gives a
    # ~0.3s one here -- and at 0.3s the reading is thread spin-up, the TT reset
    # and WHICH HELPER WON THE RACE, not the engine's speed. Measured on an
    # idle 4-core pin: d13 ran 0.35s and individual ratios spanned 0.40..2.42
    # (6x) on a NULL, for a 23-point between-run floor. Raising the depth is
    # the fix; more rounds only medians the same stochastic quantity.
    if a.threads > 1 and med_time is not None and med_time < 1.0:
        want = a.depth + 3
        print(f"\n  !! SEARCHES TOO SHORT: median {med_time:.2f}s per search at "
              f"Threads={a.threads}.\n"
              f"     Below ~1s the ratio measures helper-race outcome and "
              f"startup, not speed.\n"
              f"     Re-run deeper -- the search must dominate the fixed cost:\n"
              f"       python3 bench/nps13.py {a.engine_a} {a.engine_b} "
              f"--threads {a.threads} --depth {want} --repeat {a.repeat}"
              + (f" --cpu {a.cpu}" if a.cpu >= 0 else ""))

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
    elif a.threads > 1:
        print(f"  node counts vary across rounds -- EXPECTED at Threads="
              f"{a.threads} (helpers race the shared TT). That is exactly why "
              f"this mode ranks TIME-TO-DEPTH and not NPS.")
    else:
        print("  !! node counts varied across rounds -- non-deterministic "
              "search (SMP? book? TB?); the ratios are not trustworthy")

    # A wide spread means the MACHINE moved during the run, not the engine.
    # Measured here 2026-07-24: the same pair read 0/16 p=0.000 on a quiet box
    # (spread 0.920..0.986) and 5/16 p=0.210 twenty minutes later while builds
    # were running (spread 0.907..1.121). Without this line the second run
    # silently overturns the first.
    rng = max(ratios) - min(ratios)
    if rng > 0.10:
        if a.threads > 1:
            # A wide WITHIN-run spread is EXPECTED at Threads>1 and says
            # nothing about the host: which helper first finds the line that
            # ends an iteration is genuinely random, so time-to-depth varies
            # ~3x round to round on a fully idle, pinned box. Blaming the
            # machine here sent three consecutive acceptance runs looking for
            # a phantom co-tenant. What bounds a claim in this mode is the
            # BETWEEN-run spread that --repeat reports, not this number.
            print(f"\n  spread {rng:.3f} within the run -- EXPECTED at "
                  f"Threads={a.threads}, not a sign the box is busy.\n"
                  f"     Helper-race variance moves time-to-depth ~3x round to "
                  f"round even when idle.\n"
                  f"     Use the BETWEEN-run spread from --repeat as the floor; "
                  f"this line is not it.")
        else:
            print(f"\n  !! spread {rng:.3f} is WIDE (>0.10) -- this machine was "
                  f"busy during the run.\n     Re-measure on an idle box before "
                  f"trusting either the median or the sign test.")

    if p > 0.05:
        print("\n  VERDICT: not resolved -- the sign test does not exclude "
              "chance. More rounds, or the effect is below this instrument.")
    else:
        print(f"\n  VERDICT: {'B faster' if med > 1 else 'B slower'} by "
              f"{abs(med - 1) * 100:.2f}% (median)")
        if abs(med - 1) < 0.01:
            print("  ...but this is under 1%: the sign test bounds WITHIN-run "
                  "noise only.\n     Re-run with --repeat 3 -- one identical "
                  "binary has read +0.06% and +0.23%\n     in consecutive runs "
                  "on an idle pinned box (2026-07-25).")
    return med


if __name__ == "__main__":
    main()
