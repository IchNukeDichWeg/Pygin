#!/usr/bin/env python3
"""FI-102: `instr_bench` -- a one-sided ABANDON gate on retired instructions.

    python3 bench/instr_bench.py <A.py> <B.py> [--rounds 5] [--depth 13]
                                 [--cpu N] [--claim 0.8]
    python3 bench/instr_bench.py --selftest        # runs anywhere

    LINUX ONLY for a real measurement: it needs `perf stat`. On macOS the
    preflight refuses with the exact reason instead of reporting a number.

WHY. `bench/nps13.py`'s resolution floor is ~0.2 percentage points between
runs. FI-72 was built, measured and reverted purely because its claim (<=0.3%)
sat under that floor -- real work spent to learn nothing. Retired instructions
are near-deterministic and resolve far below 0.1%, so a candidate that does not
move the instruction count CANNOT be delivering the NPS it claims, and can be
abandoned before it is benched.

**IT CAN ONLY ABANDON, NEVER CONFIRM.** Retired instructions are not wall
clock. A change can cut instructions and lose time (worse locality, a longer
dependency chain, a mispredicted branch that used to be free), and it can keep
instructions flat and win time. So:

    instructions/node did NOT fall  +  a speed claim  ->  ABANDON, don't bench
    instructions/node fell          ->  says NOTHING. Go measure it on nps13.

Anyone reading a fall here as a confirmation has misused the tool. The output
says so on every run, deliberately.

WHY IT PAIRS WITH THE BENCH. The house bench is a FIXED node count
(1,461,732 at d11x6), and a node-identical change keeps it exactly. When the
node counts match, instructions-per-node is just total instructions, and the
comparison is as clean as the counter is.

STATUS 2026-07-27: the parser, the normalization and the decision rule are
gated by --selftest and pass. **The two acceptance controls the entry asks for
are NOT run** -- a known deletion (FI-91) must show fewer instructions per
node, and a known node-identical no-op must show none -- because this Mac has
no `perf` and neither control can be executed here at all. They are the first
thing to run on the next Linux box, before any verdict from this tool is
believed. Not claiming them is the point; FI-95 shipped the same way.
"""
import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nps13 import _CHILD, _ROOT_, DEFAULT_FEN     # noqa: E402  (one child impl)

PERF_EVENT = "instructions:u"


def perf_preflight():
    """(ok, reason). Never guess: a missing counter must not read as 0."""
    if platform.system() != "Linux":
        return False, (f"{platform.system()} has no `perf`. This gate is Linux "
                       f"only -- run it on the A/B box, not here.")
    if shutil.which("perf") is None:
        return False, ("`perf` not on PATH (apt install linux-tools-common "
                       "linux-tools-$(uname -r))")
    try:
        with open("/proc/sys/kernel/perf_event_paranoid") as fh:
            par = int(fh.read().strip())
    except OSError:
        par = None
    if par is not None and par > 2:
        return False, (f"perf_event_paranoid={par} blocks user counters "
                       f"(sudo sysctl kernel.perf_event_paranoid=2)")
    probe = subprocess.run(["perf", "stat", "-x,", "-e", PERF_EVENT,
                            "--", "true"], capture_output=True, text=True)
    if parse_perf(probe.stderr) is None:
        return False, (f"`perf stat -e {PERF_EVENT}` produced no count "
                       f"(virtualised host without PMU access?)")
    return True, "ok"


def parse_perf(text):
    """Retired instructions from `perf stat -x,` output, or None.

    The CSV form is `<count>,<unit>,<event>,...`, and a counter that could not
    be read prints `<not counted>` or `<not supported>` in the count field.
    Those must come back as None -- a 0 here would silently become a 100%
    instruction reduction, i.e. the strongest possible ABANDON verdict from a
    measurement that never happened.
    """
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and PERF_EVENT.split(":")[0] in parts[2]:
            try:
                return int(parts[0])
            except ValueError:
                return None
    return None


