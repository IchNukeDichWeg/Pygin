#!/usr/bin/env python3
"""benchmark.py -- one-position engine benchmark with warmup + averaging.

    python3 benchmark.py --type depth --value 14
    python3 benchmark.py --type time  --value 5 --runs 8 --threads 4 --hash 256
    python3 benchmark.py --type nodes --value 2000000 --fen "r1bqkbnr/..."

Runs the live C core (cengine, shipped defaults) on ONE position under ONE
limit type, WARMUP times unmeasured (macOS validates a fresh .so on first
use and caches warm -- the first run after a build reads ~50% slow, the
lesson from the v39 bench campaign), then RUNS measured runs, and prints
per-run and averaged values: nodes, time, NPS, depth, seldepth, hashfull,
best move.

The TT is reset COLD before every run (warmup and measured) so each run does
identical work -- without this, run 2 of a depth-limited search hits the
warm table and finishes in milliseconds, poisoning the average.

Limit types:
    depth  -- fixed-depth search; value = plies.
    time   -- timed search; value = SECONDS (float ok). The driver's
              soft-stop economies (P-35/U-06) are DISABLED so the full
              budget is spent, mirroring cuci's `go movetime` rule (B-05).
    nodes  -- node-limited search; value = node count (C-side abort, FB-09).
"""
import argparse
import statistics
import sys
import time

import chess

import cengine


def build_engine(threads, hash_mb):
    e = cengine.Engine()
    e.use_book = False                    # a book hit searches 0 nodes
    e.use_tb = False
    e.smp_workers = max(1, min(64, threads))
    entries = hash_mb * 1024 * 1024 // 24  # cuci's Hash MB -> bits mapping
    e._lib.set_tt_bits(max(16, entries.bit_length() - 1))
    return e


def one_run(e, board, kind, value):
    e._lib.cs_tt_reset()                  # cold: identical work every run
    t0 = time.perf_counter()
    if kind == "depth":
        mv = e.get_best_move(board.copy(), int(value))
    elif kind == "nodes":
        e.node_limit = int(value)
        try:
            mv = e.get_best_move(board.copy(), 60)
        finally:
            e.node_limit = None
    else:                                 # time
        e.use_stability_time = False      # B-05: spend the FULL budget
        e.soft_stop_frac = None
        mv = e.get_best_move_timed(board.copy(), float(value), 60)
    dt = max(1e-9, time.perf_counter() - t0)
    return {
        "move": str(mv),
        "nodes": e.nodes_searched or 0,
        "time": dt,
        "nps": (e.nodes_searched or 0) / dt,
        "depth": e.last_depth or 0,
        "seldepth": e._lib.cs_seldepth(),
        "hashfull": e._lib.cs_hashfull(),
        "score": e.last_score,
    }


def fmt(r):
    return (f"move {r['move']:7s} depth {r['depth']:2d}/{r['seldepth']:<2d} "
            f"score {r['score']:6d}  nodes {r['nodes']:>12,}  "
            f"time {r['time']:7.3f}s  nps {int(r['nps']):>10,}  "
            f"hashfull {r['hashfull']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fen", default=chess.STARTING_FEN,
                    help="position to search (default: startpos)")
    ap.add_argument("--type", required=True, dest="kind",
                    choices=("depth", "time", "nodes"),
                    help="limit type for every run")
    ap.add_argument("--value", required=True, type=float,
                    help="plies / seconds / node count (per --type)")
    ap.add_argument("--threads", type=int, default=1,
                    help="Lazy-SMP threads (default 1)")
    ap.add_argument("--hash", type=int, default=48,
                    help="TT size in MB, cuci mapping (default 48)")
    ap.add_argument("--warmup", type=int, default=2,
                    help="unmeasured warmup runs (default 2)")
    ap.add_argument("--runs", type=int, default=5,
                    help="measured runs to average (default 5)")
    args = ap.parse_args()

    try:
        board = chess.Board(args.fen)
    except ValueError as ex:
        sys.exit(f"bad FEN: {ex}")
    if board.is_game_over():
        sys.exit("position is already game-over")

    e = build_engine(args.threads, args.hash)
    print("== Pygin benchmark ==")
    print(f"fen     : {board.fen()}")
    print(f"limit   : {args.kind} = {args.value:g}   threads {e.smp_workers}"
          f"   hash {args.hash} MB   warmup {args.warmup}   runs {args.runs}")
    print(f"engine  : abi {e._lib.csearch_abi()}  (live cengine defaults)")

    for i in range(args.warmup):
        r = one_run(e, board, args.kind, args.value)
        print(f"warmup {i + 1}: {fmt(r)}")

    results = []
    for i in range(args.runs):
        r = one_run(e, board, args.kind, args.value)
        results.append(r)
        print(f"run    {i + 1}: {fmt(r)}")

    npss = [r["nps"] for r in results]
    mean_nps = statistics.fmean(npss)
    spread = (statistics.stdev(npss) / mean_nps * 100) if len(npss) > 1 else 0.0
    print(f"\n== average over {args.runs} runs ==")
    print(f"nps     : {int(mean_nps):,}   "
          f"(min {int(min(npss)):,}, max {int(max(npss)):,}, "
          f"stdev {spread:.1f}%)")
    print(f"nodes   : {int(statistics.fmean(r['nodes'] for r in results)):,}")
    print(f"time    : {statistics.fmean(r['time'] for r in results):.3f} s")
    print(f"depth   : {statistics.fmean(r['depth'] for r in results):.1f}"
          f"  (seldepth {statistics.fmean(r['seldepth'] for r in results):.1f})")
    print(f"score   : {statistics.fmean(r['score'] for r in results):+.0f} cp"
          f"   move {results[-1]['move']}")


if __name__ == "__main__":
    main()
