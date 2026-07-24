"""Convert Texel held-out loss into Elo, using a pair with a KNOWN A/B result.

Why this exists
---------------
The project's standing rule was "under ~1% held-out is no evidence". Every
number behind that rule was measured BEFORE FB-43, on a split where ~97% of
games straddled train/val, so those percentages were inflated by an unknown
factor. Applied to the clean split, the same rule rejects everything.

This measures the conversion directly: take two shipped versions whose A/B
Elo is known, evaluate BOTH on the current clean split, and divide.

TWO POINTS NOW, AND THEY DO NOT LIE ON A LINE:

    v53 -> v54 PST retune   +0.196% clean  ->  +31.20 +/- 5.6 Elo  (11,668 games)
    2026-07-24 PST retune   +0.085% clean  ->   -4.92 +/- 7.4 Elo   (8,474 games)

The first point alone suggested ~159 Elo per 1% clean, which predicted +13.5
for the second. It measured NEGATIVE -- the 95% CI (-12.3 .. +2.5) excludes
the prediction outright. So the ratio is NOT a line through the origin, and
extrapolating it downward is exactly the mistake the old ">= 1%" rule existed
to prevent.

WHAT TO CONCLUDE: a clean held-out gain only carries Elo once it is LARGE.
+0.196% converted; +0.085% -- less than half of it -- converted to nothing.
Treat anything under ~0.15% clean as unproven, and do not price a candidate
in Elo from its loss at all. The loss picks which candidate to test; only the
A/B says what it is worth.

RE-RUN THIS when a new eval release lands an A/B result, and add the point
above rather than replacing it. A third point is what turns two contradictory
readings into a usable curve.

    python3 calibrate_loss_elo.py                       # v53 -> v54 (default)
    python3 calibrate_loss_elo.py <old_eval.py> <elo>   # any other pair
"""
# Path shim: this script moved into a subfolder on 2026-07-24 but
# still imports the engine modules that live at the repo root.
import os as _os, sys as _sys
_ROOT_ = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_ROOT_, _os.path.join(_ROOT_, "lib")]
import importlib.util
import multiprocessing as mp
import sys

import numpy as np

import texel
import engine as E

DEFAULT_OLD = "Old Engine/53/engine_eval.py"
DEFAULT_ELO = 31.20          # v53 -> v54, 11,668 games, GSPRT[0,2] ACCEPT
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
POSITIONS = _os.path.join(_ROOT, "texel_positions.npy")
K = 1.1                      # the K a full tune fits on this corpus


def _load(path, name):
    sp = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def main():
    old_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OLD
    known_elo = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ELO
    old = _load(old_path, "old_eval")

    arr = np.load(POSITIONS, mmap_mode="r")
    ntrain = int(len(arr) * 0.8)
    g = arr["game"]
    while 0 < ntrain < len(g) and g[ntrain] == g[ntrain - 1]:   # FB-43
        ntrain -= 1
    print(f"clean split: {ntrain:,} train / {len(arr) - ntrain:,} val")

    texel.PARAMS = texel.PARAMS + texel.pst_params()   # PSTs are in scope
    W = max(1, mp.cpu_count() - 1)

    with mp.Pool(W, initializer=texel._worker_init,
                 initargs=(POSITIONS, W, ntrain, ())) as pool:
        new_vec = texel.baseline_vector(E.Engine())
        l_new = texel._loss(pool, W, new_vec, K, val=True)
        print(f"shipped held-out : {l_new:.6f}")

        for label, attr, key in texel.PARAMS:
            if hasattr(old.Engine, attr):
                src = getattr(old.Engine, attr)
                texel._set(E.Engine, attr, key,
                           src[key] if key is not None else src)
        old_vec = texel.baseline_vector(E.Engine())
        moved = sum(1 for a, b in zip(old_vec, new_vec) if a != b)
        l_old = texel._loss(pool, W, old_vec, K, val=True)
        print(f"{old_path} held-out: {l_old:.6f}   ({moved} params differ)")

    gain = (l_old - l_new) / l_old * 100
    if gain <= 0:
        print("\nno clean gain measurable -- cannot calibrate from this pair")
        return
    per_pct = known_elo / gain
    print(f"\n{gain:+.3f}% clean held-out  ==  {known_elo:+.2f} Elo (measured)")
    print(f"   (ratio for THIS pair: ~{per_pct:.0f} Elo per 1% clean)")
    print("   NOTE: the ratio does not extrapolate downward -- a +0.085%\n"
          "   candidate measured -4.92 Elo over 8,474 games. See the module\n"
          "   docstring; use the loss to RANK candidates, not to price them.")


if __name__ == "__main__":
    main()
