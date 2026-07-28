#!/usr/bin/env python3
"""NNUE/tools/lazy_probe.py -- could a cheap bound skip the net, and how often?

    python3 NNUE/tools/lazy_probe.py --net NNUE/nets/nnue_v3_d16_2880b51afe28.nnue

Answers the ONE question that decides whether lazy NNUE eval is worth
building, before any of it is built: at every position where the net actually
ran, would a free bound have already answered the search's question?

The bound is FI-42's incremental tapered material+PST accumulator -- already
maintained by apply_move, so reading it costs a multiply and a divide. That
matters more than its accuracy: the thing it races is 96% one matmul, so a
bound that is not free buys nothing.

WHAT IS REPORTED, per margin M:

  skip%    share of NN evals where the bound alone was decisive -- cheap is
           more than M past a window edge, so the search could have taken it
           and never called the net
  wrong%   share of those skips where the NET disagreed about which side of
           the window it fell on. These are the ones that would change the
           search's decision, i.e. the actual cost of being lazy
  recover  skip% x the measured 21.4% NPS the net costs on this box, in Elo
           at the repo's 1.16 Elo/%NPS -- the OPTIMISTIC ceiling, since it
           prices the saving and charges nothing for wrong%

Read them together. A margin with a big skip% and a non-zero wrong% is not a
win; a margin with 2% skip is not worth writing. What you want to see is a
margin where wrong% is ~0 and skip% is still double digits. If no such margin
exists, the idea is dead and this run cost an afternoon instead of a week.

The instrumented .so is built here, NOT by setup.sh: the probe is compiled
out of the shipped build entirely (-DCS_LAZY_PROBE), because a dormant branch
on the hot path has already cost this engine 0.5% NPS once. It also gets its
own FILENAME -- dyld resolves by name, and a same-named second image would be
silently ignored in favour of the one already mapped.
"""

import argparse
import ctypes
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, REPO)

import chess                                                     # noqa: E402

SO_NAME = "csearch_lazyprobe.so"      # MUST differ from csearch.so -- see above

# The bench suite: a spread of phases, not a tactical set. Skip rate is a
# property of the tree's shape, so it has to be sampled over ordinary
# positions rather than the sharp ones a mate suite would give.
POSITIONS = [
    ("start",     chess.STARTING_FEN),
    ("kiwipete",  "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("midgame",   "r2q1rk1/pp2ppbp/2np1np1/2p5/4P3/2NP1N1P/PPP1BPP1/R1BQ1RK1 w - - 0 1"),
    ("open",      "r1bq1rk1/pp2bppp/2n1pn2/3p4/3P4/2NBPN2/PP3PPP/R1BQ1RK1 w - - 0 1"),
    ("imbalance", "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/3P1N2/PPP2PPP/RNBQK2R w KQkq - 0 1"),
    ("endgame",   "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"),
    ("pawn eg",   "8/5ppp/8/5PPP/8/6k1/8/6K1 w - - 0 1"),
    ("rook eg",   "8/8/8/4k3/8/8/4R3/4K3 w - - 0 1"),
]


def build(cc="cc"):
    """Compile csearch.c with the probe enabled, into its own .so."""
    out = os.path.join(REPO, SO_NAME)
    src = [os.path.join(REPO, f) for f in ("csearch.c", "eval_c.c", "Constants.c")]
    newest = max(os.path.getmtime(f) for f in src
                 + [os.path.join(REPO, "NNUE", "nnue.c")])
    if os.path.exists(out) and os.path.getmtime(out) > newest:
        print(f"{SO_NAME} up to date")
        return out
    tune = "-mcpu=native" if os.uname().machine == "arm64" else "-march=native"
    cmd = [cc, "-O3", tune, "-shared", "-fPIC", "-I.", "-w",
           "-DCS_LAZY_PROBE", "-o", out] + src + ["-lm", "-lpthread"]
    print(f"building {SO_NAME} ...", flush=True)
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"build failed:\n{p.stdout}{p.stderr}")
    return out


