#!/usr/bin/env python3
"""NNUE/gen_data.py -- FI-15 Phase 2: self-play training-data generation.

    python3 NNUE/gen_data.py out.pygdata --positions 100000 \
        --nodes 5000 --workers 8 --seed 1

Plays fixed-node self-play games with the LABELING engine config and writes
quiet, shaping-free positions to the .pygdata format (data_format.py):
label = the engine's own search score (White POV cp) + the game WDL result.

LABELING ENGINE (the F49-30 amendment, binding):
  * CYCLE_DETECT = False  -- FI-29's forcible-repetition alpha-raise
    draw-flattens labels via path history that position-only features
    cannot see; labels are v48-era search semantics. The PLAYING config
    keeps FI-29 on at inference -- this class exists only here.
  * ROOT_LMR = False      -- labels come from the CONFIRMED v50 search,
    not the armed-but-unproven FI-56 candidate.
  * cold TT per labeling search (cs_tt_reset before every move): labels are
    (near-)position-deterministic, so verify_labels.py's re-search audit can
    reproduce them. Residual history dependence: in-window repetition
    scoring for records with hmc > 0 (the audit gates exactly on hmc == 0
    records and reports drift on the rest). Game history IS still passed
    (correct in-game draw play).

POSITION FILTER (F5-19 + riders; every rule doubles as the audit spec):
  * skip the randomized opening plies, in-check nodes, positions whose
    bestmove is a capture/promotion (non-quiet), |score| > 2000 cp or
    mate-range, hmc >= 40 (rule-50 shuffle window);
  * F5-19 shaping exclusions, all detectable at label time:
    cantwin (favored-by-score side has no pawns/R/Q and <= 1 minor or 2N),
    mop-up (lone-loser + npm advantage >= MOPUP_MIN_ADV, the C shortcut's
    exact gate), contempt/draw shaping (score in {0, +/-CONTEMPT} at
    hmc >= 8);
  * within-game transposition dedup.

Threat bytes (T16) are computed by the C truth (csearch.so nnue_threats)
at generation time and stored per record, so trainer and engine consume
byte-identical threat inputs (threat_ver stamps the dataset).

Parallelism: the parent spawns worker SUBPROCESSES (fresh process per
engine, per the repo's one-process-one-config .so rule), each writing a
shard; shards are merged into the final file. 223-worker server usage:
just pass --workers 223.
"""

import argparse
import ctypes
import os
import random
import subprocess
import sys

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess

from config import (LABEL_NODES, LABEL_MAX_ABS_CP, LABEL_MAX_HMC,
                    LABEL_MIN_RANDOM_PLIES, LABEL_MAX_RANDOM_PLIES,
                    LABEL_MAX_PLIES, LABEL_ADJ_CP, LABEL_ADJ_STREAK)
from data_format import RECORD_DTYPE, write_pygdata, merge_pygdata


def make_label_engine():
    import cengine

    class LabelEngine(cengine.Engine):
        CYCLE_DETECT = False   # F49-30: labels are v48-era search values;
                               # FI-29 stays on at inference
        ROOT_LMR = False       # labels from the confirmed v50 search
        USE_NNUE = False

    eng = LabelEngine()
    eng.use_book = False
    eng.use_tb = False
    eng.smp_workers = 1
    lib = eng._lib
    B = ctypes.c_uint64
    lib.nnue_threats.argtypes = [B] * 8 + [ctypes.c_int] * 2 + \
        [B, ctypes.POINTER(ctypes.c_uint8)]
    return eng


def cantwin_shaped(board, score_w):
    """F5-19: would the CW-01 clamp (or its horizon shadow) shape this
    label? Conservative: uses the SEARCH score's sign as the favored-side
    proxy, so it over-excludes slightly -- exclusion is always safe."""
    def bare(color):
        occ = board.occupied_co[color]
        if (board.pawns | board.rooks | board.queens) & occ:
            return False
        nb = bin(board.bishops & occ).count("1")
        nn = bin(board.knights & occ).count("1")
        return nb + nn <= 1 or (nb == 0 and nn == 2)
    if score_w > 0:
        return bare(chess.WHITE)
    if score_w < 0:
        return bare(chess.BLACK)
    return bare(chess.WHITE) or bare(chess.BLACK)


def mopup_shaped(board, mopup_min):
    """F5-19: the lone-loser strong mop-up shortcut's exact C gate
    (eval_white): one side bare (king+pawns only), npm advantage >= min."""
    kings, pawns = board.kings, board.pawns
    npm = {}
    lone = {}
    for color in (chess.WHITE, chess.BLACK):
        occ = board.occupied_co[color]
        lone[color] = (occ & ~kings & ~pawns) == 0
        npm[color] = (320 * bin(board.knights & occ).count("1")
                      + 330 * bin(board.bishops & occ).count("1")
                      + 500 * bin(board.rooks & occ).count("1")
                      + 900 * bin(board.queens & occ).count("1"))
    if lone[chess.WHITE] == lone[chess.BLACK]:
        return False
    if not (kings & board.occupied_co[chess.WHITE]) or \
       not (kings & board.occupied_co[chess.BLACK]):
        return False
    return abs(npm[chess.WHITE] - npm[chess.BLACK]) >= mopup_min


