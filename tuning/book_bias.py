#!/usr/bin/env python3
"""tuning/book_bias.py -- what an opening book costs, measured from a campaign.

    python3 tuning/book_bias.py --ptnml 324 1248 1957 1167 304
    python3 tuning/book_bias.py NNUE/campaigns/sprt_*.json

Playing every opening TWICE (once per colour) is what makes the pentanomial
work: whatever the opening itself is worth cancels between the two games of a
pair, so the pair variance is smaller than two independent games would be.
The size of that gap IS the book's bias -- Fishtest derives its "RMS bias"
the same way, from pentanomial vs trinomial.

Why it matters: variance decides how many games a verdict costs. A book with
large per-opening bias buys a bigger reduction from pairing, and a book with
none buys nothing. This prints the reduction so the choice of book is a
measured decision rather than a habit.

The Elo figure is a derived quantity and the constant is ours, not Fishtest's
-- treat the VARIANCE RATIO as the solid number and the Elo as indicative.
"""

import argparse
import glob
import json
import math
import os
import sys


def stats(penta, wdl):
    """(ratio, rms_elo, draw_rate, var_pair, var_indep) -- needs BOTH.

    The pentanomial alone cannot give the per-game variance: its middle bucket
    deliberately merges DD with WL, so the trinomial is not recoverable from
    it. W/D/L must be supplied; both are printed by every match.py summary.
    """
    n = float(sum(penta))
    w, d, l = (float(x) for x in wdl)
    g = w + d + l
    if n <= 0 or g <= 0:
        return None
    # observed PAIR variance, pair score on 0..1
    p = [c / n for c in penta]
    vals = (0.0, 0.25, 0.5, 0.75, 1.0)
    mu_pair = sum(pi * v for pi, v in zip(p, vals))
    var_pair = sum(pi * (v - mu_pair) ** 2 for pi, v in zip(p, vals))
    # per-GAME variance from the trinomial, then what two INDEPENDENT games
    # would give as a pair: var_game / 2
    pw, pd, pl = w / g, d / g, l / g
    mu_g = pw + 0.5 * pd
    var_game = (pw * (1 - mu_g) ** 2 + pd * (0.5 - mu_g) ** 2
                + pl * (0.0 - mu_g) ** 2)
    var_indep = var_game / 2.0
    ratio = var_pair / var_indep if var_indep else float("nan")
    removed = max(0.0, var_indep - var_pair)
    rms_elo = math.sqrt(removed) * 4 / math.log(10) * 100
    return ratio, rms_elo, pd, var_pair, var_indep


def report(label, penta, wdl):
    s = stats(penta, wdl)
    if s is None:
        print(f"{label}: need a non-empty pentanomial AND W/D/L"); return
    ratio, rms, p_drw, vp, vi = s
    print(f"{label}")
    print(f"  pairs                 {sum(penta):,}   ptnml {'/'.join(map(str,penta))}")
    print(f"  pair variance         {vp:.6f}")
    print(f"  independent-pair pred {vi:.6f}")
    print(f"  VARIANCE RATIO        {ratio:.3f}   "
          f"({'pairing removes %.0f%%' % ((1-ratio)*100) if ratio < 1 else 'pairing buys nothing'})")
    print(f"  RMS bias (indicative) {rms:.0f} Elo")
    print(f"  draw-ish share        {p_drw*100:.1f}%")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state", nargs="*", help="sprt_*.json campaign state file(s)")
    ap.add_argument("--ptnml", nargs=5, type=int, metavar=("LL","LD","DD_WL","WD","WW"),
                    help="the five buckets directly")
    ap.add_argument("--wdl", nargs=3, type=int, metavar=("W","D","L"),
                    help="engine-1 wins, draws, losses (from the match summary) "
                         "-- REQUIRED: the trinomial cannot be recovered from "
                         "the pentanomial, whose middle bucket merges DD with WL")
    a = ap.parse_args()
    if a.ptnml:
        if not a.wdl:
            ap.error("--ptnml also needs --wdl (see --help)")
        report("--ptnml", a.ptnml, a.wdl)
    paths = []
    for pat in a.state:
        paths.extend(sorted(glob.glob(pat)))
    for p in paths:
        try:
            d = json.load(open(p))
            penta = [int(v) for v in d["penta"]]
            wdl = d.get("wdl")
        except Exception as e:
            print(f"{p}: unreadable ({e})"); continue
        if not wdl:
            print(f"{os.path.basename(p)}: state file carries no W/D/L -- "
                  f"pass --ptnml {' '.join(map(str,penta))} --wdl W D L\n")
            continue
        report(os.path.basename(p), penta, wdl)
    if not a.ptnml and not paths:
        ap.error("give --ptnml or one or more sprt_*.json paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