def collect(net, depth, cap):
    """Search every position with the net armed; return the raw samples."""
    import cengine

    class ProbeEngine(cengine.Engine):
        CSEARCH_SO = SO_NAME
        USE_NNUE = True
        NNUE_FILE = os.path.abspath(net)

    eng = ProbeEngine()
    eng.use_book = eng.use_tb = False
    eng.smp_workers = 1          # the probe buffer is not thread-safe, and a
                                 # single thread is what makes the sample a
                                 # clean picture of one tree
    lib = eng._lib
    if not hasattr(lib, "set_lazy_probe"):
        sys.exit(f"{SO_NAME} has no probe -- stale build?")
    lib.lazy_probe_dump.argtypes = [ctypes.POINTER(ctypes.c_int32),
                                    ctypes.c_long]
    lib.lazy_probe_dump.restype = ctypes.c_long

    buf = (ctypes.c_int32 * (4 * cap))()
    rows = []
    for name, fen in POSITIONS:
        lib.cs_tt_reset()
        lib.set_lazy_probe(1)
        t0 = time.time()
        eng.get_best_move(chess.Board(fen), depth)
        n = lib.lazy_probe_dump(buf, cap)
        lib.set_lazy_probe(0)
        rows += [tuple(buf[4 * i + k] for k in range(4)) for i in range(n)]
        print(f"  {name:<10} {n:>9,} NN evals   {time.time() - t0:5.1f}s",
              flush=True)
    return rows


def report(rows, nps_cost):
    n = len(rows)
    print(f"\n{n:,} NN static evals sampled\n")

    err = sorted(abs(nn - ch) for nn, ch, _a, _b in rows)
    def pct(p):
        return err[min(len(err) - 1, int(p / 100 * len(err)))]
    print("how wrong is the free bound (|net - cheap|, centipawns):")
    print(f"  median {pct(50)}   p90 {pct(90)}   p99 {pct(99)}   "
          f"max {err[-1]}")
    print("  -> the margin has to cover this, and every cp of margin is "
          "skip rate you give up.\n")

    print(f"{'margin':>7}{'skip%':>9}{'wrong%':>9}{'NPS back':>10}"
          f"{'Elo (ceiling)':>15}")
    for M in (0, 25, 50, 75, 100, 150, 200, 300, 500):
        skip = wrong = 0
        for nn, ch, a, b in rows:
            # The search only needs to know which side of the window it is on.
            # A skip is safe when the NET lands on that same side.
            if ch - M >= b:
                skip += 1
                if nn < b:
                    wrong += 1
            elif ch + M <= a:
                skip += 1
                if nn > a:
                    wrong += 1
        s = 100 * skip / n
        w = 100 * wrong / max(skip, 1)
        back = s / 100 * nps_cost
        print(f"{M:>7}{s:>8.1f}%{w:>8.2f}%{back:>9.1f}%{1.16 * back:>+14.1f}")

    print(f"\nElo column = skip% x {nps_cost:.1f}% (this net's NPS cost) x 1.16 "
          f"Elo/%NPS.\nIt is a CEILING: it prices the saving and charges "
          f"nothing for wrong%.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--net", required=True)
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--cap", type=int, default=4_000_000,
                    help="max samples kept per position (default 4,000,000)")
    ap.add_argument("--nps-cost", type=float, default=21.4,
                    help="%% of NPS the net costs on THIS box (default 21.4, "
                         "the Mac; the x86 A/B box measured 30.6)")
    ap.add_argument("--cc", default=os.environ.get("CC", "cc"))
    args = ap.parse_args()

    if not os.path.exists(args.net):
        sys.exit(f"lazy_probe: no such net: {args.net}")
    build(args.cc)
    rows = collect(args.net, args.depth, args.cap)
    if not rows:
        sys.exit("lazy_probe: no samples -- did the net arm?")
    report(rows, args.nps_cost)


if __name__ == "__main__":
    main()
