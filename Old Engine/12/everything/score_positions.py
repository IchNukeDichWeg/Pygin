"""
score_positions.py
==================
Step 1 of Texel tuning. Reads PGN files, extracts quiet positions, scores them
with Stockfish at a fixed depth, and writes a CSV dataset for tune.py.

    python3 score_positions.py games.pgn -o positions.csv
    python3 score_positions.py a.pgn b.pgn --depth 10 --max-positions 100000

Output: CSV with columns  fen,score_cp  where score_cp is in centipawns from
White's perspective, clamped to ±SCORE_CAP. Positions with a mate score or
|score| > SCORE_CAP are dropped.

Quiet-position criteria (all must hold):
  * not in check
  * the move that reached this position was not a capture or promotion
  * past the first SKIP_MOVES half-moves of the game (skip the opening)
  * game is not over

50 000 – 200 000 positions is a good target. More is always better but takes
longer; the tuner can subsample at run-time.
"""

import argparse
import csv
import os
import sys
import time

import chess
import chess.engine
import chess.pgn


# ====================================================================== #
#  CONFIG -- edit here or pass as CLI flags
# ====================================================================== #
DEFAULT_STOCKFISH   = "stockfish"   # path / name of the Stockfish binary
DEFAULT_DEPTH       = 12            # Stockfish depth per position
DEFAULT_SKIP_MOVES  = 8             # skip first N half-moves per game (book)
DEFAULT_MAX_GAME    = 30            # max positions extracted per game
DEFAULT_SCORE_CAP   = 2000          # discard positions where |score| > this (cp)
DEFAULT_OUTPUT      = "positions.csv"
# ====================================================================== #


def iter_quiet(game, skip_moves, max_per_game):
    """Yield chess.Board copies for each quiet position in the game."""
    board = game.board()
    ply = 0
    count = 0
    for node in game.mainline():
        move = node.move
        ply += 1
        # Check capture/promotion BEFORE push (is_capture reads board state pre-move).
        is_cap  = board.is_capture(move)
        is_prom = bool(move.promotion)
        board.push(move)
        if board.is_game_over():
            break
        if ply <= skip_moves:
            continue
        if count >= max_per_game:
            break
        # Quiet: not in check after the move, the move itself not a capture/promo.
        if board.is_check() or is_cap or is_prom:
            continue
        yield board.copy()
        count += 1


def main():
    ap = argparse.ArgumentParser(
        description="Score quiet positions with Stockfish for Texel tuning.")
    ap.add_argument("pgn_files", nargs="+", metavar="PGN",
                    help="PGN file(s) to read")
    ap.add_argument("-o", "--output",   default=DEFAULT_OUTPUT,
                    help=f"output CSV (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--stockfish",      default=DEFAULT_STOCKFISH,
                    help=f"Stockfish binary (default: {DEFAULT_STOCKFISH})")
    ap.add_argument("--depth",    type=int, default=DEFAULT_DEPTH,
                    help=f"Stockfish depth per position (default: {DEFAULT_DEPTH})")
    ap.add_argument("--skip-moves", type=int, default=DEFAULT_SKIP_MOVES,
                    help=f"half-moves to skip per game (default: {DEFAULT_SKIP_MOVES})")
    ap.add_argument("--max-per-game", type=int, default=DEFAULT_MAX_GAME,
                    help=f"max positions per game (default: {DEFAULT_MAX_GAME})")
    ap.add_argument("--score-cap", type=int, default=DEFAULT_SCORE_CAP,
                    help=f"discard |score| > this cp (default: {DEFAULT_SCORE_CAP})")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="stop after this many total positions")
    ap.add_argument("--append", action="store_true",
                    help="append to existing output file instead of overwriting")
    args = ap.parse_args()

    try:
        sf = chess.engine.SimpleEngine.popen_uci(args.stockfish)
    except Exception as e:
        sys.exit(f"Cannot start Stockfish ({args.stockfish!r}): {e}")

    # Single-thread, modest hash -- we're doing many short analyses.
    sf.configure({"Threads": 1, "Hash": 64})

    t0 = time.time()
    total = 0
    games = 0

    try:
        mode = "a" if args.append else "w"
        write_header = not (args.append and os.path.exists(args.output))
        with open(args.output, mode, newline="") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(["fen", "score_cp"])

            for path in args.pgn_files:
                with open(path) as pgn_fh:
                    while True:
                        game = chess.pgn.read_game(pgn_fh)
                        if game is None:
                            break
                        games += 1
                        for board in iter_quiet(game, args.skip_moves,
                                                args.max_per_game):
                            info = sf.analyse(
                                board, chess.engine.Limit(depth=args.depth))
                            pov = info["score"].white()
                            if pov.is_mate():
                                continue
                            cp = pov.score()
                            if cp is None or abs(cp) > args.score_cap:
                                continue
                            writer.writerow([board.fen(), cp])
                            total += 1
                            if total % 500 == 0:
                                fh.flush()
                                dt = time.time() - t0
                                rate = total / dt if dt > 0 else 0
                                print(f"  {total:>8,} positions  |  "
                                      f"{games:,} games  |  "
                                      f"{rate:,.0f} pos/s", flush=True)
                            if args.max_positions and total >= args.max_positions:
                                break
                if args.max_positions and total >= args.max_positions:
                    break
    finally:
        sf.quit()

    dt = time.time() - t0
    print(f"\nDone: {total:,} positions from {games} games "
          f"in {dt:.1f}s  ->  {args.output}")


if __name__ == "__main__":
    main()
