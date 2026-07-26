#!/usr/bin/env python3
"""NNUE/verify.py -- run every net-acceptance gate and print one verdict.

    python3 NNUE/verify.py --nnue NNUE/nets/nnue_v1_8cc4d9d6caeb.nnue

Wraps the checks that must pass before a freshly trained net is armed for a
screen, in the order they are worth running (cheapest and most diagnostic
first), and reports selftest.py-style PASS/FAIL lines plus an exit code:
0 = every gate passed, 1 = at least one failed.

    forward     C forward == numpy reference          HARD GATE
    increment   incremental accumulator == refresh    HARD GATE
    selftest    NNUE unit checks (oracle, mates, draws, fortress)
    smoke       100 self-play games: no crash, legal play, no leak
    nps         net ON vs OFF throughput              REPORT ONLY

Each gate runs as a SUBPROCESS. cengine's FB-04 rule is one process, one
engine config -- and these gates arm the net differently from each other, so
sharing an interpreter would silently reuse the first configuration. It also
means a segfault in one gate is reported as a failure instead of taking the
whole run down.

nps never fails the run. It prices the net (typically -40% to -60% of HCE
throughput) and the --nodes screen already charges that cost by scaling each
side's node budget, so it is a number to KNOW, not a bar to clear.
"""

import argparse
import os
import subprocess
import sys
import time

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        fails.append(label)


def run(label, argv, ok_when, skip_codes=(), quiet=False):
    """Run a gate; ok_when(returncode, output) -> (ok, detail)."""
    t0 = time.time()
    print(f"\n{label}", flush=True)
    p = subprocess.run([sys.executable] + argv, cwd=REPO_DIR,
                       capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode in skip_codes:
        print(f"  SKIP  {label}  ({out.strip().splitlines()[-1:] or ['']}[0])")
        return
    if not quiet:
        for line in out.strip().splitlines()[-3:]:
            print(f"    | {line}")
    ok, detail = ok_when(p.returncode, out)
    check(label, ok, f"{detail}, {time.time() - t0:.0f}s" if detail
          else f"{time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nnue", required=True,
                    help="the .nnue to accept (the hashed filename train.py "
                         "printed)")
    ap.add_argument("--positions", type=int, default=100_000,
                    help="forward gate sample (default 100,000)")
    ap.add_argument("--pushes", type=int, default=1_000_000,
                    help="increment gate pushes (default 1,000,000)")
    ap.add_argument("--games", type=int, default=100,
                    help="self-play smoke games (default 100)")
    ap.add_argument("--nodes", type=int, default=3000,
                    help="nodes per move in the smoke (default 3000)")
    ap.add_argument("--quick", action="store_true",
                    help="10x smaller forward/increment/smoke -- a plumbing "
                         "check, NOT an acceptance run")
    args = ap.parse_args()

    net = os.path.abspath(args.nnue)
    if not os.path.exists(net):
        sys.exit(f"verify: no such net: {args.nnue}")
    if args.quick:
        args.positions //= 10
        args.pushes //= 10
        args.games //= 10

    print(f"verifying {os.path.relpath(net, REPO_DIR)} "
          f"({os.path.getsize(net) / 1e6:.1f} MB)")

    run("forward gate (C == numpy reference)",
        ["NNUE/verify_c.py", "forward", "--positions", str(args.positions),
         "--net", net],
        lambda rc, out: (rc == 0 and "0 eval mismatches" in out
                         and "0 feature-set mismatches" in out,
                         f"{args.positions:,} positions"))

    run("increment gate (accumulator == full refresh)",
        ["NNUE/verify_c.py", "increment", "--pushes", str(args.pushes),
         "--net", net],
        lambda rc, out: (rc == 0 and "0 mismatches" in out,
                         f"{args.pushes:,} pushes"))

    run("NNUE selftest (oracle, mates, draws, fortress)",
        ["NNUE/selftest_nnue.py", net],      # the net UNDER TEST, not toy.nnue
        lambda rc, out: (rc == 0, "all checks"),
        skip_codes=(42,))

    run(f"self-play smoke ({args.games} games)",
        ["NNUE/selfplay_smoke.py", "--games", str(args.games),
         "--nodes", str(args.nodes), "--net", net],   # net UNDER TEST
        lambda rc, out: (rc == 0 and "smoke OK" in out, "no crash/leak"))

    # Report only -- deliberately not a gate. See the module docstring.
    print("\nnet cost (report only, never fails the run)", flush=True)
    p = subprocess.run([sys.executable, "NNUE/verify_c.py", "nps",
                        "--net", net], cwd=REPO_DIR,
                       capture_output=True, text=True)
    off = on = None
    for line in (p.stdout or "").splitlines():
        if line.startswith("off:"):
            off = float(line.split()[1].replace(",", ""))
        elif line.startswith("on:"):
            on = float(line.split()[1].replace(",", ""))
    if off and on:
        print(f"  INFO  nps off {off/1e6:.2f}M -> on {on/1e6:.2f}M "
              f"({100 * (on / off - 1):+.1f}%)")
    else:
        print(f"  INFO  nps gate did not report (rc={p.returncode})")

    print()
    if fails:
        print(f"NNUE verify FAILED: {', '.join(fails)}")
        return 1
    print(f"NNUE verify OK -- {os.path.basename(net)} is safe to arm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