def play_game(eng, rng, nodes, contempt, mopup_min):
    """One self-play game; returns a list of RECORD_DTYPE rows."""
    board = chess.Board()
    for _ in range(rng.randint(LABEL_MIN_RANDOM_PLIES, LABEL_MAX_RANDOM_PLIES)):
        moves = list(board.legal_moves)
        if not moves:
            return []
        board.push(rng.choice(moves))
    if board.is_game_over(claim_draw=True):
        return []

    lib = eng._lib
    tbuf = (ctypes.c_uint8 * 16)()
    recs = []
    seen = set()
    streak = 0
    adjudicated = None
    while (not board.is_game_over(claim_draw=True)
           and len(board.move_stack) < LABEL_MAX_PLIES):
        lib.cs_tt_reset()              # cold TT: (near-)reproducible labels
        eng.node_limit = nodes
        mv = eng.get_best_move(board, 24)
        if mv is None:
            break
        score_w = eng.last_score
        mate = abs(score_w) >= eng.MATE_THRESHOLD
        if mate or abs(score_w) >= LABEL_ADJ_CP:
            streak += 1
        else:
            streak = 0

        tkey = board._transposition_key()
        keep = (not mate
                and tkey not in seen
                and not board.is_check()
                and not board.is_capture(mv) and mv.promotion is None
                and abs(score_w) <= LABEL_MAX_ABS_CP
                and board.halfmove_clock < LABEL_MAX_HMC
                and not (score_w in (0, contempt, -contempt)
                         and board.halfmove_clock >= 8)
                and not cantwin_shaped(board, score_w)
                and not mopup_shaped(board, mopup_min))
        if keep:
            seen.add(tkey)
            lib.nnue_threats(*eng._bargs(board), tbuf)
            r = np.zeros((), dtype=RECORD_DTYPE)
            r["pawns"] = board.pawns
            r["knights"] = board.knights
            r["bishops"] = board.bishops
            r["rooks"] = board.rooks
            r["queens"] = board.queens
            r["kings"] = board.kings
            r["occ_w"] = board.occupied_co[chess.WHITE]
            r["castling"] = board.clean_castling_rights()
            r["score"] = score_w
            r["stm"] = 1 if board.turn else 0
            r["ep"] = board.ep_square if board.ep_square is not None else -1
            r["hmc"] = board.halfmove_clock
            r["threat"] = np.frombuffer(bytes(tbuf), dtype=np.uint8)
            recs.append(r)

        board.push(mv)
        if streak >= LABEL_ADJ_STREAK:
            adjudicated = 1 if score_w > 0 else -1
            break

    if adjudicated is not None:
        result = adjudicated
    else:
        out = board.outcome(claim_draw=True)
        result = (0 if out is None or out.winner is None
                  else (1 if out.winner == chess.WHITE else -1))
    for r in recs:
        r["result"] = result
    return recs


def run_worker(shard_path, positions, nodes, seed):
    eng = make_label_engine()
    contempt = eng._py.CONTEMPT
    mopup_min = eng._py.MOPUP_MIN_ADV
    rng = random.Random(seed)
    rows = []
    games = 0
    while len(rows) < positions:
        rows.extend(play_game(eng, rng, nodes, contempt, mopup_min))
        games += 1
        if games % 20 == 0:
            print(f"[worker seed={seed}] {games} games, "
                  f"{len(rows)}/{positions} positions", flush=True)
    arr = np.stack(rows[:positions]) if rows else \
        np.zeros(0, dtype=RECORD_DTYPE)
    write_pygdata(shard_path, arr)
    print(f"[worker seed={seed}] done: {len(arr)} positions "
          f"from {games} games -> {shard_path}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--positions", type=int, default=100_000)
    ap.add_argument("--nodes", type=int, default=LABEL_NODES)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--shard", help=argparse.SUPPRESS)   # internal
    args = ap.parse_args()

    if args.shard:                     # worker mode (fresh process = fresh
        run_worker(args.shard, args.positions, args.nodes, args.seed)  # .so)
        return

    per = (args.positions + args.workers - 1) // args.workers
    shards = []
    procs = []
    for w in range(args.workers):
        sp = f"{args.out}.shard{w}"
        shards.append(sp)
        procs.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), args.out,
             "--shard", sp, "--positions", str(per),
             "--nodes", str(args.nodes), "--seed", str(args.seed * 100003 + w)],
            cwd=REPO_DIR))
    fails = sum(p.wait() != 0 for p in procs)
    if fails:
        sys.exit(f"gen_data: {fails} worker(s) failed; shards kept for "
                 "inspection")
    total = merge_pygdata(args.out, shards)
    for sp in shards:
        os.remove(sp)
    print(f"gen_data: {total} positions -> {args.out}")


if __name__ == "__main__":
    main()
