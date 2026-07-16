#!/usr/bin/env python3
"""scaling_probe.py -- a cheap A/B PRE-SCREEN from search-tree SHAPE.

    python3 scaling_probe.py <candidate> [--depth D] [--positions N]
    python3 scaling_probe.py all          # run the known-verdict calibration set
    python3 scaling_probe.py --list       # show candidates

Idea (from adamtwiss/coda issue #6, adapted to Pygin): instead of spending a
10,000-game A/B on every toggle, run the candidate OFF vs ON over a fixed
position set at a fixed depth, single-threaded and cold-TT, and read the
tree-shape signals that *might* predict whether the full A/B is worth it:

  * nodes-to-depth ratio (cand/base) -- < 1 means the feature reaches the
    same depth on fewer nodes ⇒ deeper in a fixed time budget. Pygin's
    doctrine is that NPS/depth converts to Elo (~1-3 Elo per 1% NPS) while
    pure tree-reshaping usually reads null, so this is the load-bearing number.
  * move-change rate -- fraction of positions whose final best move differs.
    0% ⇒ a pure-efficiency feature (Elo can only come from the node delta);
    high ⇒ a behaviour change (Elo can go either way -- this is exactly where
    Pygin's search features have historically gone null/negative).
  * tail EBF -- effective branching factor over the last few plies (geo-mean
    of nodes(d)/nodes(d-1)). Coda's hypothesis: a tail EBF that HOLDS UP means
    the search is still finding productive lines at depth (scales); one that
    COLLAPSES means the tree is converging early (caps).

CALIBRATION RESULT (2026-07-14, N=40, d14): RUN AND IT REFUTES ITSELF for
search features. Against 4 toggles whose 10k A/B we already know (FI-25
confirmed +13.52, FI-18 null -1.25, FI-06 null +2.26, FI-04 null +2.15) the
tree-shape signals do NOT track Elo: the confirmed winner had the MOST-
NEGATIVE tail-EBFΔ and the Elo-LOSING null had the most-positive tail-EBFΔ
and cut the most nodes. Node-ratio and move-change-rate don't separate them
either. So this probe is NOT a valid Elo greenlight/skip oracle for
behaviour-changing search toggles on Pygin -- treat that as a settled
negative result (see memory/scaling-probe.md), not an open question.

What it IS still good for: (1) the move-change rate reliably classifies a
toggle as EFFICIENCY-ONLY (0% move change ⇒ can only pay via node savings,
the safe NPS-style bet) vs BEHAVIOUR-CHANGING; (2) a cheap "does this toggle
do anything at all" sniff test; (3) the ORIGINAL coda use -- TC-scaling of
NPS/efficiency features -- was never actually tested here (all 4 calibration
candidates were behaviour-changing), so that remains open.

Single .so, runtime toggle flips only (same code, like selftest's set_*(0)
pins) -- NOT two engine versions in one process (that hits the dyld
cross-contamination trap; see memory/so-cross-contamination).
"""
import sys, os, statistics, math
import chess
import cengine

# candidate -> (description, baseline_setup, candidate_setup, known_verdict)
# setup fns take the ctypes lib handle and flip exactly ONE toggle.
CANDIDATES = {
    "hist_prune": (
        "FI-23 history-driven quiet pruning (set_hist_prune 0 vs 256)",
        lambda lib: lib.set_hist_prune(0),
        lambda lib: lib.set_hist_prune(256),
        "PENDING (armed, 21st A/B)"),
    "see_prune": (
        "FI-18 SEE pruning of losing captures (set_see_prune 0 vs 1)",
        lambda lib: lib.set_see_prune(0),
        lambda lib: lib.set_see_prune(1),
        "NULL -1.25 (dead null, dormant)"),
    "root_order": (
        "FI-06 root-move ordering (set_root_order 0 vs 1)",
        lambda lib: lib.set_root_order(0),
        lambda lib: lib.set_root_order(1),
        "NULL +2.26 (CI covers 0, dormant)"),
    "lmr_hist": (
        "FI-04 history-based LMR (set_lmr_hist 0 vs 8192)",
        lambda lib: lib.set_lmr_hist(0),
        lambda lib: lib.set_lmr_hist(8192),
        "NULL +2.15 (below tune bar, dormant)"),
    "tt_eval_sharpen": (
        "FI-25 TT-value pruning-eval sharpener (set_tt_eval_sharpen 0 vs 1)",
        lambda lib: lib.set_tt_eval_sharpen(0),
        lambda lib: lib.set_tt_eval_sharpen(1),
        "CONFIRMED +13.52 (shipped v45)"),
}
CALIBRATION = ["tt_eval_sharpen", "see_prune", "root_order", "lmr_hist"]


