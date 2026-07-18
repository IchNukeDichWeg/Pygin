#!/usr/bin/env python3
"""NNUE/logs_to_pygdata.py -- convert match.py A/B battle logs into NNUE
training data (.pygdata), as a SUPPLEMENT to gen_data.py self-play.

    python3 NNUE/logs_to_pygdata.py out.pygdata "New logs"/*.txt \
        --allow engine31 --allow engine32 ...

Reads the per-game blocks match.py writes (=== Game N === / FEN / engine
paths / Outcome / "--- Engine Logs ---" move lines), replays each game,
and keeps quiet positions labeled by the logged search score of the side
to move. Result (game WDL) comes from the Outcome line. Threat bytes are
computed by the C truth (csearch.so nnue_threats), same as gen_data.py.

WHY A VERSION GATE (--allow): a logged cp is only a valid label if the
engine that produced it predates FI-29 (CYCLE_DETECT, shipped v49) --
the cycle bound draw-flattens scores via path history that position-only
features cannot see (the F49-30 amendment), and that flattening cannot be
detected after the fact. Every OTHER label rule (quiet-only, |cp|<=2000,
cantwin/mop-up/contempt shaping, hmc, dedup) is re-checked here at
conversion time, identically to gen_data.py. The C-era eval scale itself
is constant across v31-v48 (search features changed, the eval did not,
apart from CW-01 -- which the cantwin detector excludes), so mixing
versions is safe ONCE the cycle gate is applied.

Only moves played by a side whose engine PATH contains one of the --allow
substrings are recorded (both sides still get replayed for continuity).
Recommended allow-list: the Old Engine snapshot names 31..48 (their
version is pinned by their path). 'cengine.py' is ambiguous (its version
depends on the campaign date) -- allow it only for logs you can date to
pre-v49 campaigns (before 2026-07-17).

Labels here come from DEEPER searches than gen_data's 5,000 nodes (50+0.2
TC ~ depth 12-16): higher quality per position, at the cost of not being
reproducible (verify_labels.py's re-search gate does not apply to this
source; its dataset-stats block still does). Timed-search labels also
carry warm-TT/history context -- acceptable for a supplementary source.
"""

import argparse
import ctypes
import glob
import os
import re
import sys

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)
sys.path.insert(0, NNUE_DIR)
sys.path.insert(0, REPO_DIR)

import chess

from config import LABEL_MAX_ABS_CP, LABEL_MAX_HMC
from data_format import RECORD_DTYPE, write_pygdata, merge_pygdata
from gen_data import cantwin_shaped, mopup_shaped

MOVE_RE = re.compile(
    r"^\[(?P<name>[^\]]+)\] move (?P<san>\S+): info depth (?P<depth>\d+) "
    r"score (?P<kind>cp|mate) (?P<val>-?\d+) ")

CONTEMPT = 50            # engine.py CONTEMPT (constant across the C era)
MOPUP_MIN = 400          # engine.py MOPUP_MIN_ADV (constant across the C era)


def load_threat_lib():
    lib = ctypes.CDLL(os.path.join(REPO_DIR, "csearch.so"))
    B = ctypes.c_uint64
    lib.nnue_threats.argtypes = [B] * 8 + [ctypes.c_int] * 2 + \
        [B, ctypes.POINTER(ctypes.c_uint8)]
    return lib


def game_blocks(path):
    """Yield dicts: fen, white_path, black_path, result, move-lines."""
    blk = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("=== Game "):
                if blk:
                    yield blk
                blk = {"fen": None, "white": "", "black": "",
                       "result": None, "moves": [], "in_log": False}
            elif blk is None:
                continue
            elif line.startswith("FEN: "):
                blk["fen"] = line[5:].strip()
            elif "(White): " in line:
                blk["white"] = line.split("(White): ", 1)[1].strip()
            elif "(Black): " in line:
                blk["black"] = line.split("(Black): ", 1)[1].strip()
            elif line.startswith("Outcome: "):
                o = line[9:]
                if o.startswith("ERROR"):
                    blk["result"] = None          # excluded game
                elif o.startswith("draw") or "1/2-1/2" in o:
                    blk["result"] = 0
                elif "1-0" in o:
                    blk["result"] = 1
                elif "0-1" in o:
                    blk["result"] = -1
            elif line.startswith("--- Engine Logs ---"):
                blk["in_log"] = True
            elif line.startswith("--- PGN ---"):
                blk["in_log"] = False
            elif blk["in_log"] and line.startswith("["):
                blk["moves"].append(line)
    if blk:
        yield blk


