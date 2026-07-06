# Chess Equal Position Finder
# Install dependencies with:
#   pip install python-chess stockfish
#
# Also requires Stockfish engine binary installed on your system:
#   - Linux:   sudo apt install stockfish
#   - macOS:   brew install stockfish
#   - Windows: download from https://stockfishchess.org/download/

import chess
import chess.pgn
import chess.engine
import random
import sys
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────
POSITION_LIMIT = 5000  # number of equal positions to find before stopping


INPUT_FILE = "/Users/sam/Downloads/games.pgn"
OUTPUT_FILE = "/Users/sam/Downloads/new_equal_positions.txt"

# Piece count range to target middlegame / early endgame positions
MIN_PIECES = 10
MAX_PIECES = 26

# Stockfish search depth
STOCKFISH_DEPTH = 14

# Equal-position threshold in centipawns (±50 cp = ±0.5 pawns)
EQUAL_THRESHOLD_CP = 40

# Path to the Stockfish binary – adjust if it lives somewhere non-standard
STOCKFISH_PATH = "stockfish"


# ── Helpers ──────────────────────────────────────────────────────────────────

def piece_count(board: chess.Board) -> int:
    """Return the total number of pieces on the board (all colors combined)."""
    return bin(board.occupied).count("1")


def collect_candidate_fens(game: chess.pgn.Game) -> list[str]:
    """
    Replay every move in *game* and collect FEN strings for positions
    whose total piece count is in [MIN_PIECES, MAX_PIECES].
    """
    candidates: list[str] = []
    board = game.board()

    for move in game.mainline_moves():
        board.push(move)
        count = piece_count(board)
        if MIN_PIECES <= count <= MAX_PIECES:
            candidates.append(board.fen())

    return candidates


def evaluate_fen(engine: chess.engine.SimpleEngine, fen: str) -> int | None:
    """
    Ask Stockfish to evaluate *fen* at STOCKFISH_DEPTH.

    Returns the centipawn score from White's perspective, or None on failure.
    Mate scores are mapped to ±100 000 so they are never considered equal.
    """
    try:
        board = chess.Board(fen)
        info = engine.analyse(board, chess.engine.Limit(depth=STOCKFISH_DEPTH))
        score = info["score"].white()

        if score.is_mate():
            # Any forced mate is not an equal position
            return 100_000 if score.mate() > 0 else -100_000

        return score.score()  # centipawns from White's POV

    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] Stockfish evaluation failed for FEN:\n"
              f"         {fen}\n"
              f"         Reason: {exc}", file=sys.stderr)
        return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    input_path = Path(INPUT_FILE)
    if not input_path.exists():
        print(f"[ERROR] Input file '{INPUT_FILE}' not found.", file=sys.stderr)
        sys.exit(1)

    # Open the Stockfish engine once and reuse it for all positions
    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    except FileNotFoundError:
        print(
            f"[ERROR] Stockfish binary not found at '{STOCKFISH_PATH}'.\n"
            "        Install it or set STOCKFISH_PATH to the correct location.",
            file=sys.stderr,
        )
        sys.exit(1)

    equal_fens: list[str] = []

    try:
        with input_path.open() as pgn_file:
            game_index = 0
            games_found = 0
            while games_found < POSITION_LIMIT:
                game = chess.pgn.read_game(pgn_file)
                if game is None:
                    break  # End of file

                game_index += 1
                headers = game.headers
                label = (
                    f"Game {game_index}: "
                    f"{headers.get('White', '?')} vs {headers.get('Black', '?')} "
                    f"({headers.get('Date', '?')})"
                )
                # print(f"Processing {label} …")

                # ── Step 1: collect candidate positions ──────────────────────
                candidates = collect_candidate_fens(game)

                if not candidates:
                    # print(
                    #     f"  [SKIP] No positions with {MIN_PIECES}–{MAX_PIECES} "
                    #     "pieces found in this game."
                    # )
                    continue

                # print(f"  Found {len(candidates)} candidate position(s).")
                for i in range(min(4, len(candidates))):  # evaluate up to 4 random positions per game
                # ── Step 2: pick one at random ───────────────────────────────
                    chosen_fen = random.choice(candidates)

                    # ── Step 3: evaluate with Stockfish ──────────────────────────
                    cp_score = evaluate_fen(engine, chosen_fen)

                    if cp_score is None:
                        # print("  [SKIP] Could not evaluate position.")
                        continue

                    # print(f"  Score: {cp_score:+d} cp")

                    # ── Step 4: keep if roughly equal ────────────────────────────
                    if abs(cp_score) <= EQUAL_THRESHOLD_CP:
                        games_found += 1
                        print(f" {games_found} ✓ Position is roughly equal – saving.")
                        equal_fens.append(chosen_fen)
                        break  # Move on to the next game after finding one equal position

    finally:
        engine.quit()

    # ── Write results ────────────────────────────────────────────────────────
    output_path = Path(OUTPUT_FILE)
    with output_path.open("w") as out_file:
        for fen in equal_fens:
            out_file.write(fen + "\n")

    print(
        f"\nDone. {len(equal_fens)} equal position(s) written to '{OUTPUT_FILE}'."
    )


if __name__ == "__main__":
    main()