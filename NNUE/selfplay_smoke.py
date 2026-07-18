#!/usr/bin/env python3
"""NNUE/selfplay_smoke.py -- FI-15 Phase 5 stability smoke.

    python3 NNUE/selfplay_smoke.py [--games 100] [--nodes 3000]

Plays fast fixed-node self-play games with g_use_nnue=1 (the toy net --
playing strength is IRRELEVANT, this proves plumbing): no crash, no
exception, only legal moves, finite sane scores, games terminate, RSS does
not grow unbounded (leak canary), and after all games the warm process
still finds mates and detects draws (TT-integrity canary: a corrupted
persistent table would poison exactly these).
"""

import argparse
import os
import resource
import sys

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import random

import chess

NET = os.path.join(NNUE_DIR, "nets", "toy.nnue")


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--nodes", type=int, default=3000)
    args = ap.parse_args()

    import cengine
    cengine.Engine.USE_NNUE = True
    cengine.Engine.NNUE_FILE = NET
    eng = cengine.Engine()
    eng.use_book = False
    eng.use_tb = False
    eng.smp_workers = 1

    rng = random.Random(0)
    rss0 = rss_mb()
    results = {1: 0, 0: 0, -1: 0}
    plies = 0
    for g in range(args.games):
        board = chess.Board()
        for _ in range(rng.randint(2, 8)):          # tiny random opening
            mv = list(board.legal_moves)
            if mv:
                board.push(rng.choice(mv))
        while (not board.is_game_over(claim_draw=True)
               and len(board.move_stack) < 250):
            eng.node_limit = args.nodes
            mv = eng.get_best_move(board, 24)
            assert mv is not None and mv in board.legal_moves, \
                f"illegal/none move {mv} in {board.fen()}"
            s = eng.last_score
            assert -eng.MATE_SCORE <= s <= eng.MATE_SCORE, \
                f"insane score {s} in {board.fen()}"
            board.push(mv)
            plies += 1
        out = board.outcome(claim_draw=True)
        r = (0 if out is None or out.winner is None
             else (1 if out.winner == chess.WHITE else -1))
        results[r] += 1
        if (g + 1) % 20 == 0:
            print(f"  {g+1}/{args.games} games, {plies} plies, "
                  f"rss {rss_mb():.0f} MB", flush=True)

    growth = rss_mb() - rss0
    print(f"games W/D/L {results[1]}/{results[0]}/{results[-1]}, "
          f"{plies} plies total, RSS growth {growth:.0f} MB")
    assert growth < 300, f"suspicious RSS growth {growth:.0f} MB (leak?)"

    # TT-integrity canary in the SAME warm process
    eng.node_limit = None
    eng.get_best_move(chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"), 4)
    assert eng.last_score >= eng.MATE_THRESHOLD, "warm-TT mate lost"
    eng._lib.cs_tt_reset()
    eng.get_best_move(chess.Board("8/8/8/4k3/8/2N5/8/4K3 w - - 0 1"), 8)
    assert abs(eng.last_score) <= 60, "KNvK draw lost after smoke"
    print("selfplay smoke OK (no crash, legal play, sane scores, no leak, "
          "TT intact)")


if __name__ == "__main__":
    main()
