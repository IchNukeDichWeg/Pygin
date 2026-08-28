#!/usr/bin/env python3
"""testing/test_tt_deadtag.py -- randomised stress test for FI-115.

    python3 testing/test_tt_deadtag.py [--minutes 10] [--seed N]

WHY THIS EXISTS. FI-115 does two things and only one of them is provable.
Evicting a TT entry whose stored piece count EXCEEDS the root's is safe by
construction -- material is irreversible, so that position cannot recur. But
the same commit also stamps a 6-bit piece count into d2 alongside depth, flag
and generation, and changes what happens to entries that are still REACHABLE
(depth-protected now, evicted on age before). Bit-packing next to live fields
and a rewritten victim rule are exactly the things that break quietly: a
corrupted depth or generation does not crash, it just makes the search worse
in a way no bench notices. The shipped bench and the d14 ladder are BLIND
here by construction -- both reset the table per search, so it never fills
and the rule never fires.

So this test does the opposite of the bench: it keeps the table WARM, makes
it TINY so it saturates in milliseconds, and drives it with positions that
are random rather than drawn from games, because a game-derived suite only
ever exercises the shapes that games happen to reach.

WHAT IT CHECKS
  1. INERTNESS      -- with the tag OFF, results must be identical to a second
                       OFF process. If the stamp corrupted a neighbouring
                       field, OFF would stop reproducing.
  2. DETERMINISM    -- with the tag ON and a cold table, the same position must
                       give byte-identical nodes+score every repeat. Catches
                       reads of uninitialised or torn entries.
  3. ENGAGEMENT     -- ON and OFF must actually DIFFER somewhere under
                       pressure. A test that passes because the feature never
                       fires is worse than no test.
  4. SATURATION     -- thousands of searches through a 1.5 MB table kept warm
                       across positions, so every store is an eviction and the
                       victim rule runs constantly. Must not crash, must always
                       return a LEGAL move, hashfull must stay in [0, 1000].
  5. DESCENDING PC  -- the FI-115 scenario proper: search positions in order of
                       DECREASING material against a warm table, so earlier
                       entries become provably dead as the root shrinks.
  6. EDGE MATERIAL  -- promotions (piece count flat while material changes),
                       en passant, castling rights, and 3-to-5-man endgames
                       where the count sits at the bottom of the 6-bit field.

Positions are randomly generated, and the seed is printed so any failure
replays exactly.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time

import chess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL = []


def _paint(t, c):
    return f"{c}{t}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else t


def check(label, ok, detail=""):
    line = f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else "")
    print(_paint(line, "\033[32m" if ok else "\033[31m"), flush=True)
    if not ok:
        FAIL.append(label)
    return ok


def bar(done, total, t0, stage):
    el = time.time() - t0
    rate = done / el if el else 0
    eta = (total - done) / rate if rate else 0
    line = (f"    {stage} {done:,}/{total:,} ({100*done/total:5.1f}%) "
            f"{rate:5.1f}/s  elapsed {el:5.0f}s  ETA {eta:5.0f}s")
    if sys.stdout.isatty():
        print("\r" + line + "  ", end="", flush=True)
    elif done == total or done % max(1, total // 8) == 0:
        print(line, flush=True)


# --------------------------------------------------------------------- #
# position generation -- random, NOT drawn from played games
# --------------------------------------------------------------------- #
def random_positions(rng, n, want_pc=None):
    """Random legal positions by playing uniformly random legal moves.

    Uniform-random play is the point: a suite built from engine games only ever
    contains the shapes the engine steers into, and those are exactly the ones
    already covered by every other test here. Random play reaches lopsided
    material, stranded kings and dead-drawn endings that games do not.
    """
    out = []
    guard = 0
    while len(out) < n and guard < n * 60:
        guard += 1
        b = chess.Board()
        for _ in range(rng.randint(0, 140)):
            if b.is_game_over(claim_draw=False):
                break
            b.push(rng.choice(list(b.legal_moves)))
        if b.is_game_over(claim_draw=False):
            continue
        pc = chess.popcount(b.occupied)
        if want_pc and not (want_pc[0] <= pc <= want_pc[1]):
            continue
        out.append(b.fen())
    return out


WORKER = """
import json, sys, os
sys.path.insert(0, {root!r})
import chess, cengine
cengine.Engine.TT_DEADTAG = {tag}
cengine.Engine.TT_BITS = {bits}
e = cengine.Engine(); e.use_book = False; e.use_tb = False; e.smp_workers = 1
fens = json.load(open(sys.argv[1]))
out = []
for i, f in enumerate(fens):
    if {cold}:
        e._lib.cs_tt_reset()
    b = chess.Board(f)
    e.node_limit = {nodes}
    mv = e.get_best_move(b, {depth})
    mv = mv[0] if isinstance(mv, tuple) else mv
    legal = mv is not None and mv in b.legal_moves
    out.append([str(mv), int(e.last_score), int(e.nodes_searched),
                bool(legal), int(e._lib.cs_hashfull())])