def load_positions(n):
    """N full FENs spread evenly across the A/B opening book (UHO), so the
    probe samples the same distribution the real matches draw from."""
    for path in ("UHO_4060_v4.epd", "fen.txt"):
        if os.path.isfile(path):
            with open(path) as f:
                lines = [l.strip() for l in f if l.strip()]
            step = max(1, len(lines) // n)
            picked = lines[::step][:n]
            return [" ".join(l.split()[:6]) for l in picked]
    # fallback: a small built-in mixed set
    return [
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3",
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "8/2k5/3p4/p2P1p2/P2P1P2/8/8/4K3 w - - 0 1",
    ]


def curve(engine, board, depth):
    """One cold-TT fixed-depth search; return per-depth cumulative nodes plus
    the final best move and seldepth. Uses the on_depth callback, which fires
    once per completed ID iteration with cumulative nodes (same signal the
    CE_LADDER pins)."""
    rows = {}
    engine.on_depth = lambda rec: rows.__setitem__(rec["depth"],
                                                    rec.get("nodes", 0))
    engine._lib.cs_tt_reset()
    mv = engine.get_best_move(board, depth)
    engine.on_depth = None
    seld = engine._lib.cs_seldepth()
    return rows, (mv.uci() if mv else None), seld


def tail_ebf(rows, k=4):
    """Geo-mean of nodes(d)/nodes(d-1) over the last k plies present."""
    ds = sorted(d for d in rows if rows[d] > 0)
    if len(ds) < 3:
        return None
    ratios = []
    for a, b in zip(ds, ds[1:]):
        if rows[a] > 0:
            ratios.append(rows[b] / rows[a])
    tail = ratios[-k:]
    return math.exp(sum(math.log(r) for r in tail) / len(tail)) if tail else None


def probe(name, depth, npos):
    desc, base_setup, cand_setup, verdict = CANDIDATES[name]
    engine = cengine.Engine()
    engine.use_book = engine.use_tb = False
    engine.smp_workers = 1
    lib = engine._lib
    positions = [chess.Board(f) for f in load_positions(npos)]

    node_ratios, ebf_base, ebf_cand = [], [], []
    move_changes, score_ds = 0, []
    usable = 0
    for b in positions:
        base_setup(lib)
        rb, mb, sb = curve(engine, b, depth)
        cand_setup(lib)
        rc, mc, sc = curve(engine, b, depth)
        common = sorted(set(rb) & set(rc) & {d for d in rb if rb[d] > 0})
        if len(common) < 3:
            continue                         # mate/too-shallow: skip
        usable += 1
        d = common[-1]                       # deepest depth both reached
        if rb[d] > 0:
            node_ratios.append(rc[d] / rb[d])
        eb, ec = tail_ebf(rb), tail_ebf(rc)
        if eb: ebf_base.append(eb)
        if ec: ebf_cand.append(ec)
        if mb != mc:
            move_changes += 1

    def med(xs): return statistics.median(xs) if xs else float("nan")
    nr = med(node_ratios)
    mcr = move_changes / usable if usable else float("nan")
    eb, ec = med(ebf_base), med(ebf_cand)

    print(f"\n=== {name} ===")
    print(f"  {desc}")
    print(f"  known A/B verdict: {verdict}")
    print(f"  positions used: {usable}/{npos}   depth: {depth}   threads: 1")
    _dir = ("fewer nodes ⇒ deeper" if nr < 0.995 else
            "more nodes ⇒ shallower" if nr > 1.005 else "≈ equal")
    print(f"  median nodes-to-depth ratio (cand/base): {nr:.3f}   ({_dir})")
    print(f"  move-change rate: {mcr*100:.0f}%  ({move_changes}/{usable})")
    print(f"  tail EBF: base {eb:.3f}  cand {ec:.3f}"
          f"   ({'holds/expands' if ec >= eb else 'collapses'})")
    print(f"  --> {_verdict(nr, mcr, eb, ec)}")
    return dict(name=name, node_ratio=nr, move_change=mcr,
                ebf_base=eb, ebf_cand=ec, verdict=verdict)


def _verdict(nr, mcr, eb, ec):
    """Descriptive read of the signals -- deliberately NOT an Elo prediction.
    Calibration (memory/scaling-probe.md) found that the node-ratio and
    tail-EBF signals do NOT reliably separate known winners from known nulls
    at small N (the apparent tail-EBF separation at N=10 vanished at N=16), so
    we don't pretend to greenlight/skip from tree shape. The ONE robust signal
    is the move-change rate: a feature that never changes the best move can
    only pay via node savings.

    ponytail: honest-null classifier, not an oracle -- upgrade to a real
    predictor only once calibration shows a stable, correlating signal."""
    if mcr < 0.02:
        if nr < 0.97:
            return ("EFFICIENCY-ONLY: same moves, ~{:.0%} fewer nodes ⇒ any "
                    "Elo comes purely via depth. This class is the safe bet "
                    "(cf. the NPS wins) -- worth an A/B.").format(1 - nr)
        return ("INERT: no move change, no node saving ⇒ expect ~0. Deprioritize.")
    return ("BEHAVIOUR-CHANGING ({:.0%} moves differ): the A/B is the only "
            "arbiter. Tree-shape signals did NOT reliably predict win-vs-null "
            "in calibration, so this run neither greenlights nor skips it -- "
            "it just confirms the feature is live.").format(mcr)


def main():
    args = sys.argv[1:]
    if not args or "--list" in args:
        print("candidates:", ", ".join(CANDIDATES))
        print("  or:  all   (runs the known-verdict calibration set)")
        return
    depth = int(args[args.index("--depth")+1]) if "--depth" in args else 14
    npos = int(args[args.index("--positions")+1]) if "--positions" in args else 12
    target = args[0]
    names = CALIBRATION if target == "all" else [target]
    results = []
    for n in names:
        if n not in CANDIDATES:
            print(f"unknown candidate {n!r}; --list to see options"); continue
        results.append(probe(n, depth, npos))
    if target == "all" and results:
        print("\n=== calibration summary (probe signal vs KNOWN A/B) ===")
        print(f"  {'candidate':<16} {'nodeΔ':>7} {'moveΔ':>6} {'tailEBFΔ':>9}"
              f"   known verdict")
        for r in results:
            print(f"  {r['name']:<16} {r['node_ratio']:>6.3f}"
                  f" {r['move_change']*100:>5.0f}% {r['ebf_cand']-r['ebf_base']:>+9.3f}"
                  f"   {CANDIDATES[r['name']][3]}")
        print("  CALIBRATION FINDING (N=40, 2026-07-14): the tree-shape signals"
              " do NOT track win-vs-null for Pygin search features. The +13.52"
              " winner had the MOST-NEGATIVE tail-EBFΔ; the -1.25 null had the"
              " most-positive AND cut the most nodes. Node-ratio and move-Δ don't"
              " separate either. ⇒ do not use this as an Elo greenlight/skip"
              " oracle for behaviour-changing search toggles. See"
              " memory/scaling-probe.md.")


def _selfcheck():
    """A config compared to ITSELF must give ratio≈1.0 and 0% move change --
    if it doesn't, the harness (cold-TT reset, node accounting) is broken."""
    CANDIDATES["_id"] = ("identity (set_hist_prune 0 vs 0)",
                         lambda lib: lib.set_hist_prune(0),
                         lambda lib: lib.set_hist_prune(0), "n/a")
    r = probe("_id", depth=10, npos=4)
    assert abs(r["node_ratio"] - 1.0) < 1e-9, r["node_ratio"]
    assert r["move_change"] == 0.0, r["move_change"]
    print("\nselfcheck OK: identity probe is a clean null")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--selfcheck"]:
        _selfcheck()
    else:
        main()