def convert_log(path, allow, lib, seen, stats):
    tbuf = (ctypes.c_uint8 * 16)()
    rows = []
    for blk in game_blocks(path):
        if blk["result"] is None or blk["fen"] is None:
            stats["skipped_games"] += 1
            continue
        try:
            board = chess.Board(blk["fen"])
        except ValueError:
            stats["skipped_games"] += 1
            continue
        ok = True
        for line in blk["moves"]:
            m = MOVE_RE.match(line)
            if not m:
                continue                      # PV lines never match
            side_path = blk["white"] if board.turn else blk["black"]
            try:
                move = board.parse_san(m.group("san"))
            except ValueError:
                stats["desync_games"] += 1    # replay desync: drop the rest
                ok = False
                break
            keep = any(a in side_path for a in allow)
            if keep and m.group("kind") == "cp":
                cp_stm = int(m.group("val"))
                score_w = cp_stm if board.turn else -cp_stm
                tkey = board._transposition_key()
                if (abs(score_w) <= LABEL_MAX_ABS_CP
                        and tkey not in seen
                        and not board.is_check()
                        and not board.is_capture(move)
                        and move.promotion is None
                        and board.halfmove_clock < LABEL_MAX_HMC
                        and not (score_w in (0, CONTEMPT, -CONTEMPT)
                                 and board.halfmove_clock >= 8)
                        and not cantwin_shaped(board, score_w)
                        and not mopup_shaped(board, MOPUP_MIN)):
                    seen.add(tkey)
                    lib.nnue_threats(
                        board.pawns, board.knights, board.bishops,
                        board.rooks, board.queens, board.kings,
                        board.occupied_co[chess.WHITE],
                        board.occupied_co[chess.BLACK],
                        1 if board.turn else 0,
                        board.ep_square if board.ep_square is not None else -1,
                        board.clean_castling_rights(), tbuf)
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
                    r["result"] = blk["result"]
                    r["stm"] = 1 if board.turn else 0
                    r["ep"] = (board.ep_square
                               if board.ep_square is not None else -1)
                    r["hmc"] = board.halfmove_clock
                    r["threat"] = np.frombuffer(bytes(tbuf), dtype=np.uint8)
                    rows.append(r)
            board.push(move)
        if ok:
            stats["games"] += 1
    stats["positions"] += len(rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("logs", nargs="+", help="match.py *_vs_*.txt log files "
                                            "(globs ok)")
    ap.add_argument("--allow", action="append", default=[],
                    help="engine-path substring whose moves' labels are "
                         "kept (repeatable). E.g. --allow engine31. "
                         "Only pre-v49 (pre-CYCLE_DETECT) engines!")
    args = ap.parse_args()
    if not args.allow:
        sys.exit("logs_to_pygdata: pass at least one --allow (pre-v49 "
                 "engine path substring, e.g. --allow engine48)")

    paths = []
    for g in args.logs:
        paths.extend(sorted(glob.glob(g)) if any(c in g for c in "*?[") else [g])
    lib = load_threat_lib()
    seen = set()
    stats = {"games": 0, "skipped_games": 0, "desync_games": 0,
             "positions": 0}
    shards = []
    for i, p in enumerate(paths):
        rows = convert_log(p, args.allow, lib, seen, stats)
        sp = f"{args.out}.shard{i}"
        write_pygdata(sp, np.stack(rows) if rows else
                      np.zeros(0, dtype=RECORD_DTYPE))
        shards.append(sp)
        print(f"[{i+1}/{len(paths)}] {os.path.basename(p)}: "
              f"+{len(rows):,} positions (total {stats['positions']:,})",
              flush=True)
    total = merge_pygdata(args.out, shards)
    for sp in shards:
        os.remove(sp)
    print(f"done: {total:,} positions from {stats['games']:,} games "
          f"-> {args.out}")
    print(f"  skipped {stats['skipped_games']:,} error/unparseable games, "
          f"{stats['desync_games']:,} replay desyncs; global dedup "
          f"{len(seen):,} unique positions kept")


if __name__ == "__main__":
    main()
