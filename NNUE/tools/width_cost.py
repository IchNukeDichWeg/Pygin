#!/usr/bin/env python3
"""Forward-pass cost vs TAIL WIDTH -- the gate before training another net.

    python3 NNUE/tools/width_cost.py            # sweep 32/24/16/12/8
    python3 NNUE/tools/width_cost.py 32 16 8

WHY. The v3 net is SPEED-bound, not eval-bound: the eval is worth ~+50 Elo and
the forward pass gives most of it back -- 19.1% of nodes on arm64, 28.0% on
x86, which is why the same net measures +19.30 on arm64 and +1.67 on x86.
Training a BETTER net improves the half that already works. The lever is a
NARROWER one.

Cost depends only on the tail width, never on the weights, so this needs no
training: generate untrained nets at each width and measure the engine's NPS
against the HCE baseline. If no width gets meaningfully under the x86 deficit,
the direction dies here for free instead of after a training run and an A/B.

Run it on the machine whose deficit you care about -- the numbers are per
microarchitecture, and x86 is the one that decides shippability.
"""
import os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "NNUE"))

CHILD = r'''
import sys, os
sys.path.insert(0, os.getcwd())
import cengine, cuci, io, contextlib
net = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-" else None
if net:
    cengine.Engine.USE_NNUE = True
    cengine.Engine.NNUE_FILE = net
    cengine.Engine.LAZY_NNUE = os.environ.get("WC_LAZY") == "1"
e = cengine.Engine()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cuci.run_bench(e)
line = [l for l in buf.getvalue().splitlines() if "nps" in l][-1]
print(line.split()[-2])
'''


def nps(net, lazy=False):
    env = dict(os.environ, WC_LAZY="1" if lazy else "0")
    r = subprocess.run([sys.executable, "-c", CHILD, net or "-"],
                       capture_output=True, text=True, cwd=ROOT, env=env)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def main():
    widths = [int(a) for a in sys.argv[1:]] or [32, 24, 16, 12, 8]
    import numpy as np
    import verify_c as V
    from nnue_ref import QuantNet

    base = nps(None)
    if base is None:
        print("could not measure the HCE baseline"); return 2
    print(f"HCE baseline: {base:,} nps\n")
    print("  width   nps          deficit vs HCE")
    tmp = tempfile.mkdtemp(prefix="widthcost_")
    for d2 in widths:
        if d2 > 32:
            print(f"  {d2:>5}   skipped -- NN_D2_MAX is 32"); continue
        path = os.path.join(tmp, f"w{d2}.nnue")
        rng = np.random.default_rng(1234)
        QuantNet.from_float(
            rng.normal(0, 0.05, (V.IN_DIM, V.HIDDEN)),
            rng.normal(0, 0.2, V.HIDDEN),
            rng.normal(0, 0.3, (d2, 2 * V.HIDDEN + V.THREAT_DIM)),
            rng.normal(0, 0.2, d2),
            rng.normal(0, 0.3, (V.D3, d2)), rng.normal(0, 0.2, V.D3),
            rng.normal(0, 0.3, V.D3), 0.05).save(path)
        n = nps(path)
        if n is None:
            print(f"  {d2:>5}   FAILED to load/run"); continue
        print(f"  {d2:>5}   {n:>10,}   {100*(1-n/base):>6.1f}%")
    print("\nThe v3 net is width 32. Compare the 32 row against the measured"
          "\ndeficits (19.1% arm64 / 28.0% x86) to sanity-check the tool.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