print(json.dumps(out))
"""


def run(fens, tag, bits, nodes, depth, cold, tmp):
    """One config per process -- cengine's toggles are process-wide (FB-04)."""
    with open(tmp, "w") as fh:
        json.dump(fens, fh)
    src = WORKER.format(root=ROOT, tag=tag, bits=bits, nodes=nodes,
                        depth=depth, cold=cold)
    p = subprocess.run([sys.executable, "-c", src, tmp],
                       capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        return None, (p.stderr or "")[-1500:]
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), ""
    except Exception as exc:                       # noqa: BLE001
        return None, f"unparseable worker output: {exc}\n{p.stdout[-500:]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    tmp = os.path.join(ROOT, ".tt_deadtag_fens.json")
    t_start = time.time()
    budget = args.minutes * 60

    print("== FI-115 dead-entry TT replacement: randomised stress ==")
    print(f"seed {seed}   (rerun exactly with --seed {seed})")
    print(f"budget ~{args.minutes:.0f} min\n")

    # scale the workload to the time budget, measured against ~10 minutes
    scale = max(0.1, budget / 600.0)
    SAT_NODES = 250_000      # deep enough that each search really
                             # churns the 1.5 MB table it shares
    N_SAT = int(900 * scale)
    N_DET = int(40 * scale)
    N_PC = int(220 * scale)
    N_EDGE = int(160 * scale)

    try:
        # ---- 1 + 3: inertness OFF, and ON vs OFF must actually differ ---- #
        print("[1/6] generating random positions ...", flush=True)
        base = random_positions(rng, max(60, int(70 * scale)))
        print(f"      {len(base)} positions, piece counts "
              f"{min(chess.popcount(chess.Board(f).occupied) for f in base)}"
              f"-{max(chess.popcount(chess.Board(f).occupied) for f in base)}\n")

        print("[2/6] INERTNESS: tag OFF must reproduce across processes")
        a, err = run(base, False, 16, 60000, 24, True, tmp)
        b, err2 = run(base, False, 16, 60000, 24, True, tmp)
        if a is None or b is None:
            check("tag OFF runs at all", False, err or err2)
        else:
            same = sum(x == y for x, y in zip(a, b))
            check("tag OFF is bit-reproducible across processes",
                  same == len(a), f"{same}/{len(a)} identical")
            check("tag OFF returns a legal move everywhere",
                  all(r[3] for r in a), f"{sum(r[3] for r in a)}/{len(a)}")

        print("\n[3/6] ENGAGEMENT: tag ON must differ from OFF under pressure")
        c, err = run(base, True, 16, 60000, 24, False, tmp)   # WARM, tiny TT
        d, _ = run(base, False, 16, 60000, 24, False, tmp)
        if c is None or d is None:
            check("tag ON runs at all", False, err)
        else:
            diff = sum(x != y for x, y in zip(c, d))
            check("ON differs from OFF somewhere (feature really fires)",
                  diff > 0, f"{diff}/{len(c)} positions differ")
            check("tag ON returns a legal move everywhere",
                  all(r[3] for r in c), f"{sum(r[3] for r in c)}/{len(c)}")
            hf = [r[4] for r in c]
            check("hashfull stays in [0,1000] with the tag on",
                  all(0 <= h <= 1000 for h in hf),
                  f"min {min(hf)} max {max(hf)}")

        # ---- 2: determinism with the tag ON --------------------------- #
        print("\n[4/6] DETERMINISM: tag ON, cold table, repeated searches")
        det = base[:max(6, N_DET)]
        runs = []
        for k in range(3):
            r, e2 = run(det, True, 16, 80000, 24, True, tmp)
            if r is None:
                check("determinism run completed", False, e2)
                break
            runs.append(r)
        if len(runs) == 3:
            ok = runs[0] == runs[1] == runs[2]
            check("tag ON is deterministic over 3 repeats", ok,
                  f"{len(det)} positions x 3")

        # ---- 5: descending piece count against a WARM table ----------- #
        print("\n[5/6] DESCENDING MATERIAL: warm table, root shrinks")
        print("      (entries stored at high material become provably dead)")
        buckets = []
        t0 = time.time()
        for lo, hi in ((28, 32), (22, 27), (16, 21), (10, 15), (6, 9), (3, 5)):
            got = random_positions(rng, max(4, N_PC // 6), want_pc=(lo, hi))
            buckets.extend(got)
            bar(len(buckets), N_PC, t0, "gen")
        if sys.stdout.isatty():
            print()
        desc, e3 = run(buckets, True, 16, 50000, 24, False, tmp)   # WARM
        if desc is None:
            check("descending-material sweep completed", False, e3)
        else:
            check("descending material: every move legal",
                  all(r[3] for r in desc),
                  f"{sum(r[3] for r in desc)}/{len(desc)} over "
                  f"{len(buckets)} positions, 32 men down to 3")
            # cengine reports mate as MATE_SCORE (1,000,000) minus the ply,
            # so a forced mate legitimately reads ~999,99x -- random positions
            # are FULL of them. The real invariant is that every score is
            # either a plain cp value or a well-formed mate, never something
            # outside both bands (which is what a corrupted d2 would look like).
            MATE, THRESH = 1_000_000, 999_000
            bad = [r[1] for r in desc
                   if not (abs(r[1]) <= 30000 or THRESH <= abs(r[1]) <= MATE)]
            check("descending material: every score is cp or a valid mate",
                  not bad, f"{len(bad)} outside both bands"
                           + (f", e.g. {bad[:3]}" if bad else
                              f"; mates seen: "
                              f"{sum(abs(r[1]) >= THRESH for r in desc)}"))

        # ---- 6: edge material ----------------------------------------- #
        print("\n[6/6] EDGE MATERIAL: promotions, ep, castling, 3-5 men")
        edge = random_positions(rng, N_EDGE // 2, want_pc=(3, 6))
        edge += [f for f in random_positions(rng, N_EDGE, want_pc=(7, 32))
                 if chess.Board(f).ep_square is not None
                 or chess.Board(f).castling_rights][:N_EDGE // 2]
        edge += [
            "8/P6k/8/8/8/8/6K1/8 w - - 0 1",             # promotion available
            "8/8/8/8/8/8/6k1/4K2R w K - 0 1",            # castling, 3 men
            "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",         # en passant
            "8/8/8/4k3/8/8/8/4K3 w - - 0 1",             # bare kings, pc=2
            "8/8/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1",  # 16 men, no black
        ]
        eg, e4 = run(edge, True, 16, 40000, 24, False, tmp)
        if eg is None:
            check("edge-material sweep completed", False, e4)
        else:
            check("edge material: every move legal", all(r[3] for r in eg),
                  f"{sum(r[3] for r in eg)}/{len(eg)}")

        # ---- 4: saturation churn -------------------------------------- #
        # Size this phase from a MEASURED rate rather than a guessed one: the
        # first version assumed 2.2 searches/sec, the machine did 90, and a
        # "10 minute" run finished in five seconds -- a stress test that does
        # not stress anything passes for the wrong reason.
        left = budget - (time.time() - t_start)
        cal = random_positions(rng, 12)
        t_cal = time.time()
        _c, _e = run(cal, True, 16, SAT_NODES, 24, False, tmp)
        rate = len(cal) / max(0.05, time.time() - t_cal)
        n_sat = max(60, min(20000, int(left * rate * 0.92)))
        print(f"\n[+] SATURATION: {n_sat:,} searches through a warm 1.5 MB "
              f"table\n    (calibrated at {rate:.0f} searches/s, "
              f"{left:.0f}s of budget left)")
        sat = random_positions(rng, n_sat)
        t0 = time.time()
        res, e5 = run(sat, True, 16, SAT_NODES, 24, False, tmp)
        if res is None:
            check("saturation sweep completed without crashing", False, e5)
        else:
            hf = [r[4] for r in res]
            check("saturation: no crash over the whole sweep", True,
                  f"{len(res)} searches in {time.time()-t0:.0f}s")
            check("saturation: every move legal", all(r[3] for r in res),
                  f"{sum(r[3] for r in res)}/{len(res)}")
            check("saturation: hashfull sane throughout",
                  all(0 <= h <= 1000 for h in hf),
                  f"min {min(hf)} max {max(hf)} permille")
            check("saturation: table really did fill (rule was exercised)",
                  max(hf) > 700, f"peak hashfull {max(hf)} permille")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print()
    if FAIL:
        print(_paint(f"== FAILED: {len(FAIL)} check(s): {', '.join(FAIL)} "
                     f"==  (replay with --seed {seed})", "\033[31m"))
        sys.exit(1)
    print(_paint(f"== ALL FI-115 STRESS CHECKS PASSED ==  "
                 f"({time.time()-t_start:.0f}s, seed {seed})", "\033[32m"))
    sys.exit(0)


if __name__ == "__main__":
    main()
