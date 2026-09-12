#!/usr/bin/env python3
"""scripts/rating_ladder.py -- place this engine on a published rating scale.

    python3 scripts/rating_ladder.py --check opponents/anchor_foo.py
    python3 scripts/rating_ladder.py --anchors opponents/anchor_*.py \
        --games 2000 --tc 10+0.1 --workers 48

WHAT THIS ANSWERS, AND WHAT IT DOES NOT. Each anchor is an engine with a
published rating, so a match against it estimates where we sit on THAT list's
scale: ours = anchor + measured difference. That is a PLACEMENT. It does not
decide whether a change ships -- the A/B ladder does that, and the two must not
be mixed: an anchor moves whenever the list updates or the binary changes, and a
yardstick that moves cannot measure progress.

UCI_Elo IS NOT A RATING, which is why this exists. Stockfish weakens itself by
choosing worse moves and the cap is calibrated against nothing. A CCRL-rated
opponent is anchored to real games against a real pool.

CONDITIONS ARE PART OF THE RESULT. The rating lists run their own hardware,
book, opening set and time control, and every one of those differs here. The
JSON this writes records our time control, worker count, opening book and each
anchor's rating WITH the list and the date it was read, so the number is always
reported as "on that scale, under these conditions".

POOLING. Anchors from the SAME list are pooled by inverse variance (a tighter
estimate counts for more). Anchors from different lists are never pooled --
different pools, different scales -- so they are reported as separate rows.

BRACKETING. An opponent hundreds of points away decides nearly every game the
same way, so its error bar barely shrinks with games played. The pooled line
prints each anchor's |difference|; anything past ~200 points is noted as weak
evidence rather than silently averaged in.
"""

import argparse
import glob
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCORE_RE = re.compile(r"Engine 1 score:\s*([\d.]+)/(\d+)\s*\(([\d.]+)%\)\s*=>\s*"
                      r"([+-][\d.]+)\s*\+/-\s*([\d.]+)\s*Elo")


def load_anchor(path):
    spec = importlib.util.spec_from_file_location(
        "anchor_" + os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check(path):
    """Handshake, name, one move -- verify an anchor before trusting it."""
    import chess
    import uci_engine
    m = load_anchor(path)
    name = uci_engine.id_name(getattr(m, "BINARY", None))
    print(f"{os.path.basename(path)}")
    print(f"  id name     {name}")
    print(f"  rating      {getattr(m, 'RATING', None)}  "
          f"[{getattr(m, 'RATING_LIST', '?')}]")
    e = m.Engine()
    t0 = time.time()
    mv = e.get_best_move_timed(chess.Board(), 0.3)
    print(f"  first move  {mv}  depth {e.last_depth}  "
          f"nodes {e.nodes_searched:,}  ({time.time() - t0:.2f}s)")
    ok = mv is not None and name
    print(f"  {'OK' if ok else 'NOT USABLE'}")
    return 0 if ok else 1


def play(anchor_path, ours, games, tc, workers, seed):
    """One match against one anchor. Returns the parsed result dict."""
    positions = max(1, games // 2)
    cmd = [sys.executable, "match.py", ours, anchor_path, str(positions), "0",
           "--tc", tc, "--workers", str(workers), "--seed", str(seed)]
    print("   " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = p.stdout + p.stderr
    m = None
    for line in out.splitlines():
        hit = SCORE_RE.search(line)
        if hit:
            m = hit
    if not m:
        tail = "\n".join(out.strip().splitlines()[-5:])
        print(f"   !! no score line from match.py (rc={p.returncode}). Tail:\n{tail}")
        return None
    return {"score": float(m.group(1)), "games": int(m.group(2)),
            "pct": float(m.group(3)), "elo": float(m.group(4)),
            "err": float(m.group(5))}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", metavar="ANCHOR", help="verify one anchor and exit")
    ap.add_argument("--anchors", nargs="*", default=[],
                    help="anchor modules (globs are fine)")
    ap.add_argument("--ours", default="cengine.py", help="our engine module")
    ap.add_argument("--games", type=int, default=2000, help="games per anchor")
    ap.add_argument("--tc", default="10+0.1")
    ap.add_argument("--workers", type=int, default=0, help="0 = cores/2 (TIMED)")
    ap.add_argument("--seed", type=int, default=62)
    ap.add_argument("--out", default="data/rating_ladder.json")
    a = ap.parse_args()

    if a.check:
        return check(a.check)
    paths = sorted({p for g in a.anchors for p in glob.glob(g)})
    if not paths:
        ap.error("no anchors matched -- pass --anchors opponents/anchor_*.py")

    workers = a.workers or max(1, (os.cpu_count() or 2) // 2)
    print(f"-> {len(paths)} anchors, {a.games:,} games each, {a.tc}, "
          f"{workers} workers, ours = {a.ours}")
    rows = []
    for path in paths:
        m = load_anchor(path)
        rating, lst = getattr(m, "RATING", None), getattr(m, "RATING_LIST", "?")
        if rating is None:
            print(f"-> SKIP {os.path.basename(path)}: no RATING (rig test, not "
                  f"an anchor)")
            continue
        print(f"-> {os.path.basename(path)}  anchor {rating} [{lst}]")
        r = play(path, a.ours, a.games, a.tc, workers, a.seed)
        if not r:
            continue
        r.update({"anchor": os.path.basename(path), "rating": rating,
                  "list": lst, "ours": rating + r["elo"]})
        rows.append(r)
        print(f"   {r['pct']:.2f}%  diff {r['elo']:+.2f} +/- {r['err']:.1f}"
              f"  ->  us {r['ours']:.0f} on [{lst}]")

    if not rows:
        print("-> no usable anchors")
        return 1

    print("\n== placement ==")
    by_list = {}
    for r in rows:
        by_list.setdefault(r["list"], []).append(r)
    summary = []
    for lst, rs in by_list.items():
        # Inverse-variance pooling, and only within one list.
        wsum = sum(1.0 / (r["err"] ** 2) for r in rs if r["err"] > 0)
        est = sum(r["ours"] / (r["err"] ** 2) for r in rs if r["err"] > 0) / wsum
        err = math.sqrt(1.0 / wsum)
        print(f"[{lst}]  {est:.0f} +/- {err:.0f}   from {len(rs)} anchor(s)")
        for r in rs:
            far = " (|diff| > 200: weak evidence, bracket closer)" \
                if abs(r["elo"]) > 200 else ""
            print(f"   {r['anchor']:<34} {r['ours']:.0f} "
                  f"(diff {r['elo']:+.1f} +/- {r['err']:.1f}){far}")
        summary.append({"list": lst, "estimate": est, "error": err,
                        "anchors": rs})

    os.makedirs(os.path.join(ROOT, os.path.dirname(a.out)), exist_ok=True)
    with open(os.path.join(ROOT, a.out), "w") as fh:
        json.dump({"conditions": {"tc": a.tc, "workers": workers,
                                  "ours": a.ours, "seed": a.seed,
                                  "games_per_anchor": a.games,
                                  "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                   "results": summary}, fh, indent=1)
    print(f"\n-> wrote {a.out}")
    print("These are placements on each list's scale under OUR conditions, not "
          "ratings from those lists. Quote them that way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
