#!/usr/bin/env python3
"""scripts/pool_penta.py -- pool a split campaign into one result.

    python3 scripts/pool_penta.py ab_deadtag192_0.log ab_deadtag192_2500.log

Splitting a run across boxes only pays if the halves are added back up
correctly. Each half alone carries roughly sqrt(2) times the error bar of
the whole and is not the result; this sums the pentanomial counts and
reports the pooled Elo (via match.py's own pentanomial estimator, so the
number matches what a single undivided run would have printed) plus the
post-hoc LLR.

POOLING IS ONLY VALID IF THE HALVES ARE THE SAME INSTRUMENT: same clock,
same book, same seed, same worker density, and DISJOINT openings. The script
checks what it can see in the logs and refuses on a mismatch -- density it
cannot check, so that one is on you.
"""
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "testing")]
import sprt                                              # noqa: E402

_sp = importlib.util.spec_from_file_location("m", os.path.join(ROOT, "match.py"))
_m = importlib.util.module_from_spec(_sp)
sys.modules["m"] = _m
try:
    _sp.loader.exec_module(_m)
except SystemExit:
    pass


def parse(path):
    t = open(path, errors="ignore").read()
    pen = re.findall(r"^Ptnml:\s*([0-9,\s]+)$", t, re.M)
    if not pen:
        sys.exit(f"{path}: no Ptnml line -- did the run finish?")
    mode = re.findall(r"^Mode:\s*(.+?)\s*\|", t, re.M) or \
        re.findall(r"^Mode:\s*(.+)$", t, re.M)
    ops = re.findall(r"positions \[(\d+), (\d+)\)", t)
    return {
        "path": os.path.basename(path),
        "penta": [int(x) for x in pen[-1].replace(",", " ").split()],
        "mode": (mode[-1].strip() if mode else "?"),
        "span": (int(ops[-1][0]), int(ops[-1][1])) if ops else None,
    }


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    runs = [parse(p) for p in sys.argv[1:]]

    modes = {r["mode"] for r in runs}
    if len(modes) != 1:
        sys.exit(f"REFUSING: different instruments, cannot pool -- {modes}")
    spans = [r["span"] for r in runs if r["span"]]
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a, b = spans[i], spans[j]
            if a[0] < b[1] and b[0] < a[1]:
                sys.exit(f"REFUSING: opening ranges overlap, {a} and {b} -- "
                         "the same games would be counted twice")

    print(f"instrument: {runs[0]['mode']}")
    for r in runs:
        print(f"  {r['path']:34s} pairs {sum(r['penta']):6,}  "
              f"openings {r['span']}  ptnml {r['penta']}")

    tot = [sum(r["penta"][i] for r in runs) for i in range(5)]
    pairs = sum(tot)
    games = pairs * 2
    score = sum(c * v for c, v in
                zip([x / pairs for x in tot], (0.0, .25, .5, .75, 1.0)))
    e, margin = _m.elo(score, games, penta=tot)
    r = sprt.evaluate(tot, elo0=0.0, elo1=4.0, model="normalized")

    print(f"\nPOOLED  {games:,} games / {pairs:,} pairs")
    print(f"  Ptnml:  {tot[0]:,} / {tot[1]:,} / {tot[2]:,} / {tot[3]:,} / {tot[4]:,}")
    print(f"  Score:  {100*score:.2f}%")
    print(f"  Elo:    {e:+.2f} +/- {margin:.1f}   "
          f"(range {e-margin:+.1f} / {e+margin:+.1f})")
    print(f"  LLR:    {r['llr']:+.3f}  [0,4] normalized, bounds "
          f"{r['lower']:+.3f} / {r['upper']:+.3f}")
    if e - margin > 0:
        print("\n  The interval CLEARS ZERO -- a real improvement at this TC.")
    elif e + margin < 0:
        print("\n  The interval is entirely BELOW ZERO -- a real regression.")
    else:
        print("\n  The interval TOUCHES ZERO. Not confirmed: this needs more "
              "games,\n  and calling it a win here would be the mistake.")


if __name__ == "__main__":
    main()