def measure_instructions(engine_path, fen, depth, cpu=-1):
    """(instructions, nodes) for one fixed-depth search in a FRESH process."""
    cmd = ["perf", "stat", "-x,", "-e", PERF_EVENT, "--",
           sys.executable, "-c", _CHILD, _ROOT_, engine_path, fen,
           str(depth), str(cpu), "1"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=_ROOT_)
    line = next((l for l in r.stdout.splitlines() if l.startswith("NPS13 ")),
                None)
    if line is None:
        raise RuntimeError(f"{engine_path}: no search result\n{r.stderr}")
    nodes = json.loads(line[len("NPS13 "):])["nodes"]
    instr = parse_perf(r.stderr)
    if instr is None or nodes <= 0:
        raise RuntimeError(f"{engine_path}: no instruction count "
                           f"(nodes={nodes})\n{r.stderr}")
    return instr, nodes


def verdict(ratio, claim_pct):
    """One-sided rule. `ratio` is B's instructions per node over A's.

    A claimed speedup of X% needs the work to fall by roughly X% as well; the
    gate fires only when the instruction count did not move DOWN at all, which
    is the unambiguous case. Anything in between is left to nps13 on purpose --
    this tool exists to save benches, not to arbitrate close ones.
    """
    delta = (ratio - 1.0) * 100.0
    if claim_pct is None:
        return None, f"instructions/node {delta:+.3f}% (no claim given)"
    if delta >= 0.0:
        return "ABANDON", (
            f"instructions/node {delta:+.3f}% -- the candidate claims "
            f"{claim_pct:+.2f}% NPS but does not retire fewer instructions "
            f"per node. It cannot be delivering that speed. Do not bench it.")
    return "CONTINUE", (
        f"instructions/node {delta:+.3f}% -- work really did fall. This "
        f"CONFIRMS NOTHING about wall clock; measure it on nps13.")


def selftest():
    csv = ("# started on ...\n"
           "\n"
           "12345678901,,instructions:u,1000000,100.00,,\n")
    assert parse_perf(csv) == 12345678901, parse_perf(csv)
    # THE CONTROL: an unread counter must be None, never 0. A 0 would read as
    # a 100% instruction reduction -- the strongest ABANDON this tool can
    # emit -- from a measurement that did not happen.
    for bad in ("<not counted>,,instructions:u,0,0.00,,\n",
                "<not supported>,,instructions:u,0,0.00,,\n",
                "12345,,cycles:u,1000,100.00,,\n",
                "", "garbage\n"):
        assert parse_perf(bad) is None, bad

    assert verdict(1.0000, 0.8)[0] == "ABANDON"      # flat work, speed claimed
    assert verdict(1.0200, 0.8)[0] == "ABANDON"      # MORE work, speed claimed
    assert verdict(0.9920, 0.8)[0] == "CONTINUE"
    assert verdict(0.9920, None)[0] is None
    assert "CONFIRMS NOTHING" in verdict(0.9920, 0.8)[1]

    ok, why = perf_preflight()
    assert isinstance(ok, bool) and why
    print(f"instr_bench selftest: OK  (perf available here: {ok} -- {why})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("engine_a", nargs="?")
    ap.add_argument("engine_b", nargs="?")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--depth", type=int, default=13)
    ap.add_argument("--cpu", type=int, default=-1)
    ap.add_argument("--claim", type=float, default=None,
                    help="the NPS gain the candidate claims, in percent")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest or not a.engine_a:
        selftest()
        return 0

    ok, why = perf_preflight()
    if not ok:
        print(f"instr_bench: cannot measure -- {why}", file=sys.stderr)
        return 2

    fen = DEFAULT_FEN
    ratios = []
    for rnd in range(a.rounds + 1):          # round 1 discarded, as in nps13
        ia, na = measure_instructions(a.engine_a, fen, a.depth, a.cpu)
        ib, nb = measure_instructions(a.engine_b, fen, a.depth, a.cpu)
        r = (ib / nb) / (ia / na)
        if rnd:
            ratios.append(r)
        print(f"  round {rnd:>2}{' (discarded)' if not rnd else ''}: "
              f"A {ia/na:,.1f} instr/node ({na:,} nodes)   "
              f"B {ib/nb:,.1f} ({nb:,})   ratio {r:.5f}")
        if na != nb and rnd:
            print(f"    note: node counts differ ({na:,} vs {nb:,}) -- this is "
                  f"NOT a node-identical pair, so the per-node normalization "
                  f"is doing real work here")
    med = statistics.median(ratios)
    v, msg = verdict(med, a.claim)
    print(f"\nmedian instructions/node ratio (B/A): {med:.5f}")
    print(f"{v + ': ' if v else ''}{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
